"""Uso del asistente por solicitud, causa ``budget_exceeded`` y borrador sin citas declarado
(TASK-316; LC-LAZ-13 y 15; NFR-LAZ-31 y 35; BR-LAZ-71; PAT-LAZ-ESC-02).

Revisión laz_0033, aditiva sobre ``loop.assistant_request`` y ``loop.assistant_draft``
(``laz_0029``); ninguna tabla nueva:

- **Uso por solicitud** (NFR-LAZ-31): ``input_tokens``, ``output_tokens`` y
  ``cache_read_tokens`` (acumulados de todos los intentos), ``provider_duration_ms``, y del último
  intento con respuesta ``response_model``, ``inference_geo``, ``stop_reason`` y
  ``provider_request_id``. Nunca el contenido: el contexto que salió se audita por
  ``context_digest``. El **tope mensual de fichas** por organización (NFR-LAZ-35) se cuenta sobre
  estas columnas, la misma fila que el límite de tasa (el contador y la huella son el mismo dato).
  ``NOT NULL DEFAULT 0`` en los contadores: la imagen anterior inserta sin ellos.
- Son **columnas de trabajo** del guardián ⛓ (cuarto argumento de ``catalog.guard_update``, como
  ``attempt_count``): el disparador ``append_only_update`` se reemplaza con la lista ampliada y
  sigue ``ENABLE ALWAYS``. Los campos de la solicitud siguen sin cambiar; ``vigia_app`` recibe
  ``UPDATE`` solo sobre estas columnas.
- ``unavailable_cause`` admite ``budget_exceeded``: al 100 % del tope de fichas la solicitud queda
  ``unavailable`` **sin construir el contexto** (TASK-316, PAT-LAZ-ESC-01). Solo se añade un
  valor; la imagen anterior no lo escribe nunca.
- ``assistant_draft.proposed_cause_category``: la categoría que propone el borrador (una de las
  tres, tech-stack-decisions §2.3), nula en la imagen anterior, que no escribe borradores.
- ``assistant_draft.citations`` admite una lista vacía **solo** con
  ``insufficient_evidence_declared`` (BR-LAZ-71: citas concretas **o** declaración de lo que
  falta). Antes exigía al menos una cita y un borrador que declaraba evidencia insuficiente sin
  citar no se podía guardar.
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "laz_0033"
down_revision: str | None = "laz_0032"
branch_labels: None = None
depends_on: None = None

USAGE_COLUMNS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "provider_duration_ms",
    "response_model",
    "inference_geo",
    "stop_reason",
    "provider_request_id",
)
"""Las columnas de uso de ``assistant_request``: de trabajo para el guardián ⛓."""

WORK_COLUMNS: Final = ("attempt_count", *USAGE_COLUMNS)
"""Cuarto argumento de ``catalog.guard_update`` en ``assistant_request`` desde esta revisión."""

_CLOSING: Final = "{resolved_at,unavailable_cause}"
_TRANSITIONS: Final = "{pending>ready,pending>unavailable,pending>rejected_by_validators}"


def _length(column: str, low: int, high: int) -> str:
    return f"CHECK (char_length({column}) BETWEEN {low} AND {high})"


_STATEMENTS: Final = (
    f"""
    ALTER TABLE loop.assistant_request
        ADD COLUMN input_tokens bigint NOT NULL DEFAULT 0
            CONSTRAINT assistant_request_input_tokens CHECK (input_tokens >= 0),
        ADD COLUMN output_tokens bigint NOT NULL DEFAULT 0
            CONSTRAINT assistant_request_output_tokens CHECK (output_tokens >= 0),
        ADD COLUMN cache_read_tokens bigint NOT NULL DEFAULT 0
            CONSTRAINT assistant_request_cache_read_tokens CHECK (cache_read_tokens >= 0),
        ADD COLUMN provider_duration_ms bigint NOT NULL DEFAULT 0
            CONSTRAINT assistant_request_provider_duration_ms CHECK (provider_duration_ms >= 0),
        ADD COLUMN response_model text
            CONSTRAINT assistant_request_response_model_length
                {_length("response_model", 1, 128)},
        ADD COLUMN inference_geo text
            CONSTRAINT assistant_request_inference_geo_length {_length("inference_geo", 1, 32)},
        ADD COLUMN stop_reason text
            CONSTRAINT assistant_request_stop_reason_length {_length("stop_reason", 1, 64)},
        ADD COLUMN provider_request_id text
            CONSTRAINT assistant_request_provider_request_id_length
                {_length("provider_request_id", 1, 128)}
    """,
    "ALTER TABLE loop.assistant_request DROP CONSTRAINT assistant_request_unavailable_cause_values",
    """
    ALTER TABLE loop.assistant_request
        ADD CONSTRAINT assistant_request_unavailable_cause_values CHECK (
            unavailable_cause IN
                ('no_api_key', 'circuit_open', 'timeout', 'provider_error', 'budget_exceeded')
        )
    """,
    f"""
    CREATE OR REPLACE TRIGGER append_only_update BEFORE UPDATE ON loop.assistant_request
        FOR EACH ROW EXECUTE FUNCTION catalog.guard_update(
            '{_CLOSING}', 'state', '{_TRANSITIONS}', '{{{",".join(WORK_COLUMNS)}}}'
        )
    """,
    "ALTER TABLE loop.assistant_request ENABLE ALWAYS TRIGGER append_only_update",
    f"GRANT UPDATE ({', '.join(USAGE_COLUMNS)}) ON loop.assistant_request TO vigia_app",
    """
    ALTER TABLE loop.assistant_draft
        ADD COLUMN proposed_cause_category text
            CONSTRAINT assistant_draft_proposed_cause_category_values CHECK (
                proposed_cause_category IN ('process', 'engineering', 'organization')
            )
    """,
    "ALTER TABLE loop.assistant_draft DROP CONSTRAINT assistant_draft_citations",
    """
    ALTER TABLE loop.assistant_draft
        ADD CONSTRAINT assistant_draft_citations CHECK (
            CASE WHEN jsonb_typeof(citations) = 'array'
                THEN jsonb_array_length(citations) BETWEEN 0 AND 100
                    AND (jsonb_array_length(citations) >= 1 OR insufficient_evidence_declared)
                ELSE false END
        )
    """,
    "COMMENT ON COLUMN loop.assistant_request.input_tokens IS"
    " 'Fichas de entrada de todos los intentos (NFR-LAZ-31 y 35, TASK-316)'",
    "COMMENT ON COLUMN loop.assistant_request.inference_geo IS"
    " 'Región donde el proveedor ejecutó el último intento con respuesta (NFR-LAZ-31)'",
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
