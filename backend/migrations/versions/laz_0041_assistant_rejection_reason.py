"""Motivo del rechazo de una salida del asistente, sin su texto (VIG-344; A-68; TASK-316).

Revisión laz_0041, aditiva sobre ``loop.assistant_request`` (``laz_0029`` y ``laz_0033``):

- ``rejection_reason``: el motivo cerrado por el que una salida quedó ``rejected_by_validators``
  (``RejectionReason`` de ``loop.assistant.consumer``). Solo el código: el texto rechazado nunca
  se guarda (PR-LAZ-20). ``GET /loop/assistant-requests/{id}`` lo devuelve en ``rejection.code``
  cuando es de uno de los dos validadores de U-04.
- Nula fuera de ``rejected_by_validators`` (``assistant_request_rejection_reason_only_rejected``)
  y nula también en las solicitudes que la imagen anterior rechazó: esa imagen no la escribe y la
  respuesta sale sin ``rejection``, como antes.
- Es un **cierre** más del guardián ⛓ (primer argumento de ``catalog.guard_update``): pasa de
  nulo a su valor una sola vez, en la misma sentencia que saca ``state`` de ``pending``. El
  disparador ``append_only_update`` se reemplaza con la lista ampliada y las mismas columnas de
  trabajo de ``laz_0033``, y sigue ``ENABLE ALWAYS``. ``vigia_app`` recibe ``UPDATE`` solo sobre
  esta columna nueva.
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "laz_0041"
down_revision: str | None = "laz_0040"
branch_labels: None = None
depends_on: None = None

REJECTION_REASONS: Final = (
    "schema_invalid",
    "citation_outside_list",
    "no_evidence",
    "blocked_vocabulary_term",
    "person_attribution_detected",
    "free_text_policy",
)
"""Los valores de ``RejectionReason`` (una prueba exige que coincidan)."""

CLOSING_COLUMNS: Final = ("resolved_at", "unavailable_cause", "rejection_reason")
"""Primer argumento de ``catalog.guard_update`` en ``assistant_request`` desde esta revisión."""

WORK_COLUMNS: Final = (
    "attempt_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "provider_duration_ms",
    "response_model",
    "inference_geo",
    "stop_reason",
    "provider_request_id",
)
"""Las columnas de trabajo de ``laz_0033``, sin cambios."""

_TRANSITIONS: Final = "{pending>ready,pending>unavailable,pending>rejected_by_validators}"


def _array(items: tuple[str, ...]) -> str:
    return "{" + ",".join(items) + "}"


_STATEMENTS: Final = (
    f"""
    ALTER TABLE loop.assistant_request
        ADD COLUMN rejection_reason text
            CONSTRAINT assistant_request_rejection_reason_values CHECK (
                rejection_reason IN ({", ".join(f"'{reason}'" for reason in REJECTION_REASONS)})
            ),
        ADD CONSTRAINT assistant_request_rejection_reason_only_rejected CHECK (
            rejection_reason IS NULL OR state = 'rejected_by_validators'
        )
    """,
    f"""
    CREATE OR REPLACE TRIGGER append_only_update BEFORE UPDATE ON loop.assistant_request
        FOR EACH ROW EXECUTE FUNCTION catalog.guard_update(
            '{_array(CLOSING_COLUMNS)}', 'state', '{_TRANSITIONS}', '{_array(WORK_COLUMNS)}'
        )
    """,
    "ALTER TABLE loop.assistant_request ENABLE ALWAYS TRIGGER append_only_update",
    "GRANT UPDATE (rejection_reason) ON loop.assistant_request TO vigia_app",
    "COMMENT ON COLUMN loop.assistant_request.rejection_reason IS"
    " 'Motivo cerrado de rejected_by_validators, sin el texto rechazado (VIG-344, PR-LAZ-20)'",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
