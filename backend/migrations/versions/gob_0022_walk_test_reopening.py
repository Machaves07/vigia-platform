"""Reapertura de la sesión de walk-test (TASK-214, LC-GOB-06; BL §3.3, interfaces §3.3).

Revisión gob_0022, aditiva. ``POST /walk-tests/{session_id}/reopen`` pasa una sesión
``incomplete`` a ``reopened`` con motivo, y el motivo, quién y cuándo «quedan en la sesión y en la
auditoría de U-02» (nota del redactor de TASK-214: no hay tipo de registro para la reapertura).
``gob_0017`` no tenía dónde guardarlos en la sesión:

- ``reopened_at``, ``reopened_by`` y ``reopen_reason_es``: los de la **última** reapertura. Las
  tres juntas o ninguna; el motivo, de 10 a 500 caracteres como el resto de motivos del catálogo.
  Cada reapertura deja además su entrada ``walk_test_reopened`` en la auditoría con su motivo, así
  que una segunda reapertura no borra el rastro de la primera.
- ``vigia_app`` recibe ``UPDATE`` solo de esas tres columnas (``walk_test_session`` es una
  proyección 🔒, no una tabla ⛓).

El índice único parcial «una sesión abierta por zona» (``walk_test_session_one_open_per_zone``)
ya lo creó ``gob_0017``: esta revisión no lo toca.

La imagen anterior no escribe ni lee estas columnas: arranca igual sobre este esquema
(NFR-NUC-14).
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0022"
down_revision: str | None = "gob_0021"
branch_labels: None = None
depends_on: None = None

REOPENING_COLUMNS: Final = ("reopened_at", "reopened_by", "reopen_reason_es")
"""Columnas nuevas de ``walk_test_session`` que ``vigia_app`` puede actualizar."""

_STATEMENTS = (
    """
    ALTER TABLE catalog.walk_test_session
        ADD COLUMN reopened_at timestamptz,
        ADD COLUMN reopened_by uuid REFERENCES identity.user_account (user_id),
        ADD COLUMN reopen_reason_es text
            CONSTRAINT walk_test_session_reopen_reason_length
                CHECK (char_length(reopen_reason_es) BETWEEN 10 AND 500)
    """,
    """
    ALTER TABLE catalog.walk_test_session
        ADD CONSTRAINT walk_test_session_reopening_complete CHECK (
            (reopened_at IS NULL) = (reopened_by IS NULL)
            AND (reopened_at IS NULL) = (reopen_reason_es IS NULL)
            AND (reopened_at IS NULL OR reopened_at >= started_at)
        )
    """,
    "GRANT UPDATE (reopened_at, reopened_by, reopen_reason_es)"
    " ON catalog.walk_test_session TO vigia_app",
    "COMMENT ON COLUMN catalog.walk_test_session.reopen_reason_es IS"
    " 'Motivo de la última reapertura (incomplete → reopened); cada una queda en la auditoría'",
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
