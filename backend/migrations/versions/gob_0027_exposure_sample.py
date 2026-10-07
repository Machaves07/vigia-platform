"""Muestras de exposición del acta y base de la reejecución por regresión (TASK-216, LC-GOB-08).

Revisión gob_0027, aditiva.

- ``catalog.exposure_sample`` ⛓ (nota de TASK-216: las muestras no figuran entre las entidades de
  ``gob_0017``): una muestra por pase mostrado, ``{pass_id, fetched_at, displayed_at}`` con el
  **reloj del navegador** (tramo 3a de NFR-GOB-70), que U-05 envía por ``POST /walk-tests/{id}/
  exposure-samples``. Solo anexar: ``UPDATE``, ``DELETE`` y ``TRUNCATE`` los rechaza
  ``shared.vigia_reject_mutation`` también para el dueño (``ENABLE ALWAYS``). La clave única
  ``exposure_sample_one_per_pass`` deja una sola muestra por pase; la clave foránea compuesta
  ``(organization_id, plant_id, session_id, pass_id)`` exige que el pase sea **de esa sesión**
  (para eso ``walk_test_pass`` recibe la clave única ``walk_test_pass_session_key``, cuyo índice
  empieza por el alcance). ``displayed_at >= fetched_at``. Sin texto libre.
- ``catalog.walk_test_session.regression_basis_record_id``: en una sesión ``regression_rerun``,
  el ``ledger_record_id`` de la regresión ``pending`` que la abrió (su última marca). El cierre la
  compara con la fila de la regresión: si cambió, otra marca llegó durante la reejecución y la
  regresión sigue ``pending`` (BR-GOB-55). Se escribe al insertar y nunca se actualiza (sin
  ``UPDATE`` para ``vigia_app``); nula en las ``initial``.

``FORCE ROW LEVEL SECURITY`` con ``organization_isolation`` y la RESTRICTIVE
``provider_concession_scope`` de ``gob_0017``. ``vigia_app``: ``SELECT`` e ``INSERT``.

La imagen anterior no lee ni escribe la tabla nueva, inserta sesiones sin la columna nueva (queda
nula) y lee las suyas por nombre de columna: arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0027"
down_revision: str | None = "gob_0026"
branch_labels: None = None
depends_on: None = None

TABLES: Final = ("exposure_sample",)
"""Tablas nuevas de ``catalog``."""
APPEND_ONLY_TABLES: Final = ("exposure_sample",)
"""Tablas ⛓ nuevas (``migrations/append_only.py``)."""

_STATEMENTS = (
    """
    ALTER TABLE catalog.walk_test_pass
        ADD CONSTRAINT walk_test_pass_session_key
        UNIQUE (organization_id, plant_id, session_id, pass_id)
    """,
    """
    CREATE TABLE catalog.exposure_sample (
        sample_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        session_id uuid NOT NULL,
        pass_id uuid NOT NULL,
        fetched_at timestamptz NOT NULL,
        displayed_at timestamptz NOT NULL,
        recorded_by uuid NOT NULL REFERENCES identity.user_account (user_id),
        recorded_at timestamptz NOT NULL,
        CONSTRAINT exposure_sample_pass_fkey
            FOREIGN KEY (organization_id, plant_id, session_id, pass_id)
            REFERENCES catalog.walk_test_pass (organization_id, plant_id, session_id, pass_id),
        CONSTRAINT exposure_sample_one_per_pass UNIQUE (pass_id),
        CONSTRAINT exposure_sample_order CHECK (displayed_at >= fetched_at)
    )
    """,
    """
    CREATE INDEX exposure_sample_scope
        ON catalog.exposure_sample (organization_id, plant_id, session_id)
    """,
    "ALTER TABLE catalog.exposure_sample ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE catalog.exposure_sample FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY organization_isolation ON catalog.exposure_sample
        AS PERMISSIVE FOR ALL TO PUBLIC
        USING (organization_id = shared.vigia_current_organization())
        WITH CHECK (organization_id = shared.vigia_current_organization())
    """,
    """
    CREATE POLICY provider_concession_scope ON catalog.exposure_sample
        AS RESTRICTIVE FOR ALL TO PUBLIC
        USING (identity.rls_provider_scope_allows(organization_id, plant_id))
        WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
    """,
    "CREATE TRIGGER append_only_update BEFORE UPDATE ON catalog.exposure_sample"
    " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
    "CREATE TRIGGER append_only_delete BEFORE DELETE ON catalog.exposure_sample"
    " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
    "CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON catalog.exposure_sample"
    " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
    "ALTER TABLE catalog.exposure_sample ENABLE ALWAYS TRIGGER append_only_update",
    "ALTER TABLE catalog.exposure_sample ENABLE ALWAYS TRIGGER append_only_delete",
    "ALTER TABLE catalog.exposure_sample ENABLE ALWAYS TRIGGER append_only_no_truncate",
    "GRANT SELECT, INSERT ON catalog.exposure_sample TO vigia_app",
    "COMMENT ON TABLE catalog.exposure_sample IS 'ExposureSample (TASK-216, tramo 3a de"
    " NFR-GOB-70) ⛓ solo anexar; seguridad a nivel de fila forzada por organización y concesión"
    " de proveedor'",
    # --- Base de la reejecución ------------------------------------------------------------------
    """
    ALTER TABLE catalog.walk_test_session
        ADD COLUMN regression_basis_record_id uuid,
        ADD CONSTRAINT walk_test_session_regression_basis_only_rerun
            CHECK (regression_basis_record_id IS NULL OR kind = 'regression_rerun')
    """,
    "COMMENT ON COLUMN catalog.walk_test_session.regression_basis_record_id IS"
    " 'Última marca de la regresión cuando se abrió la reejecución (BR-GOB-55); nunca cambia'",
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
