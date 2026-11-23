"""Índice parcial del histórico de lo cerrado (TASK-586; O-11; PAT-LAZ-ESC-03).

Revisión laz_0044, aditiva: un índice. La tarea la nombraba ``laz_0042``; ``gob_0042`` y
``gob_0043`` ocuparon esos números antes (la cadena numera las tres unidades a la vez), así que se
renumera a la siguiente libre, como prevé la tarea.

- ``finding_state_closed`` sobre ``loop.finding_state``, con las mismas columnas que
  ``finding_state_inbox`` (``organization_id, plant_id, zone_id, lifecycle_state,
  occurred_at_bucket DESC, finding_state_id``) y el predicado de los **dos** estados del
  histórico, ``closed`` y ``discarded``, que el índice de la bandeja no cubre (solo los siete
  activos). Sobre la tabla particionada: PostgreSQL crea su copia en cada partición, también en
  las que ``create_partitions`` cree después.
- La sentencia de ``loop.adapters.postgres.history_query`` repite los dos estados como literal
  para que el planificador lo use también con un plan genérico.

La imagen anterior no lee ni escribe nada nuevo: arranca igual sobre este esquema (NFR-NUC-14).
``MINIMUM_SCHEMA_VERSION`` no sube: el histórico es correcto sin el índice (solo más lento).
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "laz_0044"
down_revision: str | None = "gob_0043"
branch_labels: None = None
depends_on: None = None

INDEX: Final = "finding_state_closed"
CLOSED_STATES: Final = ("closed", "discarded")
"""Los estados del índice (una prueba exige que coincidan con ``CLOSED_STATES`` del lazo)."""

_STATEMENTS: Final = (
    f"""
    CREATE INDEX {INDEX} ON loop.finding_state
        (organization_id, plant_id, zone_id, lifecycle_state, occurred_at_bucket DESC,
         finding_state_id)
        WHERE lifecycle_state IN ({", ".join(f"'{state}'" for state in CLOSED_STATES)})
    """,
    f"COMMENT ON INDEX loop.{INDEX} IS"
    " 'Histórico de lo cerrado: closed y discarded, en orden de cubo (TASK-586, PAT-LAZ-ESC-03)'",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Solo hacia adelante (NFR-NUC-14).")
