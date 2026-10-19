"""Orden único de candados del lazo (TASK-311; AGENTS.md, «Orden de candados entre operaciones»).

Toda escritura de U-04 sobre un hallazgo (clasificación, acción, evidencia, cierre, incerrable,
reapertura y la tarea de plazos) toma sus candados **en este orden** y solo después escribe
(``LOCK_ORDER``):

1. ``finding``: la exclusión del hallazgo, ``pg_advisory_xact_lock`` sobre
   ``loop_subject|<organización>|<sujeto>`` (``lock_finding``), **antes** de leer su estado. Se
   toma sobre el sujeto (``subject_id``: el episodio o la detección), que es uno a uno con
   ``finding_state_id`` y la clave que ya usaba la clasificación (TASK-308): así todas las
   operaciones sobre el mismo hallazgo comparten **una** clave. Una operación sobre varios
   hallazgos (la tarea de plazos, TASK-313) los toma todos, ordenados por
   ``(organización, sujeto)``, antes de cualquier otra cosa;
2. ``corrective_actions``: las filas de ``loop.corrective_action`` del hallazgo, todas de una vez
   con ``SELECT … ORDER BY action_id FOR UPDATE`` (``lock_actions``), antes de escribir;
3. ``plant_chain``: la cadena de la planta, que toma el disparador del expediente al anexar
   (``EscritorExpediente`` dentro de la transacción);
4. ``projection_checkpoint``: la fila de control de la proyección de la planta
   (``LoopProjectionStore.apply_in_transaction``, ``SELECT … FOR UPDATE``);
5. ``subject_rows``: las filas ``episode`` y ``finding_state`` del sujeto (el mismo almacén).

Los pasos 3 a 5 los toma ``LoopProjectionStore.write_and_apply`` en ese orden (su docstring); las
filas propias de la operación (``corrective_action``, ``action_evidence_entry``,
``closure_attachment``) se escriben después. La vía de eventos (TASK-341) y la configuración del
lazo (``loop_configuration|<planta>``, que **no** toma ningún candado de hallazgo) solo toman
candados a partir del 3, después de nadie: no hay ciclo. **Nunca** se toma 1 o 2 después de haber
escrito en el expediente.

Este módulo no lee ni escribe nada más que los candados; los estados los interpreta cada operación.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.shared.db import Transaction

__all__ = ["FINDING_LOCK_PREFIX", "LOCK_ORDER", "lock_actions", "lock_finding"]

LOCK_ORDER: Final = (
    "finding",
    "corrective_actions",
    "plant_chain",
    "projection_checkpoint",
    "subject_rows",
)
"""El orden único de los candados de una escritura del lazo (ver el docstring del módulo)."""

FINDING_LOCK_PREFIX: Final = "loop_subject"
"""Prefijo de la clave de la exclusión del hallazgo (el de TASK-308, sin cambios)."""

_LOCK_FINDING: Final = text(
    "SELECT pg_advisory_xact_lock("
    "hashtextextended('loop_subject|' || :organization_id || '|' || :subject_id, 0))"
)
_LOCK_ACTIONS: Final = text(
    "SELECT action_id, record_id, action_type, assignee_user_id, assigned_at, due_at, state"
    " FROM loop.corrective_action"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND subject_id = :subject_id"
    " ORDER BY action_id FOR UPDATE"
)


async def lock_finding(
    transaction: Transaction, organization_id: uuid.UUID, subject_id: uuid.UUID
) -> None:
    """Paso 1: la exclusión del hallazgo hasta el fin de la transacción."""
    await transaction.execute(
        _LOCK_FINDING, {"organization_id": str(organization_id), "subject_id": str(subject_id)}
    )


async def lock_actions(
    transaction: Transaction,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> Sequence[Any]:
    """Paso 2: las acciones del hallazgo, bloqueadas de una vez y en orden de ``action_id``."""
    result = await transaction.execute(
        _LOCK_ACTIONS,
        {"organization_id": organization_id, "plant_id": plant_id, "subject_id": subject_id},
    )
    return result.all()
