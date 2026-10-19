"""Arrendamiento del reclamo de ``monthly_package`` (TASK-342 a 344; VIG-239 rework 1;
PAT-LAZ-RES-05; BR-LAZ-108).

Revisión laz_0035, aditiva: una tabla nueva.

``accreditation.monthly_package_lease`` serializa la composición de un ``(zone_id, period)``:
solo quien tiene el arrendamiento vigente sube el zip y sella. Sin esto, dos composiciones del
mismo periodo reclaman la misma fila de ``monthly_package`` (``claim`` no excluye un intento en
curso, solo uno ya sellado) y ambas suben a la misma clave de ``vigia-evidence`` sin condición:
si el perdedor sube después del sellado del ganador, el objeto deja de coincidir con
``package_sha256`` de ``monthly_package_produced`` (P4/P5, BR-LAZ-108). El mismo mecanismo que
``accreditation.export_request`` (``laz_0031``, PAT-LAZ-RES-01) sobre una tabla propia, porque
``monthly_package`` no tiene columnas de arrendamiento y la migración de TASK-338 ya cerró su
forma.

``lease_owner``/``lease_until`` nulos a la vez (sin arrendamiento vigente); vencido
(``lease_until < ahora``) o liberado, cualquiera puede reclamarlo de nuevo. ``attempt_count`` sube
en cada adquisición. Seguridad a nivel de fila forzada por organización y por concesión de
proveedor por planta, como el resto de ``accreditation`` (A-46/A-69).
"""

from __future__ import annotations

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0035"
down_revision: str | None = "laz_0034"
branch_labels: None = None
depends_on: None = None

SCHEMA = "accreditation"
TABLE = "monthly_package_lease"

_STATEMENTS = (
    f"""
    CREATE TABLE {SCHEMA}.{TABLE} (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        period date NOT NULL CONSTRAINT monthly_package_lease_month
            CHECK (extract(day FROM period) = 1),
        lease_owner text
            CONSTRAINT monthly_package_lease_owner
                CHECK (char_length(lease_owner) BETWEEN 1 AND 128),
        lease_until timestamptz,
        attempt_count integer NOT NULL DEFAULT 0
            CONSTRAINT monthly_package_lease_attempt_count CHECK (attempt_count >= 0),
        CONSTRAINT monthly_package_lease_pkey PRIMARY KEY (organization_id, zone_id, period),
        CONSTRAINT monthly_package_lease_lease
            CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
    )
    """,
    *row_security(SCHEMA, TABLE, plant_column="plant_id", organization_only=False),
    f"GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO vigia_app",
    f"GRANT UPDATE (lease_owner, lease_until, attempt_count) ON {SCHEMA}.{TABLE} TO vigia_app",
    f"COMMENT ON TABLE {SCHEMA}.{TABLE} IS 'Arrendamiento de la composición de un paquete"
    " mensual por (zone_id, period) (PAT-LAZ-RES-05, VIG-239): solo quien lo tiene vigente"
    " sube el zip y sella; seguridad a nivel de fila forzada por organización y concesión de"
    " proveedor por planta; retención: sin retención propia, vive mientras viva"
    f" monthly_package; {retention_tag('operational_state')}'",
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
