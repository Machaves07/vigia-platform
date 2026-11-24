#!/bin/sh
# Un reinicio conserva /tmp del contenedor: sin esto, el marcador de la vez anterior daria por
# restaurado un arranque que aun no lo esta (TASK-545). Lo mismo la marca propia de las evidencias,
# que habilita las bajas del espejo de /almacen (TASK-608).
rm -f "${VIGIA_RESTORE_MARKER:-/tmp/vigia-restored}" "${VIGIA_EVIDENCE_RESTORE_MARKER:-/tmp/vigia-evidence-restored}"
