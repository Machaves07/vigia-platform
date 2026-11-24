#!/usr/bin/env bash
# Restaura la base de integración desde una copia de `backup.sh` (TASK-545, VIG-393).
#
#   local/integration/restore.sh --yes /ruta/vigia-20261107T030000Z.dump
#
# SUSTITUYE el contenido de la base `vigia`: por eso exige `--yes`. Y no borra nada hasta haber
# comprobado la copia nueva y guardado la viva:
#
#   1. lee la copia ENTERA (`pg_restore --file=/dev/null`: `--list` no detecta una copia truncada);
#   2. la restaura de prueba en una base temporal (`vigia_restore_check`) y la borra;
#   3. guarda una copia de la base viva (`previa-<fecha>-<pid>.dump` en el directorio de las copias);
#   4. solo entonces para `vigia-api`, `vigia-worker` y `vigia-mtls-edge`, corta las conexiones y
#      restaura con `pg_restore --create --clean --if-exists` (con las tablas particionadas,
#      `--clean` sobre la base viva falla). Si esa restauración falla, restaura la copia previa y
#      termina con error.
#
# Si junto a la copia está su `<copia>.almacen.tar` (el espejo de vigia-evidence, TASK-608), también
# lo comprueba antes de tocar nada, guarda el vivo (`previa-<fecha>-<pid>.almacen.tar`) y, tras la
# base, sustituye el volumen `almacen` con `localstack` parado; al volver a levantarlo,
# `state.py restore` sube los objetos que falten. Sin ese archivo (copias anteriores), el almacén
# no se toca.
#
# Los roles `vigia_app` y `vigia_migrate` son del servidor, no de la base: en un servidor nuevo,
# `up -d --wait` y `docker compose ... run --rm vigia-migrate` los crean antes de este guion. La
# clave de la autoridad de nodos y los secretos no vienen en la copia.
set -euo pipefail
# shellcheck source=local/integration/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_edge_bind

[ "${1:-}" = "--yes" ] || fail restore_needs_yes "restaurar sustituye la base: repite con --yes <copia.dump>"
file="${2:-}"
[ -n "$file" ] && [ -f "$file" ] || fail restore_missing_file "indica la copia: restore.sh --yes <archivo.dump>"

validate_dump "$file" || fail dump_unreadable "pg_restore no lee la copia entera: $file (¿truncada?); no se tocó nada"
almacen="${file%.dump}.almacen.tar"
if [ -f "$almacen" ]; then
    validate_almacen "$almacen" || fail almacen_unreadable "tar no lee entera $almacen (¿truncada?); no se tocó nada"
else
    almacen=""
fi

check_database="vigia_restore_check"
psql_admin() { "${COMPOSE[@]}" exec -T postgres psql --username=vigia --dbname=postgres --no-align --tuples-only "$@"; }
psql_admin --command "DROP DATABASE IF EXISTS $check_database" --command "CREATE DATABASE $check_database" >/dev/null ||
    fail restore_check_failed "no se pudo crear la base temporal de comprobación"
checked=0
"${COMPOSE[@]}" exec -T postgres pg_restore --no-owner --no-acl --exit-on-error \
    --username=vigia --dbname="$check_database" <"$file" >/dev/null 2>&1 || checked=1
psql_admin --command "DROP DATABASE IF EXISTS $check_database" >/dev/null || true
[ "$checked" -eq 0 ] || fail restore_check_failed "la copia no se restaura ni en una base temporal; no se tocó nada"

destination="$(backup_dir)"
umask 077
temporary="$(mktemp "$destination/.previa-XXXXXX")"
trap 'rm -f "$temporary"' EXIT
dump_live_database "$temporary"
previous="$destination/previa-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
mv "$temporary" "$previous"
previous_almacen="${previous%.dump}.almacen.tar"
if [ -n "$almacen" ]; then
    temporary="$(mktemp "$destination/.previa-almacen-XXXXXX")"
    dump_live_almacen "$temporary"
    mv "$temporary" "$previous_almacen"
fi

quiet "${COMPOSE[@]}" --profile app stop vigia-mtls-edge vigia-api-1 vigia-api-2 vigia-worker
quiet "${COMPOSE[@]}" up -d --wait postgres
restore_from() {
    psql_admin --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'vigia' AND pid <> pg_backend_pid()" >/dev/null &&
        "${COMPOSE[@]}" exec -T postgres pg_restore --create --clean --if-exists --exit-on-error \
            --username=vigia --dbname=postgres <"$1" >/dev/null
}
if ! restore_from "$file"; then
    recovered="la base viva queda como estaba"
    restore_from "$previous" || recovered="NO se pudo devolver la base viva: la copia previa es $previous"
    quiet "${COMPOSE[@]}" --profile app up -d --wait || true
    fail restore_failed "la restauración falló; $recovered"
fi
if [ -n "$almacen" ]; then
    quiet "${COMPOSE[@]}" stop localstack
    if ! replace_almacen "$almacen"; then
        recovered="el almacén vivo queda como estaba"
        replace_almacen "$previous_almacen" ||
            recovered="NO se pudo devolver el almacén vivo: la copia previa es $previous_almacen"
        quiet "${COMPOSE[@]}" --profile app up -d --wait || true
        fail almacen_restore_failed "la base se restauró, pero el almacén no; $recovered"
    fi
fi
quiet "${COMPOSE[@]}" --profile app up -d --wait
echo "restaurada: $(basename "$file") (la base anterior, en $(basename "$previous"))"
