#!/bin/sh
# Recrea el estado de LocalStack (ver state.py) y deja la copia periodica en marcha (TASK-545).
# La copia incluye el espejo de vigia-evidence en /almacen (TASK-608). Su salida se tira; los
# avisos (almacen_lleno, objeto_no_guardado) van al registro del contenedor por VIGIA_EVENT_LOG.
set -eu
python3 /opt/vigia/state.py restore
nohup setsid sh -c 'while sleep "${VIGIA_SAVE_SECONDS:-10}"; do VIGIA_EVENT_LOG=/proc/1/fd/2 python3 /opt/vigia/state.py save || true; done' >/dev/null 2>&1 &
