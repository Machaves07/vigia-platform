"""Marca de ``adoption_low`` por racha de silencio (TASK-323; LC-LAZ-29; PAT-LAZ-RES-05; PR-LAZ-46).

Revisión laz_0038, aditiva: una tabla nueva.

``accreditation.adoption_alert`` guarda una fila por **racha de silencio** de una zona activa que
llegó a ``adoption_silence_days`` y por la que ``adoption_check`` ya publicó ``adoption_low``. Su
clave primaria ``(organization_id, zone_id, streak_start)`` es la clave de idempotencia de la tarea
(PAT-LAZ-RES-05, «``(zone_id, día)``: no emite dos veces ``adoption_low`` por la misma racha»):
la tarea inserta con ``ON CONFLICT DO NOTHING`` y publica el evento **solo si insertó**, en la misma
transacción. Dos ejecuciones a la vez se ordenan en la clave única y solo una publica. La bandeja
de salida no deduplica por clave (``event_id`` es un UUID v7 nuevo en cada publicación), por eso la
marca vive aquí y no en el evento.

Sin ninguna columna de usuario (H-33, P3): zona, inicio de la racha, días de silencio, umbral
vigente, día local evaluado y la marca de la publicación (del ``Clock`` de la tarea).
``evaluated_day`` es posterior a ``streak_start``: la racha solo cuenta días completos.

Seguridad a nivel de fila forzada por organización y por concesión de proveedor por planta, como
el resto de ``accreditation`` (A-46/A-69). ``vigia_app``: ``SELECT`` e ``INSERT``; nunca
``UPDATE``, ``DELETE`` ni ``TRUNCATE``: una marca no se rehace. Estado operativo sin caducidad,
como ``adoption_counter`` (su tamaño es despreciable: a lo sumo una fila por zona cada
``adoption_silence_days`` días).
"""

from __future__ import annotations

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0038"
down_revision: str | None = "laz_0037"
branch_labels: None = None
depends_on: None = None

SCHEMA = "accreditation"
TABLE = "adoption_alert"

_STATEMENTS = (
    f"""
    CREATE TABLE {SCHEMA}.{TABLE} (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        streak_start date NOT NULL,
        silent_days integer NOT NULL
            CONSTRAINT adoption_alert_silent_days CHECK (silent_days BETWEEN 1 AND 3660),
        threshold_days integer NOT NULL
            CONSTRAINT adoption_alert_threshold_days CHECK (threshold_days BETWEEN 1 AND 90),
        evaluated_day date NOT NULL,
        emitted_at timestamptz NOT NULL,
        CONSTRAINT adoption_alert_pkey PRIMARY KEY (organization_id, zone_id, streak_start),
        CONSTRAINT adoption_alert_evaluated_after_start CHECK (evaluated_day > streak_start),
        CONSTRAINT adoption_alert_zone_fkey FOREIGN KEY (organization_id, plant_id, zone_id)
            REFERENCES identity.zone (organization_id, plant_id, zone_id)
    )
    """,
    *row_security(SCHEMA, TABLE, plant_column="plant_id", organization_only=False),
    f"GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO vigia_app",
    f"COMMENT ON TABLE {SCHEMA}.{TABLE} IS 'Marca de adoption_low por racha de silencio de una"
    " zona activa (PAT-LAZ-RES-05, TASK-323): una fila por (zone_id, streak_start), insertada en"
    " la misma transacción que el evento; sin ninguna columna de usuario; seguridad a nivel de"
    " fila forzada por organización y concesión de proveedor por planta; retención: sin"
    f" caducidad, como adoption_counter; {retention_tag('operational_state')}'",
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
