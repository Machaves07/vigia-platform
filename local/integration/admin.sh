#!/usr/bin/env bash
# `vigia-admin` contra el entorno de integración (TASK-545): `local/integration/admin.sh rotate-key
# catalog --operator <id>`. Al terminar fuerza la copia de LocalStack (los secretos de firma nuevos
# no esperan a la copia periódica). Para la primera instalación usa `bootstrap.sh`.
set -euo pipefail
# shellcheck source=local/integration/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_edge_bind

status=0
"${COMPOSE[@]}" run --rm -T vigia-admin vigia-admin "$@" || status=$?
quiet "${COMPOSE[@]}" exec -T localstack python3 /opt/vigia/state.py save
exit "$status"
