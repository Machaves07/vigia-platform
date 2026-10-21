"""Frontera contigua de ingesta proyectada y acceso acotado a sus eventos (VIG-273).

No se copia el máximo compartido: cero obliga a verificar la historia anterior al primer
avance o a reconstruir. Es aditiva; la imagen N-1 conserva sus escrituras y sus tareas.
"""

from __future__ import annotations

from alembic import op

revision: str = "laz_0040"
down_revision: str | None = "laz_0039"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    ALTER TABLE loop.loop_projection_checkpoint
        ADD COLUMN ingestion_frontier bigint NOT NULL DEFAULT 0
            CONSTRAINT loop_projection_ingestion_frontier_nonnegative
                CHECK (ingestion_frontier >= 0)
    """,
    """
    COMMENT ON COLUMN loop.loop_projection_checkpoint.ingestion_frontier IS
        'Prefijo de ingestas comprobado en episode/finding_state, no el máximo compartido;
         avanza atómicamente con la proyección o su reconstrucción (VIG-273)'
    """,
    "GRANT UPDATE (ingestion_frontier) ON loop.loop_projection_checkpoint TO vigia_app",
    """
    CREATE INDEX outbox_event_loop_ingestion ON shared.outbox_event
        (organization_id, plant_id, ledger_sequence)
        WHERE event_name IN ('finding_received', 'detection_for_review_received')
    """,
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Solo hacia adelante (NFR-NUC-14).")
