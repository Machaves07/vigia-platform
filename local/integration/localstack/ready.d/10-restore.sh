#!/bin/sh
# Recrea el estado de LocalStack (ver state.py) y deja la copia periodica en marcha (TASK-545).
set -eu
python3 /opt/vigia/state.py restore
nohup setsid sh -c 'while sleep "${VIGIA_SAVE_SECONDS:-10}"; do python3 /opt/vigia/state.py save || true; done' >/dev/null 2>&1 &
