"""Tablas del esquema ``fleet`` en SQLAlchemy Core (TASK-203, LC-GOB-21 parte 2).

Reflejan la migración ``gob_0018_fleet_schema``, que es la fuente de verdad: las restricciones
(listas cerradas, longitudes, claves foráneas hacia ``identity``), las particiones mensuales, la
seguridad a nivel de fila, los disparadores de solo anexar y de la alarma abierta y los permisos
viven allí y no se repiten aquí. Estas definiciones solo dan nombre y tipo a las columnas para los
repositorios de TASK-218 a TASK-226; ``tests/integration/test_gob_0018_fleet_schema.py``
comprueba que coinciden con la base.

Sin repositorios: toda consulta se abre con un ``ScopeContext`` (BR-NUC-02) desde la tarea que
la necesite; la marca global de la lista de revocación, con el del operador. Las marcas de tiempo
no tienen valor por defecto: las pone la aplicación con su ``Clock``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Double,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import TIMESTAMP

__all__ = [
    "CAMERA_INVENTORY",
    "CLIP_UPLOAD_GRANT",
    "ENROLLMENT_ATTEMPT",
    "ENROLLMENT_CODE",
    "FLEET_ALARM",
    "FLEET_ALARM_EVALUATION",
    "HEARTBEAT_HISTORY",
    "METADATA",
    "NODE_CONFIGURATION",
    "NODE_CREDENTIAL",
    "NODE_FLEET_RECORD",
    "NODE_INVENTORY",
    "OBSERVABILITY_ORPHAN_CLOSE",
    "OPEN_FLEET_ALARM",
    "PLANT_FLEET_THRESHOLDS",
    "REVOCATION_LIST_DIRTY",
    "REVOCATION_LIST_PUBLICATION",
    "REVOCATION_LIST_STATE",
    "SCHEMA",
    "TARGET_VERSION_PUBLICATION",
    "UPDATE_RESULT",
    "VERIFICATION_CLIP",
    "ZONE_NODE_STATE",
]

SCHEMA: Final = "fleet"
METADATA: Final = MetaData(schema=SCHEMA)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _instant(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable, primary_key=primary_key)


def _text(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable)


def _jsonb(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, JSONB, nullable=nullable)


def _integer(name: str, *, nullable: bool = False) -> Column[Any]:
    return Column(name, Integer, nullable=nullable)


def _scope() -> tuple[Column[Any], Column[Any]]:
    """``organization_id`` y ``plant_id``: toda tabla de planta de ``fleet`` los lleva."""
    return _uuid("organization_id"), _uuid("plant_id")


NODE_FLEET_RECORD: Final = Table(
    "node_fleet_record",
    METADATA,
    _uuid("node_id", primary_key=True),
    *_scope(),
    _uuid("replaces_node_id", nullable=True),
    _text("hardware_fingerprint", nullable=True),
    _instant("declared_at"),
    _uuid("declared_by"),
    _instant("enrolled_at", nullable=True),
    _instant("revoked_at", nullable=True),
    _text("revocation_reason_es", nullable=True),
    _instant("decommissioned_at", nullable=True),
    _text("live_view_local_url", nullable=True),
)
"""§3.1 ``NodeFleetRecord`` 🔒."""

REVOCATION_LIST_PUBLICATION: Final = Table(
    "revocation_list_publication",
    METADATA,
    Column("singleton", Boolean, primary_key=True),
    Column("crl_number", BigInteger, nullable=False),
    _instant("published_at", nullable=True),
    _text("crl_sha256", nullable=True),
)
"""Nota de §3.1 (D-7): marca única de la lista de revocación global; solo el operador."""

REVOCATION_LIST_STATE: Final = Table(
    "revocation_list_state",
    METADATA,
    Column("singleton", Boolean, primary_key=True),
    Column("dirty_generation", BigInteger, nullable=False),
    _instant("dirty_since", nullable=True),
    Column("published_generation", BigInteger, nullable=False),
    _instant("published_at", nullable=True),
    _text("object_version_id", nullable=True),
    _instant("next_update", nullable=True),
    _integer("entries"),
)
"""``gob_0021`` (TASK-218): marca única y estado de publicación de la lista global, sin RLS."""

REVOCATION_LIST_DIRTY: Final = Table(
    "revocation_list_dirty",
    METADATA,
    _uuid("organization_id", primary_key=True),
    Column("dirty", Boolean, nullable=False),
    _instant("updated_at"),
)
"""Nota de §3.1: marca ``revocation_list_dirty`` por organización, solo como métrica."""

ENROLLMENT_CODE: Final = Table(
    "enrollment_code",
    METADATA,
    _uuid("code_id", primary_key=True),
    *_scope(),
    _uuid("node_id"),
    _text("code_hash"),
    Column("code_salt", LargeBinary, nullable=False),
    _instant("issued_at"),
    _uuid("issued_by"),
    _instant("expires_at"),
    _instant("disclosed_at"),
    _text("status"),
    _uuid("ledger_record_id"),
)
"""§3.2 ``EnrollmentCode`` 🔒; ``status`` solo hacia adelante."""

ENROLLMENT_ATTEMPT: Final = Table(
    "enrollment_attempt",
    METADATA,
    _uuid("attempt_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id", nullable=True),
    _uuid("node_id", nullable=True),
    _text("presented_code_hash"),
    _text("hardware_fingerprint"),
    _text("software_version"),
    _text("contract_version"),
    _text("result"),
    _instant("attempted_at", primary_key=True),
    _text("source_ip_hash"),
    _uuid("correlation_id"),
    _uuid("ledger_record_id", nullable=True),
)
"""§3.3 ``EnrollmentAttempt`` ⛓, particionada por mes de ``attempted_at``."""

NODE_CREDENTIAL: Final = Table(
    "node_credential",
    METADATA,
    _uuid("credential_id", primary_key=True),
    *_scope(),
    _uuid("node_id"),
    _text("certificate_serial"),
    _jsonb("subject"),
    _text("key_algorithm"),
    _instant("issued_at"),
    _instant("expires_at"),
    _text("status"),
    _uuid("rotated_from", nullable=True),
    _instant("revoked_at", nullable=True),
)
"""§3.4 ``NodeCredential`` 🔒; índice único ``(node_id, certificate_serial)``."""

NODE_INVENTORY: Final = Table(
    "node_inventory",
    METADATA,
    _uuid("node_id", primary_key=True),
    *_scope(),
    _text("software_version"),
    _text("contract_version"),
    _text("model_version"),
    _jsonb("contract_notice"),
    _instant("last_heartbeat_at", nullable=True),
    _text("communication_state"),
    _jsonb("local_queue"),
    _jsonb("clock"),
    _jsonb("signal_reader"),
    Column("uptime_seconds", BigInteger, nullable=False),
    _text("target_version", nullable=True),
    _text("last_update_result", nullable=True),
    Column("warnings", ARRAY(Text), nullable=False),
    _instant("updated_at"),
)
"""§3.5 ``NodeInventory`` 🔒."""

CAMERA_INVENTORY: Final = Table(
    "camera_inventory",
    METADATA,
    *_scope(),
    _uuid("node_id", primary_key=True),
    _uuid("camera_id", primary_key=True),
    Column("connected", Boolean, nullable=False),
    Column("measured_fps", Double, nullable=False),
    Column("declared_min_fps", Double, nullable=False),
    _text("observability_state"),
    _instant("updated_at"),
)
"""§3.6 ``CameraInventory`` 🔒."""

ZONE_NODE_STATE: Final = Table(
    "zone_node_state",
    METADATA,
    *_scope(),
    _uuid("node_id", primary_key=True),
    _uuid("zone_id", primary_key=True),
    _text("mode"),
    _text("observability_state"),
    _integer("catalog_version_in_node"),
    _instant("gate_state_valid_until"),
    _integer("open_episodes"),
    Column("coverage_ok", Boolean, nullable=False),
    _instant("updated_at"),
)
"""§3.7 ``ZoneNodeState`` 🔒."""

HEARTBEAT_HISTORY: Final = Table(
    "heartbeat_history",
    METADATA,
    _uuid("heartbeat_id", primary_key=True),
    *_scope(),
    _uuid("node_id"),
    _instant("received_at", primary_key=True),
    _instant("sent_at"),
    _jsonb("payload_summary"),
)
"""§3.8 ``HeartbeatHistory`` ⛓, particionada por mes de ``received_at``."""

FLEET_ALARM: Final = Table(
    "fleet_alarm",
    METADATA,
    _uuid("alarm_id", primary_key=True),
    *_scope(),
    _text("alarm_kind"),
    _uuid("node_id"),
    _uuid("zone_id", nullable=True),
    _instant("raised_at", primary_key=True),
    _instant("cleared_at", nullable=True),
    _uuid("raised_event_id"),
    _uuid("cleared_event_id", nullable=True),
)
"""§3.9 ``FleetAlarm`` ⛓, particionada por mes de ``raised_at``; cierre ``cleared_at``."""

OPEN_FLEET_ALARM: Final = Table(
    "open_fleet_alarm",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("plant_id"),
    Column("alarm_kind", Text, primary_key=True),
    _uuid("node_id", primary_key=True),
    _uuid("alarm_id", nullable=True),
    _instant("raised_at", nullable=True),
)
"""La ranura de la alarma abierta por (clase, nodo); solo la escriben los disparadores."""

FLEET_ALARM_EVALUATION: Final = Table(
    "fleet_alarm_evaluation",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("plant_id"),
    _uuid("node_id", primary_key=True),
    Column("alarm_kind", Text, primary_key=True),
    Column("observed", Boolean, nullable=False),
    _integer("consecutive"),
    _instant("observed_since"),
    _instant("evaluated_at"),
)
"""Evaluaciones consecutivas de la histéresis de ``FleetAlarm`` (``gob_0025``, TASK-225) 🔒."""

PLANT_FLEET_THRESHOLDS: Final = Table(
    "plant_fleet_thresholds",
    METADATA,
    _uuid("plant_id", primary_key=True),
    _uuid("organization_id"),
    _integer("queue_pending_threshold"),
    _integer("queue_age_threshold_minutes"),
    _integer("clock_drift_threshold_ms"),
    _uuid("updated_by"),
    _instant("updated_at"),
)
"""§3.10 ``PlantFleetThresholds`` 🔒."""

TARGET_VERSION_PUBLICATION: Final = Table(
    "target_version_publication",
    METADATA,
    _uuid("publication_id", primary_key=True),
    *_scope(),
    _text("target_version"),
    Column("node_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
    _instant("maintenance_window_from"),
    _instant("maintenance_window_to"),
    _uuid("published_by"),
    _instant("published_at"),
    _uuid("ledger_record_id"),
)
"""§3.11 ``TargetVersionPublication`` ⛓."""

UPDATE_RESULT: Final = Table(
    "update_result",
    METADATA,
    _uuid("update_result_id", primary_key=True),
    *_scope(),
    _uuid("node_id"),
    _text("target_version"),
    _text("result"),
    _instant("reported_at"),
    _uuid("ledger_record_id"),
)
"""§3.12 ``UpdateResult`` ⛓."""

CLIP_UPLOAD_GRANT: Final = Table(
    "clip_upload_grant",
    METADATA,
    _uuid("clip_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _uuid("node_id"),
    _text("purpose"),
    _text("storage_key"),
    _text("content_type"),
    Column("max_size_bytes", BigInteger, nullable=False),
    _jsonb("required_headers"),
    _instant("issued_at"),
    _instant("expires_at"),
    _text("status"),
    _instant("used_at", nullable=True),
    _instant("orphaned_at", nullable=True),
)
"""§3.13 ``ClipUploadGrant`` 🔒; ``status`` solo hacia adelante con sus cierres."""

VERIFICATION_CLIP: Final = Table(
    "verification_clip",
    METADATA,
    _uuid("clip_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _uuid("node_id"),
    _instant("received_at"),
    _text("sha256"),
    _jsonb("blur_check_result", nullable=True),
    _instant("first_served_at", nullable=True),
)
"""Nota de §3.13 ``VerificationClip`` ⛓; cierres ``blur_check_result`` y ``first_served_at``
(``gob_0022``, TASK-222)."""

NODE_CONFIGURATION: Final = Table(
    "node_configuration",
    METADATA,
    _uuid("node_id", primary_key=True),
    *_scope(),
    _jsonb("time_sources"),
    _integer("sent_records_retention_days"),
    _integer("token_max_age_seconds"),
    _integer("heartbeat_interval_seconds"),
    _integer("mute_after_seconds"),
    _integer("grouping_window_ms"),
    _instant("updated_at"),
)
"""Nota de §3.14 ``NodeConfiguration`` 🔒."""

OBSERVABILITY_ORPHAN_CLOSE: Final = Table(
    "observability_orphan_close",
    METADATA,
    _uuid("event_id", primary_key=True),
    *_scope(),
    _uuid("zone_id"),
    _uuid("node_id"),
    _uuid("opened_event_id"),
    _uuid("ledger_record_id"),
    _instant("received_at"),
)
"""Cierre huérfano de un evento de observabilidad ⛓ (``gob_0024``, TASK-221; BL §2.4)."""
