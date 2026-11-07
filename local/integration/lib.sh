# Utilidades comunes de los guiones del entorno de integración (TASK-545). Se cargan con `source`.
#
# Variables que respetan: COMPOSE_PROJECT_NAME (otro proyecto, p. ej. el de la prueba),
# VIGIA_INTEGRATION_ENV (archivo con VIGIA_EDGE_BIND y el resto de `.env.example`; por omisión
# `local/integration/.env` si existe), VIGIA_BACKUP_DIR. Ningún guion imprime un secreto.
#
# Todo fallo sale por `fail <código> <mensaje>` con un código cerrado (`error[<código>]: …`): las
# pruebas comparan el código, no el texto.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${VIGIA_INTEGRATION_ENV:-$LIB_DIR/.env}"
COMPOSE=(docker compose -f compose.integration.yml)
if [ -f "$ENV_FILE" ]; then
    COMPOSE+=(--env-file "$ENV_FILE")
fi

fail() {
    local code="$1"
    shift
    echo "error[$code]: $*" >&2
    exit 1
}

# Corre una orden sin ruido: si falla, vuelca su salida a stderr y devuelve su código.
quiet() {
    local log status=0
    log="$(mktemp)"
    "$@" >"$log" 2>&1 || status=$?
    if [ "$status" -ne 0 ]; then
        cat "$log" >&2
    fi
    rm -f "$log"
    return "$status"
}

# La IP de publicación de vigia-mtls-edge, de la variable o del archivo de entorno. Solo se acepta
# la IP de la tailnet o el bucle local (lista de aceptación de `edge_guard.py`, la misma que aplica
# el contenedor): 0.0.0.0, ::ffff:0.0.0.0, la LAN y las IP públicas se rechazan aquí, antes de
# tocar nada.
require_edge_bind() {
    local value="${VIGIA_EDGE_BIND:-}"
    if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
        value="$(sed -n 's/^VIGIA_EDGE_BIND=//p' "$ENV_FILE" | tail -n 1)"
    fi
    [ -n "$value" ] || fail edge_bind_missing "falta VIGIA_EDGE_BIND (la IP de la tailnet): ponla en $ENV_FILE o en el entorno"
    python3 "$LIB_DIR/edge_guard.py" --check "$value" 2>/dev/null ||
        fail edge_bind_not_allowed "VIGIA_EDGE_BIND=$value no es el bucle local ni la IP de la tailnet (100.64.0.0/10)"
}

# El directorio de las copias de la base (0700, fuera del árbol).
backup_dir() {
    local destination="${VIGIA_BACKUP_DIR:-$HOME/vigia-integration-backups}"
    (umask 077 && mkdir -p "$destination")
    chmod 700 "$destination"
    printf '%s' "$destination"
}

# Valida una copia de verdad: la lee ENTERA (`pg_restore --file=/dev/null` recorre también los
# datos; `--list` solo lee el índice y da 0 con la copia truncada por el final).
validate_dump() {
    "${COMPOSE[@]}" exec -T postgres pg_restore --file=/dev/null <"$1" >/dev/null 2>&1
}

# Una copia `--create` de la base viva en el archivo dado (0600); la valida antes de darla por buena.
dump_live_database() {
    local target="$1"
    "${COMPOSE[@]}" exec -T postgres pg_dump --format=custom --create --username=vigia --dbname=vigia >"$target" ||
        fail dump_failed "pg_dump falló; no se escribió ninguna copia"
    [ -s "$target" ] || fail dump_empty "la copia salió vacía"
    validate_dump "$target" || fail dump_unreadable "pg_restore no lee la copia entera"
}
