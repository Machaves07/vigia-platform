"""Cierre huérfano de un evento de observabilidad (TASK-221, LC-GOB-12; BL §2.4, BR-CTR-11).

Revisión gob_0024, aditiva. «Un cierre de evento sin apertura se acepta y queda marcado como cierre
huérfano», y el diseño no dice dónde. TASK-204 no declaró ningún campo para ello en el registro
``observability_event_received`` (su contenido es el modelo del contrato con ``receipt``, que no
cambia), así que la marca vive en una **proyección de** ``fleet`` (nota del redactor de TASK-221):

- ``fleet.observability_orphan_close`` ⛓: una fila por evento ``closed`` aceptado cuyo
  ``opened_event_id`` no estaba aceptado en la organización **en el instante de recepción**. Se
  escribe en la misma transacción que el registro (``projection`` del escritor), con su
  ``ledger_record_id``; que la apertura llegue después no la borra (P4: es un hecho de la
  recepción).
- Organización, planta, zona y nodo referencian ``identity`` como las demás tablas de ``fleet``
  (BR-NUC-08); RLS forzada con ``organization_isolation`` y la política RESTRICTIVE
  ``provider_concession_scope`` de ``gob_0018``.
- Solo anexar: ``UPDATE``, ``DELETE`` y ``TRUNCATE`` rechazados por ``shared.vigia_reject_mutation``
  con ``ENABLE ALWAYS``; ``vigia_app`` solo ``SELECT`` e ``INSERT``.

La imagen anterior no lee ni escribe esta tabla: arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0024"
down_revision: str | None = "gob_0023"
branch_labels: None = None
depends_on: None = None

TABLE: Final = "observability_orphan_close"
"""La proyección ⛓ nueva de ``fleet``."""

_STATEMENTS = (
    f"""
    CREATE TABLE fleet.{TABLE} (
        event_id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        opened_event_id uuid NOT NULL,
        ledger_record_id uuid NOT NULL,
        received_at timestamptz NOT NULL,
        CONSTRAINT observability_orphan_close_node_fkey
            FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT observability_orphan_close_zone_fkey
            FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id),
        CONSTRAINT observability_orphan_close_distinct CHECK (opened_event_id <> event_id)
    )
    """,
    f"""
    CREATE INDEX observability_orphan_close_scope
        ON fleet.{TABLE} (organization_id, plant_id, zone_id, received_at)
    """,
    f"ALTER TABLE fleet.{TABLE} ENABLE ROW LEVEL SECURITY",
    f"ALTER TABLE fleet.{TABLE} FORCE ROW LEVEL SECURITY",
    f"""
    CREATE POLICY organization_isolation ON fleet.{TABLE} AS PERMISSIVE FOR ALL TO PUBLIC
        USING (organization_id = shared.vigia_current_organization())
        WITH CHECK (organization_id = shared.vigia_current_organization())
    """,
    f"""
    CREATE POLICY provider_concession_scope ON fleet.{TABLE} AS RESTRICTIVE FOR ALL TO PUBLIC
        USING (identity.rls_provider_scope_allows(organization_id, plant_id))
        WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
    """,
    f"CREATE TRIGGER append_only_update BEFORE UPDATE ON fleet.{TABLE}"
    " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
    f"CREATE TRIGGER append_only_delete BEFORE DELETE ON fleet.{TABLE}"
    " FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()",
    f"CREATE TRIGGER append_only_no_truncate BEFORE TRUNCATE ON fleet.{TABLE}"
    " FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()",
    f"ALTER TABLE fleet.{TABLE} ENABLE ALWAYS TRIGGER append_only_update",
    f"ALTER TABLE fleet.{TABLE} ENABLE ALWAYS TRIGGER append_only_delete",
    f"ALTER TABLE fleet.{TABLE} ENABLE ALWAYS TRIGGER append_only_no_truncate",
    f"GRANT SELECT, INSERT ON fleet.{TABLE} TO vigia_app",
    f"COMMENT ON TABLE fleet.{TABLE} IS"
    " 'Cierre huérfano de un evento de observabilidad aceptado (BL §2.4, BR-CTR-11), ⛓'",
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
