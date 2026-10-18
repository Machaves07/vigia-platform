"""Tablas del esquema ``accreditation`` en SQLAlchemy Core (TASK-338, LC-LAZ-02).

Reflejan la migración ``laz_0031_accreditation_schema``, que es la fuente de verdad: las
restricciones (listas cerradas, longitudes, claves foráneas hacia ``identity`` y entre tablas),
la seguridad a nivel de fila, los índices, los disparadores (``checkpoint_record_ids`` y
``generated_at`` fijados en el primer intento, U04-H-08; ``audit_sample_item`` solo anexar) y los
permisos viven allí y no se repiten aquí. Estas definiciones solo dan nombre y tipo a las columnas
para los repositorios de U-04; ``tests/integration/test_laz_accreditation_schema.py`` comprueba
que coinciden con la base.

``ADOPTION_COUNTER`` no tiene ninguna columna de usuario (H-33, P3). Sin ORM y sin repositorios:
toda consulta se abre con un ``ScopeContext`` (BR-NUC-02) desde la tarea que la necesite. Las
marcas de tiempo no tienen valor por defecto: las pone la aplicación con su ``Clock``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import BigInteger, Column, Date, Float, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import TIMESTAMP

__all__ = [
    "ADOPTION_COUNTER",
    "AUDIT_SAMPLE",
    "AUDIT_SAMPLE_ITEM",
    "COMMITMENT",
    "EXPORT_PACKAGE",
    "EXPORT_REQUEST",
    "EXPOSURE_INDICATOR",
    "METADATA",
    "MONTHLY_PACKAGE",
    "PROGRAM_METRIC_SNAPSHOT",
    "SCHEMA",
]

SCHEMA: Final = "accreditation"
METADATA: Final = MetaData(schema=SCHEMA)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _instant(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable)


def _text(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


def _integer(name: str) -> Column[Any]:
    return Column(name, Integer, nullable=False)


def _number(name: str) -> Column[Any]:
    return Column(name, Float, nullable=False)


def _json(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, JSONB, nullable=nullable)


def _uuids(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, ARRAY(UUID(as_uuid=True)), nullable=nullable)


def _date(name: str, *, primary_key: bool = False) -> Column[Any]:
    return Column(name, Date, nullable=False, primary_key=primary_key)


def _zone_scope() -> tuple[Column[Any], ...]:
    return _uuid("organization_id"), _uuid("plant_id"), _uuid("zone_id")


EXPORT_REQUEST: Final = Table(
    "export_request",
    METADATA,
    _uuid("export_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id"),
    _uuids("zone_ids"),
    _instant("period_from"),
    _instant("period_to"),
    _json("requested_by"),
    _uuid("user_id"),
    _instant("requested_at"),
    _text("state"),
    _text("package_form"),
    _integer("progress_percent"),
    _instant("progress_updated_at", nullable=True),
    _text("current_artifact", nullable=True),
    _text("failure_code", nullable=True),
    _instant("started_at", nullable=True),
    _instant("completed_at", nullable=True),
    _instant("expires_at"),
    _uuid("record_id", nullable=True),
    _uuid("correlation_id"),
    _text("lease_owner", nullable=True),
    _instant("lease_until", nullable=True),
    _text("multipart_upload_id", nullable=True),
    _integer("attempt_count"),
    _uuids("checkpoint_record_ids", nullable=True),
    _instant("generated_at", nullable=True),
)
"""§3.1 ``ExportRequest`` con arrendamiento, progreso, subida por partes y lo fijado en el primer
intento (``checkpoint_record_ids`` y ``generated_at``, nulos hasta entonces)."""

EXPORT_PACKAGE: Final = Table(
    "export_package",
    METADATA,
    _uuid("export_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id"),
    _text("document_sha256"),
    _text("archive_sha256"),
    _json("manifest"),
    Column("size_bytes", BigInteger, nullable=False),
    _integer("artifact_count"),
    _uuids("checkpoint_record_ids"),
    _text("verifier_version"),
)

MONTHLY_PACKAGE: Final = Table(
    "monthly_package",
    METADATA,
    _uuid("package_id", primary_key=True),
    *_zone_scope(),
    _date("period"),
    _text("state"),
    _instant("generated_at"),
    _json("findings_summary"),
    _json("recurrence_summary"),
    _uuid("metric_snapshot_id", nullable=True),
    _uuid("exposure_indicator_id", nullable=True),
    _json("coverage_summary"),
    _json("commitments_from_previous_period"),
    _uuid("audit_sample_id", nullable=True),
    _uuid("sealed_checkpoint_record_id", nullable=True),
    _uuid("record_id", nullable=True),
)

AUDIT_SAMPLE: Final = Table(
    "audit_sample",
    METADATA,
    _uuid("sample_id", primary_key=True),
    *_zone_scope(),
    _uuid("package_id"),
    _integer("population_size"),
    _integer("sample_size"),
    Column("seed", BigInteger, nullable=False),
    _json("strata"),
    _instant("selected_at"),
    _uuid("reviewer_user_id"),
    _instant("review_completed_at", nullable=True),
)

AUDIT_SAMPLE_ITEM: Final = Table(
    "audit_sample_item",
    METADATA,
    _uuid("sample_item_id", primary_key=True),
    *_zone_scope(),
    _uuid("sample_id"),
    _uuid("classification_id"),
    _uuid("subject_id"),
    _text("outcome", nullable=True),
    _text("comment", nullable=True),
    _json("reviewed_by", nullable=True),
    _instant("reviewed_at", nullable=True),
    _uuid("record_id", nullable=True),
)
"""⛓ solo anexar: el resultado de la revisión se escribe una sola vez."""

COMMITMENT: Final = Table(
    "commitment",
    METADATA,
    _uuid("commitment_id", primary_key=True),
    *_zone_scope(),
    _uuid("package_id"),
    _text("text"),
    _uuid("responsible_user_id"),
    _date("due_date"),
    _text("state"),
    _json("recorded_by"),
    _instant("recorded_at"),
    _uuid("record_id"),
    _text("closed_note", nullable=True),
    _instant("closed_at", nullable=True),
)

PROGRAM_METRIC_SNAPSHOT: Final = Table(
    "program_metric_snapshot",
    METADATA,
    _uuid("snapshot_id", primary_key=True),
    *_zone_scope(),
    _date("period"),
    _text("granularity"),
    _json("values"),
    _instant("materialized_at"),
    _instant("recomputed_at", nullable=True),
    _text("inputs_digest"),
)

EXPOSURE_INDICATOR: Final = Table(
    "exposure_indicator",
    METADATA,
    _uuid("exposure_id", primary_key=True),
    *_zone_scope(),
    _date("period"),
    _text("status"),
    _number("confirmed_minutes"),
    _number("pending_classification_minutes"),
    _number("observable_coverage_percent"),
    _json("coverage_timeline_ref"),
    _json("hourly_distribution"),
    _text("not_computable_reason_es", nullable=True),
    _text("partial_reason_es", nullable=True),
    _text("usage_notice_es"),
    _instant("materialized_at"),
    _instant("recomputed_at", nullable=True),
    _text("inputs_digest"),
)

ADOPTION_COUNTER: Final = Table(
    "adoption_counter",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("plant_id"),
    _uuid("zone_id", primary_key=True),
    _date("day", primary_key=True),
    _json("activity_counts"),
    _integer("session_count"),
    _instant("last_activity_at", nullable=True),
)
"""Por zona y día, sin ninguna columna de usuario (H-33)."""
