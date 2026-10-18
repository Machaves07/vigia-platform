# ruff: noqa: E501
"""Esquema ``accreditation`` de U-04 (TASK-338).

Las tablas son proyecciones o estado operativo; todas se acotan por organización y
planta con RLS forzada. Las marcas temporales las proporciona la aplicación.
"""

from __future__ import annotations

from typing import Final

from alembic import op

from migrations.laz_policies import retention_tag, row_security

revision: str = "laz_0030"
down_revision: str | None = "laz_0029"
branch_labels = None
depends_on = None

SCHEMA: Final = "accreditation"
TABLES: Final = (
    "export_request",
    "export_package",
    "monthly_package",
    "audit_sample",
    "audit_sample_item",
    "commitment",
    "program_metric_snapshot",
    "exposure_indicator",
    "adoption_counter",
)
RETENTION: Final = {
    "export_request": "operational_state_with_retention",
    "adoption_counter": "operational_state",
    **{
        name: "ledger_projection"
        for name in TABLES
        if name not in {"export_request", "adoption_counter"}
    },
}

_TABLES: Final = (
    """CREATE TABLE accreditation.export_request (
        export_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL,
        zone_ids uuid[] NOT NULL CHECK (cardinality(zone_ids) BETWEEN 1 AND 20),
        period_from timestamptz NOT NULL, period_to timestamptz NOT NULL,
        requested_by jsonb NOT NULL, user_id uuid NOT NULL REFERENCES identity.user_account(user_id),
        requested_at timestamptz NOT NULL, state text NOT NULL DEFAULT 'requested'
            CHECK (state IN ('requested','in_progress','ready','failed','expired')),
        package_form text NOT NULL CHECK (package_form IN ('with_clips','without_clips')),
        progress_percent integer NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
        progress_updated_at timestamptz, current_artifact text CHECK (char_length(current_artifact) <= 128),
        failure_code text, started_at timestamptz, completed_at timestamptz, expires_at timestamptz NOT NULL,
        record_id uuid, correlation_id uuid NOT NULL, lease_owner text, lease_until timestamptz,
        multipart_upload_id text CHECK (char_length(multipart_upload_id) <= 256),
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        checkpoint_record_ids uuid[] NOT NULL DEFAULT '{}', generated_at timestamptz,
        CHECK (period_to >= period_from)
    )""",
    "CREATE INDEX export_request_rate ON accreditation.export_request (organization_id, user_id, requested_at)",
    """CREATE TABLE accreditation.export_package (
        export_id uuid PRIMARY KEY REFERENCES accreditation.export_request(export_id),
        organization_id uuid NOT NULL, plant_id uuid NOT NULL, document_sha256 text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
        archive_sha256 text NOT NULL CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'), manifest jsonb NOT NULL,
        size_bytes bigint NOT NULL CHECK (size_bytes >= 0), artifact_count integer NOT NULL CHECK (artifact_count >= 0),
        checkpoint_record_ids uuid[] NOT NULL, verifier_version text NOT NULL
    )""",
    """CREATE TABLE accreditation.monthly_package (
        package_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        period date NOT NULL, state text NOT NULL CHECK (state IN ('generating','sealed','failed')),
        generated_at timestamptz NOT NULL, findings_summary jsonb NOT NULL, recurrence_summary jsonb NOT NULL,
        metric_snapshot_id uuid, exposure_indicator_id uuid, coverage_summary jsonb NOT NULL,
        commitments_from_previous_period uuid[] NOT NULL DEFAULT '{}', audit_sample_id uuid, sealed_checkpoint_record_id uuid, record_id uuid,
        UNIQUE (organization_id, zone_id, period)
    )""",
    """CREATE TABLE accreditation.audit_sample (
        sample_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        package_id uuid NOT NULL REFERENCES accreditation.monthly_package(package_id), population_size integer NOT NULL CHECK (population_size >= 0),
        sample_size integer NOT NULL CHECK (sample_size BETWEEN 0 AND 30), seed bigint NOT NULL, strata jsonb NOT NULL,
        selected_at timestamptz NOT NULL, reviewer_user_id uuid NOT NULL REFERENCES identity.user_account(user_id), review_completed_at timestamptz
    )""",
    """CREATE TABLE accreditation.audit_sample_item (
        sample_item_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        sample_id uuid NOT NULL REFERENCES accreditation.audit_sample(sample_id), classification_id uuid NOT NULL, subject_id uuid NOT NULL,
        outcome text CHECK (outcome IN ('consistent_criterion','doubtful_criterion','criterion_error')),
        comment text CHECK (char_length(comment) <= 1000), reviewed_by jsonb, reviewed_at timestamptz, record_id uuid
    )""",
    """CREATE TABLE accreditation.commitment (
        commitment_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        package_id uuid NOT NULL REFERENCES accreditation.monthly_package(package_id), text text NOT NULL CHECK (char_length(text) BETWEEN 20 AND 1000),
        responsible_user_id uuid NOT NULL REFERENCES identity.user_account(user_id), due_date date NOT NULL,
        state text NOT NULL CHECK (state IN ('open','done','dropped')), recorded_by jsonb NOT NULL, recorded_at timestamptz NOT NULL,
        record_id uuid NOT NULL, closed_note text CHECK (char_length(closed_note) <= 500), closed_at timestamptz
    )""",
    """CREATE TABLE accreditation.program_metric_snapshot (
        snapshot_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        period date NOT NULL, granularity text NOT NULL CHECK (granularity = 'month'), values jsonb NOT NULL,
        materialized_at timestamptz NOT NULL, recomputed_at timestamptz, inputs_digest text NOT NULL CHECK (inputs_digest ~ '^[0-9a-f]{64}$'),
        UNIQUE (organization_id, zone_id, period)
    )""",
    """CREATE TABLE accreditation.exposure_indicator (
        exposure_id uuid PRIMARY KEY, organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL,
        period date NOT NULL, status text NOT NULL CHECK (status IN ('computed','partial','not_computable')),
        confirmed_minutes double precision NOT NULL CHECK (confirmed_minutes >= 0), pending_classification_minutes double precision NOT NULL CHECK (pending_classification_minutes >= 0),
        observable_coverage_percent double precision NOT NULL CHECK (observable_coverage_percent BETWEEN 0 AND 100),
        coverage_timeline_ref jsonb NOT NULL, hourly_distribution jsonb NOT NULL, not_computable_reason_es text, partial_reason_es text,
        usage_notice_es text NOT NULL, materialized_at timestamptz NOT NULL, recomputed_at timestamptz,
        inputs_digest text NOT NULL CHECK (inputs_digest ~ '^[0-9a-f]{64}$'), UNIQUE (organization_id, zone_id, period)
    )""",
    """CREATE TABLE accreditation.adoption_counter (
        organization_id uuid NOT NULL, plant_id uuid NOT NULL, zone_id uuid NOT NULL, day date NOT NULL,
        activity_counts jsonb NOT NULL, session_count integer NOT NULL CHECK (session_count >= 0), last_activity_at timestamptz,
        PRIMARY KEY (organization_id, zone_id, day)
    )""",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    op.execute("CREATE SCHEMA accreditation AUTHORIZATION vigia_migrate")
    for statement in _TABLES:
        op.execute(statement)
    for table in TABLES:
        for statement in row_security(
            SCHEMA, table, plant_column="plant_id", organization_only=False
        ):
            op.execute(statement)
        op.execute(f"GRANT SELECT, INSERT ON accreditation.{table} TO vigia_app")
        op.execute(
            f"COMMENT ON TABLE accreditation.{table} IS 'Acreditación U-04; seguridad a nivel de fila forzada; concesión de proveedor por planta; {retention_tag(RETENTION[table])}'"
        )
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError("Solo hacia adelante (NFR-NUC-14).")
