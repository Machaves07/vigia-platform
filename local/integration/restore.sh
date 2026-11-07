#!/usr/bin/env bash
# Restaura la base de integración desde una copia de `backup.sh` (TASK-545, VIG-393).
#
#   local/integration/restore.sh --yes /ruta/vigia-20261107T030000Z.dump
#
# SUSTITUYE el contenido de la base `vigia`: por eso exige `--yes`. Para `vigia-api`,
# `vigia-worker` y `vigia-mtls-edge`, corta las conexiones que queden, restaura con
# `pg_restore --create --clean --if-exists` (borra y recrea la base con los permisos de base de la
# copia; con las tablas particionadas, `--clean` sobre la base viva falla) y vuelve a levantar el
# perfil `app`. Los roles `vigia_app` y `vigia_migrate` son del servidor, no de la base: en un
# servidor nuevo, `up -d --wait` y `docker compose ... run --rm vigia-migrate` los crean antes de
# este guion. La clave de la autoridad de nodos y los secretos no vienen en la copia.
set -euo pipefail
# shellcheck source=local/integration/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_edge_bind

[ "${1:-}" = "--yes" ] || fail "restaurar sustituye la base: repite con --yes <copia.dump>"
file="${2:-}"
[ -n "$file" ] && [ -f "$file" ] || fail "indica la copia: restore.sh --yes <archivo.dump>"

"${COMPOSE[@]}" exec -T postgres pg_restore --list <"$file" >/dev/null || fail "pg_restore no lee $file"
quiet "${COMPOSE[@]}" --profile app stop vigia-mtls-edge vigia-api-1 vigia-api-2 vigia-worker
quiet "${COMPOSE[@]}" up -d --wait postgres
"${COMPOSE[@]}" exec -T postgres psql --username=vigia --dbname=postgres --no-align --tuples-only \
    --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'vigia' AND pid <> pg_backend_pid()" >/dev/null
"${COMPOSE[@]}" exec -T postgres pg_restore --create --clean --if-exists --exit-on-error \
    --username=vigia --dbname=postgres <"$file" >/dev/null
quiet "${COMPOSE[@]}" --profile app up -d --wait
echo "restaurada: $(basename "$file")"
