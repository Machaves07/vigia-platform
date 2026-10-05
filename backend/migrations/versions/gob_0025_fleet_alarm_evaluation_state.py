"""Estado de la histéresis de las alarmas de flota (TASK-225, LC-GOB-16; NFR-GOB-45, 47).

Revisión gob_0025, aditiva. Ninguna entidad del diseño guarda las evaluaciones consecutivas que la
histéresis necesita (nota de TASK-225: la tabla es decisión del redactor); ``gob_0018`` no dejó
lugar para ellas.

``fleet.fleet_alarm_evaluation`` 🔒: una fila por (organización, nodo, clase) de las cuatro clases
cuya decisión depende de evaluaciones anteriores (``alarm_hysteresis.STATEFUL_KINDS``): las tres
con dos evaluaciones consecutivas (``queue_over_threshold``, ``clock_drift`` y
``camera_below_min_fps``) y ``orphan_clips_growing`` (24 h sostenidas). Sin texto libre:

- ``observed`` y ``consecutive`` (1 o 2: más no cambia ninguna decisión), con ``observed_since``,
  la primera evaluación de la racha (el ``since`` del evento);
- ``evaluated_at``, la evaluación que la escribió: ``evaluate_fleet_alarms`` solo la actualiza si
  la anterior tiene al menos medio ciclo (``UPDATE`` condicional en ``INSERT … ON CONFLICT``), así
  que un ciclo solapado con otro no cuenta dos veces la misma evaluación (NFR-GOB-08, 47).

La clave empieza por la organización (el ``ON CONFLICT`` de la evaluación) y el índice de alcance
por ``(organization_id, plant_id)``, como toda tabla de planta de ``fleet``. ``FORCE ROW LEVEL
SECURITY`` con ``organization_isolation`` y la RESTRICTIVE ``provider_concession_scope`` de
``gob_0018``. ``vigia_app``: ``SELECT``, ``INSERT`` y ``UPDATE`` de las cuatro columnas de la
evaluación; nunca ``DELETE`` ni ``TRUNCATE`` (la fila de un nodo retirado se queda como estaba).

La imagen anterior no lee ni escribe esta tabla: arranca igual sobre este esquema (NFR-NUC-14).
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0025"
down_revision: str | None = "gob_0024"
branch_labels: None = None
depends_on: None = None

TABLE: Final = "fleet_alarm_evaluation"
APP_UPDATABLE_COLUMNS: Final = ("observed", "consecutive", "observed_since", "evaluated_at")
"""Columnas que ``vigia_app`` puede actualizar (la clave, la organización y la planta, nunca)."""
STATEFUL_KINDS: Final = (
    "queue_over_threshold",
    "clock_drift",
    "camera_below_min_fps",
    "orphan_clips_growing",
)
"""``alarm_hysteresis.STATEFUL_KINDS``: las clases con fila de estado."""

_KINDS = ", ".join(f"'{kind}'" for kind in STATEFUL_KINDS)

_STATEMENTS = (
    f"""
    CREATE TABLE fleet.fleet_alarm_evaluation (
        organization_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        node_id uuid NOT NULL,
        alarm_kind text NOT NULL
            CONSTRAINT fleet_alarm_evaluation_kind_values CHECK (alarm_kind IN ({_KINDS})),
        observed boolean NOT NULL,
        consecutive integer NOT NULL
            CONSTRAINT fleet_alarm_evaluation_consecutive CHECK (consecutive BETWEEN 1 AND 2),
        observed_since timestamptz NOT NULL,
        evaluated_at timestamptz NOT NULL,
        CONSTRAINT fleet_alarm_evaluation_pkey PRIMARY KEY (organization_id, node_id, alarm_kind),
        CONSTRAINT fleet_alarm_evaluation_node_fkey FOREIGN KEY (organization_id, plant_id, node_id)
            REFERENCES identity.node_identity (organization_id, plant_id, node_id),
        CONSTRAINT fleet_alarm_evaluation_since_not_after
            CHECK (observed_since <= evaluated_at)
    )
    """,
    """
    CREATE INDEX fleet_alarm_evaluation_scope
        ON fleet.fleet_alarm_evaluation (organization_id, plant_id, node_id, alarm_kind)
    """,
    "ALTER TABLE fleet.fleet_alarm_evaluation ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE fleet.fleet_alarm_evaluation FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY organization_isolation ON fleet.fleet_alarm_evaluation
        AS PERMISSIVE FOR ALL TO PUBLIC
        USING (organization_id = shared.vigia_current_organization())
        WITH CHECK (organization_id = shared.vigia_current_organization())
    """,
    """
    CREATE POLICY provider_concession_scope ON fleet.fleet_alarm_evaluation
        AS RESTRICTIVE FOR ALL TO PUBLIC
        USING (identity.rls_provider_scope_allows(organization_id, plant_id))
        WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
    """,
    "GRANT SELECT, INSERT ON fleet.fleet_alarm_evaluation TO vigia_app",
    "GRANT UPDATE (observed, consecutive, observed_since, evaluated_at)"
    " ON fleet.fleet_alarm_evaluation TO vigia_app",
    "COMMENT ON TABLE fleet.fleet_alarm_evaluation IS 'Evaluaciones consecutivas de la histéresis"
    " de FleetAlarm por (organización, nodo, clase) (TASK-225, NFR-GOB-45) 🔒; seguridad a nivel"
    " de fila forzada por organización y concesión de proveedor'",
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
