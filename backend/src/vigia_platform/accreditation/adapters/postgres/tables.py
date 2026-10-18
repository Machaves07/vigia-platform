"""Definiciones Core del esquema ``accreditation`` (TASK-338)."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import BigInteger, Column, Date, Float, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import TIMESTAMP

SCHEMA: Final = "accreditation"
METADATA: Final = MetaData(schema=SCHEMA)


def _u(name: str, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _t(name: str, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


def _i(name: str, nullable: bool = False) -> Column[Any]:
    return Column(name, Integer, nullable=nullable)


def _at(name: str, nullable: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable)


def _scope() -> tuple[Column[Any], ...]:
    return _u("organization_id"), _u("plant_id"), _u("zone_id")


EXPORT_REQUEST = Table(
    "export_request",
    METADATA,
    _u("export_id", primary_key=True),
    _u("organization_id"),
    _u("plant_id"),
    Column("zone_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _at("period_from"),
    _at("period_to"),
    Column("requested_by", JSONB, nullable=False),
    _u("user_id"),
    _at("requested_at"),
    _t("state"),
    _t("package_form"),
    _i("progress_percent"),
    _at("progress_updated_at", True),
    _t("current_artifact", True),
    _t("failure_code", True),
    _at("started_at", True),
    _at("completed_at", True),
    _at("expires_at"),
    _u("record_id", True),
    _u("correlation_id"),
    _t("lease_owner", True),
    _at("lease_until", True),
    _t("multipart_upload_id", True),
    _i("attempt_count"),
    Column("checkpoint_record_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _at("generated_at", True),
)
EXPORT_PACKAGE = Table(
    "export_package",
    METADATA,
    _u("export_id", primary_key=True),
    _u("organization_id"),
    _u("plant_id"),
    _t("document_sha256"),
    _t("archive_sha256"),
    Column("manifest", JSONB, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    _i("artifact_count"),
    Column("checkpoint_record_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _t("verifier_version"),
)
MONTHLY_PACKAGE = Table(
    "monthly_package",
    METADATA,
    _u("package_id", primary_key=True),
    *_scope(),
    Column("period", Date, nullable=False),
    _t("state"),
    _at("generated_at"),
    Column("findings_summary", JSONB, nullable=False),
    Column("recurrence_summary", JSONB, nullable=False),
    _u("metric_snapshot_id", True),
    _u("exposure_indicator_id", True),
    Column("coverage_summary", JSONB, nullable=False),
    Column("commitments_from_previous_period", ARRAY(UUID(as_uuid=True)), nullable=False),
    _u("audit_sample_id", True),
    _u("sealed_checkpoint_record_id", True),
    _u("record_id", True),
)
AUDIT_SAMPLE = Table(
    "audit_sample",
    METADATA,
    _u("sample_id", primary_key=True),
    *_scope(),
    _u("package_id"),
    _i("population_size"),
    _i("sample_size"),
    Column("seed", BigInteger, nullable=False),
    Column("strata", JSONB, nullable=False),
    _at("selected_at"),
    _u("reviewer_user_id"),
    _at("review_completed_at", True),
)
AUDIT_SAMPLE_ITEM = Table(
    "audit_sample_item",
    METADATA,
    _u("sample_item_id", primary_key=True),
    *_scope(),
    _u("sample_id"),
    _u("classification_id"),
    _u("subject_id"),
    _t("outcome", True),
    _t("comment", True),
    Column("reviewed_by", JSONB, nullable=True),
    _at("reviewed_at", True),
    _u("record_id", True),
)
COMMITMENT = Table(
    "commitment",
    METADATA,
    _u("commitment_id", primary_key=True),
    *_scope(),
    _u("package_id"),
    _t("text"),
    _u("responsible_user_id"),
    Column("due_date", Date, nullable=False),
    _t("state"),
    Column("recorded_by", JSONB, nullable=False),
    _at("recorded_at"),
    _u("record_id"),
    _t("closed_note", True),
    _at("closed_at", True),
)
PROGRAM_METRIC_SNAPSHOT = Table(
    "program_metric_snapshot",
    METADATA,
    _u("snapshot_id", primary_key=True),
    *_scope(),
    Column("period", Date, nullable=False),
    _t("granularity"),
    Column("values", JSONB, nullable=False),
    _at("materialized_at"),
    _at("recomputed_at", True),
    _t("inputs_digest"),
)
EXPOSURE_INDICATOR = Table(
    "exposure_indicator",
    METADATA,
    _u("exposure_id", primary_key=True),
    *_scope(),
    Column("period", Date, nullable=False),
    _t("status"),
    Column("confirmed_minutes", Float, nullable=False),
    Column("pending_classification_minutes", Float, nullable=False),
    Column("observable_coverage_percent", Float, nullable=False),
    Column("coverage_timeline_ref", JSONB, nullable=False),
    Column("hourly_distribution", JSONB, nullable=False),
    _t("not_computable_reason_es", True),
    _t("partial_reason_es", True),
    _t("usage_notice_es"),
    _at("materialized_at"),
    _at("recomputed_at", True),
    _t("inputs_digest"),
)
ADOPTION_COUNTER = Table(
    "adoption_counter",
    METADATA,
    _u("organization_id", primary_key=True),
    _u("plant_id"),
    _u("zone_id", primary_key=True),
    Column("day", Date, nullable=False, primary_key=True),
    Column("activity_counts", JSONB, nullable=False),
    _i("session_count"),
    _at("last_activity_at", True),
)
