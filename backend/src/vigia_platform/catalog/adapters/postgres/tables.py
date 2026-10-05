"""Tablas del esquema ``catalog`` en SQLAlchemy Core (TASK-202, LC-GOB-21 parte 1).

Reflejan la migración ``gob_0017_catalog_schema``, que es la fuente de verdad: las restricciones
(listas cerradas, longitudes, exclusión GiST, claves foráneas hacia ``identity``), la seguridad
a nivel de fila, los disparadores de solo anexar y los permisos viven allí y no se repiten aquí.
Estas definiciones solo dan nombre y tipo a las columnas para los repositorios de M2 y M3;
``tests/integration/test_gob_0017_catalog_schema.py`` comprueba que coinciden con la base.

Sin repositorios: toda consulta se abre con un ``ScopeContext`` (BR-NUC-02) desde la tarea que
la necesite. ``updated_at``, ``issued_at`` y el resto de marcas no tienen valor por defecto: las
pone la aplicación con su ``Clock``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Double,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID
from sqlalchemy.types import TIMESTAMP

__all__ = [
    "AGREEMENT_CONFIRMATION",
    "COMMISSIONING_RECORD",
    "DECLARED_STANDARD_VERSION",
    "DOCUMENT_UPLOAD_GRANT",
    "FAMILY_ADMISSION",
    "GATE_STATE_HISTORY",
    "METADATA",
    "MOUNTING_GATE_RECORD",
    "OCCLUSION_TEST",
    "PLANT_POLICY",
    "PLANT_SIGNATORY_POLICY",
    "SCHEMA",
    "USE_AGREEMENT",
    "WALK_TEST_PASS",
    "WALK_TEST_REGRESSION",
    "WALK_TEST_SESSION",
    "WALK_TEST_STEP",
    "ZONE_CAMERA",
    "ZONE_CATALOG_VERSION",
    "ZONE_GATE_STATE",
]

SCHEMA: Final = "catalog"
METADATA: Final = MetaData(schema=SCHEMA)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _instant(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable)


def _text(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


def _jsonb(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, JSONB, nullable=nullable)


def _integer(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, Integer, nullable=nullable, primary_key=primary_key)


def _scope() -> tuple[Column[Any], Column[Any]]:
    """``organization_id`` y ``plant_id``: toda tabla de ``catalog`` los lleva."""
    return _uuid("organization_id"), _uuid("plant_id")


ZONE_CATALOG_VERSION: Final = Table(
    "zone_catalog_version",
    METADATA,
    *_scope(),
    _uuid("zone_id", primary_key=True),
    _integer("catalog_version", primary_key=True),
    _instant("issued_at"),
    _uuid("issued_by"),
    _text("role_in_use"),
    _text("reason_es"),
    Column("changed_fields", ARRAY(Text), nullable=False),
    _jsonb("payload"),
    _jsonb("envelope"),
    Column("single_occupancy", Boolean, nullable=False),
    _integer("aggregation_window_minutes"),
    _uuid("ledger_record_id"),
    _instant("superseded_at", nullable=True),
)
"""§2.1 ``ZoneCatalogVersion`` ⛓; cierre ``superseded_at``."""

DECLARED_STANDARD_VERSION: Final = Table(
    "declared_standard_version",
    METADATA,
    *_scope(),
    _uuid("zone_id"),
    _uuid("standard_id", primary_key=True),
    _integer("version", primary_key=True),
    _text("family"),
    _text("title_es"),
    _text("declared_text"),
    _jsonb("declared_by"),
    _instant("effective_from"),
    _text("tier_policy"),
    _jsonb("predicate"),
    _integer("catalog_version"),
    _integer("retired_in_catalog_version", nullable=True),
    _text("reason_es"),
)
"""§2.2 ``DeclaredStandardVersion`` ⛓; cierre ``retired_in_catalog_version``."""

ZONE_CAMERA: Final = Table(
    "zone_camera",
    METADATA,
    *_scope(),
    _uuid("zone_id", primary_key=True),
    _uuid("camera_id", primary_key=True),
    _text("role_in_zone"),
    Column("declared_min_fps", Double, nullable=False),
    _text("stream_reference"),
    _instant("updated_at"),
)
"""Nota de §3.14 ``ZoneCamera`` 🔒."""

FAMILY_ADMISSION: Final = Table(
    "family_admission",
    METADATA,
    _uuid("admission_id", primary_key=True),
    *_scope(),
    _text("family"),
    _jsonb("answers"),
    _text("justification_es", nullable=True),
    _text("result"),
    _text("failed_criterion", nullable=True),
    _uuid("evaluated_by"),
    _text("role_in_use"),
    _instant("evaluated_at"),
    _uuid("ledger_record_id"),
)
"""§2.3 ``FamilyAdmission`` ⛓, sin cierres."""

ZONE_GATE_STATE: Final = Table(
    "zone_gate_state",
    METADATA,
    _uuid("zone_id", primary_key=True),
    *_scope(),
    _jsonb("mounting"),
    _jsonb("usage"),
    _text("resulting_mode"),
    _instant("issued_at"),
    _jsonb("envelope"),
    _instant("valid_until"),
)
"""§2.4 ``ZoneGateState`` 🔒, una fila por zona."""

GATE_STATE_HISTORY: Final = Table(
    "gate_state_history",
    METADATA,
    *_scope(),
    _uuid("zone_id", primary_key=True),
    Column("gate", Text, primary_key=True),
    _text("status"),
    Column("effective_from", TIMESTAMP(timezone=True), primary_key=True),
    _instant("effective_until", nullable=True),
    # Generada por la base: un INSERT o UPDATE de SQLAlchemy nunca la escribe.
    Column(
        "effective",
        TSTZRANGE,
        Computed("tstzrange(effective_from, effective_until, '[)')", persisted=True),
        nullable=False,
    ),
    _uuid("decided_by"),
    _text("reason_es", nullable=True),
    _uuid("ledger_record_id"),
    # gob_0020: el acta (montaje) o el acuerdo (uso) del intervalo.
    _uuid("record_id", nullable=True),
)
"""§2.5 ``GateStateHistory`` ⛓; cierre ``effective_until``; exclusión ``gate_state_no_overlap``."""

MOUNTING_GATE_RECORD: Final = Table(
    "mounting_gate_record",
    METADATA,
    _uuid("record_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _text("scope_text_es"),
    _jsonb("cameras"),
    _jsonb("blur_verification"),
    _jsonb("document_ref", nullable=True),
    _uuid("signed_by"),
    _text("role_in_use"),
    Column("plant_policy_loaded_at_signing", Boolean, nullable=False),
    _uuid("ledger_record_id"),
)
"""§2.6 ``MountingGateRecord`` ⛓, sin cierres."""

PLANT_SIGNATORY_POLICY: Final = Table(
    "plant_signatory_policy",
    METADATA,
    _uuid("plant_id", primary_key=True),
    _uuid("organization_id"),
    Column("required_roles", ARRAY(Text), nullable=False),
    _integer("minimum"),
    _text("workers_role"),
    _uuid("updated_by"),
    _instant("updated_at"),
)
"""§2.7 ``PlantSignatoryPolicy`` 🔒, una fila por planta."""

USE_AGREEMENT: Final = Table(
    "use_agreement",
    METADATA,
    _uuid("agreement_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _text("status"),
    _jsonb("signatories"),
    _jsonb("document_ref", nullable=True),
    _uuid("replaces_agreement_id", nullable=True),
    _uuid("created_by"),
    _instant("created_at"),
    _instant("approved_at", nullable=True),
    _uuid("approved_by", nullable=True),
    _uuid("ledger_record_id", nullable=True),
    _instant("superseded_at", nullable=True),
    _instant("revoked_at", nullable=True),
)
"""§2.8 ``UseAgreement`` ⛓; estado solo hacia adelante y cierres de aprobación y fin."""

AGREEMENT_CONFIRMATION: Final = Table(
    "agreement_confirmation",
    METADATA,
    _uuid("agreement_id", primary_key=True),
    _uuid("user_id", primary_key=True),
    *_scope(),
    _text("role_in_use"),
    _instant("confirmed_at"),
    _text("origin"),
)
"""§2.9 ``AgreementConfirmation`` ⛓."""

PLANT_POLICY: Final = Table(
    "plant_policy",
    METADATA,
    _uuid("policy_id", primary_key=True),
    *_scope(),
    _integer("version"),
    _instant("signed_at"),
    _text("signed_by_display_name"),
    _text("legal_opinion_reference"),
    _jsonb("document_ref"),
    _text("criteria_summary_es"),
    _uuid("loaded_by"),
    _instant("loaded_at"),
    _uuid("ledger_record_id"),
)
"""§2.10 ``PlantPolicy`` ⛓."""

DOCUMENT_UPLOAD_GRANT: Final = Table(
    "document_upload_grant",
    METADATA,
    _uuid("document_id", primary_key=True),
    *_scope(),
    _text("kind"),
    _text("content_type"),
    _text("storage_key"),
    _text("sha256"),
    Column("size_bytes", BigInteger, nullable=False),
    _instant("issued_at"),
    _instant("expires_at"),
    _text("status"),
)
"""§3.14 ``DocumentUploadGrant`` 🔒; estado ``issued`` → ``used`` | ``expired``."""

WALK_TEST_SESSION: Final = Table(
    "walk_test_session",
    METADATA,
    _uuid("session_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _uuid("node_id"),
    _integer("catalog_version"),
    _text("kind"),
    _text("status"),
    _integer("passes_per_cell"),
    _jsonb("matrix_rows"),
    _instant("started_at"),
    _instant("last_activity_at"),
    _instant("closed_at", nullable=True),
    _uuid("commissioning_record_id", nullable=True),
    # gob_0023: la última reapertura (incomplete → reopened) con su motivo.
    _instant("reopened_at", nullable=True),
    _uuid("reopened_by", nullable=True),
    _text("reopen_reason_es", nullable=True),
)
"""§2.11 ``WalkTestSession`` 🔒; una sesión abierta por zona."""

WALK_TEST_STEP: Final = Table(
    "walk_test_step",
    METADATA,
    _uuid("step_id", primary_key=True),
    *_scope(),
    _uuid("session_id"),
    _text("step_kind"),
    _uuid("responsible_user_id"),
    _instant("started_at"),
    _instant("ended_at", nullable=True),
    _jsonb("correction", nullable=True),
)
"""§2.12 ``WalkTestStep`` ⛓; cierres ``ended_at`` y ``correction``."""

WALK_TEST_PASS: Final = Table(
    "walk_test_pass",
    METADATA,
    _uuid("pass_id", primary_key=True),
    *_scope(),
    _uuid("session_id"),
    _uuid("row_id"),
    _text("result"),
    _uuid("evidence_ref", nullable=True),
    _uuid("recorded_by"),
    _instant("recorded_at"),
)
"""§2.13 ``WalkTestPass`` ⛓."""

OCCLUSION_TEST: Final = Table(
    "occlusion_test",
    METADATA,
    _uuid("test_id", primary_key=True),
    *_scope(),
    _uuid("session_id"),
    _uuid("camera_id"),
    _instant("started_at"),
    _instant("ended_at"),
    _instant("deadline"),
    _text("verification"),
    Column("correlated_event_ids", ARRAY(UUID(as_uuid=True)), nullable=True),
    _text("declared_reason_es", nullable=True),
    _uuid("recorded_by"),
    _uuid("ledger_record_id", nullable=True),
)
"""§2.14 ``OcclusionTest`` ⛓; ``verification`` de ``pending`` a su resultado, una vez."""

COMMISSIONING_RECORD: Final = Table(
    "commissioning_record",
    METADATA,
    _uuid("commissioning_record_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _uuid("session_id"),
    _integer("catalog_version"),
    _jsonb("matrix_results"),
    _integer("false_negatives_total"),
    Column("false_alarm_rate_observed", Numeric, nullable=False),
    Column("false_alarm_threshold", Numeric, nullable=False),
    _jsonb("false_alarm_acceptance", nullable=True),
    _jsonb("latency"),
    _jsonb("installer_measurements"),
    _jsonb("cameras_measured"),
    _jsonb("occlusion_summary"),
    Column("total_hours", Numeric, nullable=False),
    _jsonb("steps_summary"),
    _jsonb("signatures"),
    _instant("closed_at"),
    _uuid("ledger_record_id"),
)
"""§2.15 ``CommissioningRecord`` ⛓, un acta por sesión."""

WALK_TEST_REGRESSION: Final = Table(
    "walk_test_regression",
    METADATA,
    _uuid("zone_id", primary_key=True),
    *_scope(),
    _text("state"),
    _instant("marked_at", nullable=True),
    _text("cause", nullable=True),
    _integer("catalog_version", nullable=True),
    _text("model_version", nullable=True),
    _jsonb("affected_row_ids", nullable=True),
    _instant("cleared_at", nullable=True),
    _uuid("cleared_by_session_id", nullable=True),
    _uuid("ledger_record_id"),
)
"""§2.16 ``WalkTestRegression`` 🔒, una fila por zona."""
