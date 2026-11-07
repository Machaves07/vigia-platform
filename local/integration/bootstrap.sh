#!/usr/bin/env bash
# Primera instalación y arranque repetible del entorno de integración (TASK-545, VIG-393; A-89).
#
#   local/integration/bootstrap.sh --first-install    # la primera vez: crea la autoridad de nodos
#   local/integration/bootstrap.sh                    # después: receta de reinicio, idempotente
#
# Sin `--first-install` la clave de la autoridad (volumen `autoridad`) **nunca se crea**: si falta,
# `vigia-admin` falla y este guion también, en lugar de publicar otra autoridad que dejaría huérfanos
# a los nodos ya dados de alta (A-89). Con la organización proveedora ya en la base, la marca se
# ignora: el guion hace `bootstrap --resume --publish-root`, que reutiliza la clave o falla.
#
# Hace, en este orden y sin repetir lo hecho: infraestructura (`up --wait`), migraciones, `vigia-admin
# bootstrap` (organización proveedora, claves de firma y `ca/root.pem`), la organización y la planta
# sintéticas con un usuario por rol (`seed.py site`), los umbrales de flota, la copia de las
# credenciales de prueba fuera del árbol y, al final, el perfil `app`. La salida es solo el resumen,
# igual en cada ejecución; el detalle de Docker va a un archivo temporal que se muestra si algo falla.
set -euo pipefail
# shellcheck source=local/integration/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

first_install=0
for argument in "$@"; do
    case "$argument" in
        --first-install) first_install=1 ;;
        *) fail "argumento desconocido: $argument (solo --first-install)" ;;
    esac
done
require_edge_bind

CREDENTIALS_FILE="${VIGIA_INTEGRATION_CREDENTIALS:-${XDG_STATE_HOME:-$HOME/.local/state}/vigia-integration/credentials.json}"

# Corre una orden en el servicio vigia-admin y deja su última línea JSON en $RESULT.
run_admin() {
    local out err
    err="$(mktemp)"
    out="$("${COMPOSE[@]}" run --rm -T vigia-admin "$@" 2>"$err")" || {
        cat "$err" >&2
        printf '%s\n' "$out" >&2
        rm -f "$err"
        return 1
    }
    rm -f "$err"
    RESULT="$(printf '%s\n' "$out" | grep '^{' | tail -n 1)"
}

json_field() {
    printf '%s' "$RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

quiet "${COMPOSE[@]}" up -d --wait
quiet "${COMPOSE[@]}" run --rm -T vigia-migrate

provider="$("${COMPOSE[@]}" exec -T postgres psql --username=vigia --dbname=vigia --no-align --tuples-only \
    --command "SELECT organization_id FROM identity.organization WHERE kind = 'provider' ORDER BY created_at LIMIT 1")"

if [ -z "$provider" ]; then
    arguments=(vigia-admin bootstrap --organization-code VIGIA-PROV
        --organization-name "Proveedora de integración"
        --operator-email operadora@integracion.example.test
        --operator-name "Operadora de integración" --yes)
    if [ "$first_install" -eq 1 ]; then
        arguments+=(--first-install)
    fi
    run_admin "${arguments[@]}"
else
    # La proveedora ya existe: receta de reinicio, sin crear nada. Primero el identificador, que
    # lee `vigia-admin` del volumen `proveedora`.
    run_admin python /integration/seed.py provider-id
    run_admin vigia-admin bootstrap --resume --publish-root --yes
fi
bootstrap_result="$RESULT"
run_admin python /integration/seed.py provider-id
provider="$(json_field provider_organization_id)"

run_admin python /integration/seed.py site

credentials_dir="$(dirname "$CREDENTIALS_FILE")"
[ -d "$credentials_dir" ] || mkdir -p -m 700 "$credentials_dir"
(
    umask 077
    "${COMPOSE[@]}" run --rm -T vigia-admin sh -c 'cat /state/credentials.json 2>/dev/null || echo "{}"' \
        2>/dev/null >"$CREDENTIALS_FILE.tmp"
    mv "$CREDENTIALS_FILE.tmp" "$CREDENTIALS_FILE"
)

# Punto de durabilidad: secretos de firma y raíz publicados, a disco ya (ver localstack/state.py).
quiet "${COMPOSE[@]}" exec -T localstack python3 /opt/vigia/state.py save
quiet "${COMPOSE[@]}" --profile app up -d --wait

RESULT="$bootstrap_result"
echo "proveedora: $provider"
echo "raiz_de_nodos: $(json_field root_sha256)"
echo "organizacion: INT-CLI"
echo "planta: INT-PLANTA"
echo "credenciales_de_prueba: $CREDENTIALS_FILE"
echo "BOOTSTRAP_OK"
