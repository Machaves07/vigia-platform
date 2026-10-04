"""``record_id`` en la historia de compuertas (TASK-211, LC-GOB-03; interfaces §1.2).

Revisión gob_0019, aditiva. ``GateQueryPort.state_at`` y ``gate_history`` devuelven, por
intervalo, el ``record_id`` que respalda la decisión: el del acta de alcance para el montaje y el
``agreement_id`` del acuerdo de uso para el uso (``interfaces-para-u04-u05.md`` §1.2). La tabla de
``gob_0017`` solo guardaba ``ledger_record_id`` (el registro ``gate_state_changed``), y leerlo del
contenido del expediente ataría la historia, que no caduca, a particiones que se archivan.

- ``record_id uuid``: nulo solo en ``pending``, que nunca se escribe (la ausencia de intervalo se
  lee como ``pending``). En ``revoked`` es el del acta o el acuerdo cuya aprobación se revocó.
- No entra en la lista blanca del disparador (``catalog.guard_update``): la guarda compara la fila
  entera salvo los cierres, así que la columna nueva tampoco cambia nunca.
- ``vigia_app`` ya tiene ``INSERT`` y ``SELECT`` de tabla, que alcanzan la columna nueva.

La imagen anterior no escribe en esta tabla: arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0019"
down_revision: str | None = "gob_0018"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    "ALTER TABLE catalog.gate_state_history ADD COLUMN record_id uuid",
    """
    ALTER TABLE catalog.gate_state_history
        ADD CONSTRAINT gate_state_history_record_id_when_decided
        CHECK (status = 'pending' OR record_id IS NOT NULL)
    """,
    "COMMENT ON COLUMN catalog.gate_state_history.record_id IS"
    " 'Acta de alcance (montaje) o acuerdo de uso (uso) que respalda el intervalo'",
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
