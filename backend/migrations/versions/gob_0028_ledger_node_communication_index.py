"""Índice parcial de las transiciones de comunicación por nodo (TASK-233; seguimiento de VIG-159).

Revisión gob_0028, aditiva: solo un índice.

``GET /fleet/nodes`` (``fleet.adapters.postgres.inventory_queries``) busca, para **cada** nodo de
la página, su último ``node_communication_state_changed`` en ``ledger.ledger_record`` filtrando
por organización, ``scope_zone_id IS NULL``, tipo y ``scope_node_id``. Sin índice que lleve el
nodo, el único que sirve es ``ledger_record_zone_received`` por su prefijo de organización: cada
nodo recorre **todos** los registros sin zona de la organización. La volumetría de U-03 lo midió
con un año de la organización mayor (60 zonas, unas 83 000 transiciones): mediana de 30 s por
página y ``temporarily_unavailable`` por ``statement_timeout`` (objetivo p95 ≤ 500 ms,
NFR-GOB-03).

``ledger_record_node_communication`` es parcial (solo las transiciones de comunicación, sin zona)
y lleva organización y nodo delante, como pide NFR-NUC-08; sobre la tabla particionada se crea en
cada partición y en las que cree después ``shared.vigia_create_month_partitions``. Con él, cada
nodo lee solo sus transiciones.

La imagen anterior no cambia de plan ni de resultado: lee lo mismo, más deprisa (NFR-NUC-14).
``MINIMUM_SCHEMA_VERSION`` no sube: ningún código lo exige.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0028"
down_revision: str | None = "gob_0027"
branch_labels: None = None
depends_on: None = None

_STATEMENTS = (
    """
    CREATE INDEX ledger_record_node_communication
        ON ledger.ledger_record (organization_id, scope_node_id, received_at)
        WHERE record_type = 'node_communication_state_changed' AND scope_zone_id IS NULL
    """,
    "COMMENT ON INDEX ledger.ledger_record_node_communication IS 'Última transición de"
    " comunicación de cada nodo para GET /fleet/nodes (NFR-GOB-03, TASK-233)'",
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
