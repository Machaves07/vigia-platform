#!/bin/sh
# Un reinicio conserva /tmp del contenedor: sin esto, el marcador de la vez anterior daria por
# restaurado un arranque que aun no lo esta (TASK-545).
rm -f "${VIGIA_RESTORE_MARKER:-/tmp/vigia-restored}"
