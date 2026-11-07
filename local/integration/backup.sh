#!/usr/bin/env bash
# Copia diaria de la base de integración con `pg_dump` (TASK-545, VIG-393). Para el `cron` del host
# (el directorio del registro debe existir antes: `mkdir -p ~/vigia-integration-backups`):
#
#   0 3 * * * cd /ruta/a/vigia-platform && local/integration/backup.sh >> ~/vigia-integration-backups/backup.log 2>&1
#
# Escribe `vigia-<fecha UTC>-<pid>.dump` (formato custom con `--create`, 0600, en un directorio 0700) en
# VIGIA_BACKUP_DIR (por omisión `~/vigia-integration-backups`, fuera del árbol), la lee ENTERA con
# `pg_restore --file=/dev/null` (`--list` no detecta una copia truncada por el final) y borra las de
# más de VIGIA_BACKUP_KEEP_DAYS días (14). Una copia que falla a medias no deja un archivo que
# parezca bueno. No incluye la clave de la autoridad de nodos ni los secretos: ver el README.
set -euo pipefail
# shellcheck source=local/integration/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_edge_bind

destination="$(backup_dir)"
keep_days="${VIGIA_BACKUP_KEEP_DAYS:-14}"
umask 077
# Dos copias a la vez (el `cron` y una a mano) no se pisan: cada una escribe su propio temporal y
# su propio nombre (la fecha y el pid) y solo lo publica, con `mv`, cuando ya la comprobó.
temporary="$(mktemp "$destination/.vigia-XXXXXX")"
trap 'rm -f "$temporary"' EXIT
file="$destination/vigia-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
dump_live_database "$temporary"
mv "$temporary" "$file"
find "$destination" -maxdepth 1 -name 'vigia-*.dump' -mtime +"$keep_days" -delete
echo "$file"
