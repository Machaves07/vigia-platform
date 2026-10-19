"""Tablas de apoyo de ``rebuild_loop_projection`` y ``verify_loop_projection`` (TASK-306; LC-LAZ-03
y 04; PAT-LAZ-RES-03).

Revisión laz_0036, tras ``laz_0035`` (``monthly_package_lease``, TASK-342 a 344). Tres tablas
nuevas, ninguna expuesta por la API:

- ``loop.episode_rebuild_shadow`` y ``loop.finding_state_rebuild_shadow``: la tabla sombra de la
  reconstrucción por planta (LC-LAZ-03). La reconstrucción escribe aquí, por lotes, mientras
  ``loop_projection_checkpoint.state = rebuilding``; el intercambio atómico final sustituye las
  filas de la planta en ``loop.episode`` y ``loop.finding_state`` por las de aquí, en una sola
  transacción, y vacía la sombra de esa planta. Mismas columnas que su tabla destino, sin las
  restricciones de integridad que ya impuso el pliegue (más barato de escribir por lotes); no
  llevan clave ajena hacia el expediente ni hacia ``identity.plant``, porque una fila a medio
  calcular durante la reconstrucción no tiene por qué cumplir todavía lo que exige la proyección
  en servicio.
- ``loop.loop_projection_sample_run`` (``PAT-NUC-MAN-04``, como ``ledger.evidence_sample_run``):
  la semilla registrada de la muestra diaria de ``verify_loop_projection``, una fila por
  organización, planta y día, de solo anexar.

Seguridad a nivel de fila igual que ``loop_projection_checkpoint`` (A-69, ``laz_policies``): de
solo organización, con las dos políticas RESTRICTIVE (concesión de proveedor y contexto de
proveedor denegado), porque ninguna de las tres es un dato de cliente, es control interno de la
propia proyección.

**Arrendamiento de la reconstrucción** (PAT-NUC-RES-05, como ``shared.periodic_task``):
``loop_projection_checkpoint`` gana ``rebuild_owner`` y ``rebuild_lease_until``. El reclamo es una
sola actualización condicional (``WHERE rebuild_owner IS NULL OR rebuild_lease_until < ahora``),
atómica por el bloqueo de fila de PostgreSQL: dos reclamos a la vez para la misma planta nunca
ganan los dos (la revisión de ronda 1 encontró que ``pg_try_advisory_xact_lock`` solo exigía la
transacción corta del reclamo y no cubría el cómputo ni el intercambio). Un arrendamiento vencido
(el proceso murió a medias) se retoma sin bloquear para siempre."""

from __future__ import annotations

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0036"
down_revision: str | None = "laz_0035"
branch_labels: None = None
depends_on: None = None

_HEX64 = "'^[0-9a-f]{64}$'"

_SHADOW_TABLES = ("episode_rebuild_shadow", "finding_state_rebuild_shadow")

_EPISODE_SHADOW = """
CREATE TABLE loop.episode_rebuild_shadow (
    episode_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    plant_id uuid NOT NULL,
    zone_id uuid NOT NULL,
    family text NOT NULL,
    tier text NOT NULL,
    standard_id uuid NOT NULL,
    standard_version integer NOT NULL,
    segment_record_ids uuid[] NOT NULL,
    segment_count integer NOT NULL,
    expected_segment_count integer NOT NULL,
    missing_segment_indexes integer[] NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    total_episode_duration_ms bigint NOT NULL,
    total_condition_duration_ms bigint NOT NULL,
    max_confidence double precision NOT NULL,
    clip_references jsonb NOT NULL,
    observability_state_at_start text NOT NULL,
    continues_open boolean NOT NULL,
    last_segment_received_at timestamptz NOT NULL,
    last_event_id uuid NOT NULL,
    projection_updated_at timestamptz NOT NULL,
    CONSTRAINT episode_rebuild_shadow_pkey PRIMARY KEY (plant_id, episode_id)
)
"""

_FINDING_STATE_SHADOW = f"""
CREATE TABLE loop.finding_state_rebuild_shadow (
    finding_state_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    plant_id uuid NOT NULL,
    zone_id uuid NOT NULL,
    subject_kind text NOT NULL,
    subject_id uuid NOT NULL,
    anchor_record_id uuid NOT NULL,
    family text NOT NULL,
    tier text NOT NULL,
    standard_id uuid NOT NULL,
    standard_version integer NOT NULL,
    lifecycle_state text NOT NULL,
    previous_lifecycle_state text,
    current_classification_outcome text,
    current_reason_category text,
    classification_count integer NOT NULL,
    first_classified_at timestamptz,
    last_classified_at timestamptz,
    classified_within_deadline boolean,
    current_action_id uuid,
    current_action_type text,
    assignee_user_id uuid,
    assignee_inactive boolean NOT NULL,
    due_at timestamptz,
    overdue_since timestamptz,
    overdue_days integer NOT NULL,
    closed_at timestamptz,
    closure_record_id uuid,
    unclosable_since timestamptz,
    unclosable_review_due_at timestamptz,
    unclosable_pending_review boolean NOT NULL,
    closed_with_later_different_classification boolean NOT NULL,
    reopening_count integer NOT NULL,
    has_signed_root_cause boolean NOT NULL,
    observability_state_at_event text NOT NULL,
    zone_gate_status_at_event text NOT NULL,
    zone_agreement_revoked boolean NOT NULL,
    zone_agreement_revoked_at timestamptz,
    single_occupancy_zone boolean NOT NULL,
    aggregation_window_minutes integer NOT NULL,
    walk_test_regression_pending boolean NOT NULL,
    walk_test_regression_cause text,
    occurred_at_exact timestamptz NOT NULL,
    occurred_at_bucket timestamptz NOT NULL,
    age_days integer NOT NULL,
    evidence_flagged boolean NOT NULL,
    missing_segments_declared boolean NOT NULL,
    last_event_id uuid NOT NULL,
    last_record_id uuid NOT NULL,
    projection_updated_at timestamptz NOT NULL,
    projection_digest text NOT NULL
        CONSTRAINT finding_state_rebuild_shadow_digest_format CHECK (projection_digest ~ {_HEX64}),
    CONSTRAINT finding_state_rebuild_shadow_pkey PRIMARY KEY (plant_id, finding_state_id)
)
"""

_CHECKPOINT_LEASE_COLUMNS = """
ALTER TABLE loop.loop_projection_checkpoint
    ADD COLUMN rebuild_owner uuid,
    ADD COLUMN rebuild_lease_until timestamptz,
    ADD CONSTRAINT loop_projection_checkpoint_lease_pair
        CHECK ((rebuild_owner IS NULL) = (rebuild_lease_until IS NULL))
"""

_SAMPLE_RUN = f"""
CREATE TABLE loop.loop_projection_sample_run (
    organization_id uuid NOT NULL,
    plant_id uuid NOT NULL,
    sample_day date NOT NULL,
    seed text NOT NULL
        CONSTRAINT loop_projection_sample_run_seed CHECK (seed ~ {_HEX64}),
    population integer NOT NULL
        CONSTRAINT loop_projection_sample_run_population CHECK (population >= 0),
    sample_size integer NOT NULL
        CONSTRAINT loop_projection_sample_run_sample_size
            CHECK (sample_size >= 0 AND sample_size <= population),
    started_at timestamptz NOT NULL,
    CONSTRAINT loop_projection_sample_run_pkey PRIMARY KEY (organization_id, plant_id, sample_day)
)
"""

_COMMENTS = (
    f"""
    COMMENT ON TABLE loop.episode_rebuild_shadow IS
        'Tabla sombra de rebuild_loop_projection para loop.episode (LC-LAZ-03); '
        '{retention_tag("operational_state")}'
    """,
    f"""
    COMMENT ON TABLE loop.finding_state_rebuild_shadow IS
        'Tabla sombra de rebuild_loop_projection para loop.finding_state (LC-LAZ-03); '
        '{retention_tag("operational_state")}'
    """,
    f"""
    COMMENT ON TABLE loop.loop_projection_sample_run IS
        'Semilla registrada de verify_loop_projection (PAT-NUC-MAN-04); solo anexar; '
        '{retention_tag("operational_state")}'
    """,
)

_APPEND_ONLY_SAMPLE_RUN = (
    """
    CREATE TRIGGER loop_projection_sample_run_append_only_row
        BEFORE UPDATE OR DELETE ON loop.loop_projection_sample_run
        FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()
    """,
    """
    CREATE TRIGGER loop_projection_sample_run_append_only_truncate
        BEFORE TRUNCATE ON loop.loop_projection_sample_run
        FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()
    """,
    "ALTER TABLE loop.loop_projection_sample_run ENABLE ALWAYS TRIGGER "
    "loop_projection_sample_run_append_only_row",
    "ALTER TABLE loop.loop_projection_sample_run ENABLE ALWAYS TRIGGER "
    "loop_projection_sample_run_append_only_truncate",
)


def _grants() -> list[str]:
    statements = []
    for table in (*_SHADOW_TABLES, "loop_projection_sample_run"):
        statements.append(f"GRANT SELECT, INSERT, UPDATE, DELETE ON loop.{table} TO vigia_app")
    statements.append(
        "GRANT UPDATE (rebuild_owner, rebuild_lease_until)"
        " ON loop.loop_projection_checkpoint TO vigia_app"
    )
    return statements


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    statements = [
        _EPISODE_SHADOW,
        _FINDING_STATE_SHADOW,
        _CHECKPOINT_LEASE_COLUMNS,
        _SAMPLE_RUN,
        *_COMMENTS,
    ]
    for table in _SHADOW_TABLES:
        statements += row_security("loop", table, plant_column="plant_id", organization_only=True)
    statements += row_security(
        "loop", "loop_projection_sample_run", plant_column="plant_id", organization_only=True
    )
    statements += _APPEND_ONLY_SAMPLE_RUN
    statements += _grants()
    for statement in statements:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
