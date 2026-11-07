# Utilidades comunes de los guiones del entorno de integración (TASK-545). Se cargan con `source`.
#
# Variables que respetan: COMPOSE_PROJECT_NAME (otro proyecto, p. ej. el de la prueba),
# VIGIA_INTEGRATION_ENV (archivo con VIGIA_EDGE_BIND y el resto de `.env.example`; por omisión
# `local/integration/.env` si existe). Ningún guion imprime un secreto.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$LIB_DIR/../.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="${VIGIA_INTEGRATION_ENV:-$LIB_DIR/.env}"
COMPOSE=(docker compose -f compose.integration.yml)
if [ -f "$ENV_FILE" ]; then
    COMPOSE+=(--env-file "$ENV_FILE")
fi

fail() {
    echo "error: $*" >&2
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

# La IP de publicación de vigia-mtls-edge, de la variable o del archivo de entorno.
require_edge_bind() {
    local value="${VIGIA_EDGE_BIND:-}"
    if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
        value="$(sed -n 's/^VIGIA_EDGE_BIND=//p' "$ENV_FILE" | tail -n 1)"
    fi
    [ -n "$value" ] || fail "falta VIGIA_EDGE_BIND (la IP de la tailnet): ponla en $ENV_FILE o en el entorno"
    case "$value" in
        0.0.0.0 | :: | "[::]") fail "VIGIA_EDGE_BIND=$value publicaría en todas las interfaces" ;;
    esac
}
