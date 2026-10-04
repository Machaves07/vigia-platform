"""Tipos de registro de la flota y la ingesta (domain-entities.md de U-03, §5).

Trece de los veintiséis tipos de U-03; los otros trece (catálogo, compuertas y comisionamiento)
están en ``catalog.record_types``. Todos los escribe U-03, en la cadena de **planta** y en su
versión 1. Retirar un tipo o una versión está prohibido (BR-NUC-52).

- **Ingesta** (``finding_received``, ``detection_for_review_received``,
  ``observability_event_received``): el contenido es el modelo del contrato **con** ``receipt``
  (``vigia_contracts.models``), sin redefinirlo. Sus únicas evidencias son los clips
  (``/cameras[*]/clips[*]`` y la evidencia opcional del evento, ``/evidence[*]``). El esquema del
  contrato no tiene texto libre, pero las versiones (``SemVer``), la fuente de reloj y la clave de
  almacenamiento mezclan mayúsculas y minúsculas y el registro las cuenta como texto libre
  (``schema_rules.is_free_text``): se declaran y pasan la política, sin cambiar nada del contrato.
- **Huella de hardware**: se conserva tal como la presenta el nodo solo en ``node_enrolled`` y
  ``enrollment_attempt_rejected`` (NFR-GOB-37); el origen del intento, solo como hash.
- **``ingest_rejected``**: solo identificadores y código, nunca el contenido rechazado (BR-GOB-96);
  solo ``zone_gate_not_approved`` y ``node_zone_mismatch`` lo producen (respuesta 17).
- **``node_enrolled``**: la clave es ``node_id`` + ``credential_id`` (A-55), para que la re-alta del
  mismo nodo (U03-H-04) no choque como ``idempotency_conflict``. El escritor admite claves de
  hasta 64 caracteres, así que va como los 32 hexadecimales de cada UUID, sin guiones.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, Field, StrictStr, model_validator
from vigia_contracts.models.common import UUID, SemVer, Sha256Hex, Timestamp, UUIDv7
from vigia_contracts.models.detection_for_review import DetectionForReview
from vigia_contracts.models.enumerations import RecordKind
from vigia_contracts.models.finding import Finding
from vigia_contracts.models.observability_event import ObservabilityEvent

from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult, UpdateResult
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.shared.context import ActorUnit

__all__ = [
    "FLEET_RECORD_TYPES",
    "MAX_NODE_ZONES",
    "MAX_TARGET_NODES",
    "ReleaseVersion",
    "enrollment_source_key",
    "register_fleet_record_types",
]

MAX_NODE_ZONES: Final = 16
"""Zonas por nodo (``ZoneNodeState``: 1 a 16)."""
MAX_TARGET_NODES: Final = 100
"""Nodos de una publicación de versión objetivo: la flota del piloto (RNF-DES-05)."""

ReasonEs = Annotated[StrictStr, Field(min_length=10, max_length=500)]
"""Motivo (``reason_es``): texto libre de 10 a 500 `[estimación propia]`."""

HardwareFingerprint = Annotated[
    StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
"""Huella de hardware del nodo: 64 hexadecimales en minúsculas (A-6)."""

CertificateSerial = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[0-9a-f]{1,64}$")
]
"""Número de serie del certificado que emite ``vigia-node-ca``, en hexadecimal en minúsculas."""

ReleaseVersion = Annotated[
    StrictStr,
    Field(
        min_length=5,
        max_length=64,
        pattern=(
            r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
            r"(-[0-9a-z][0-9a-z.-]{0,30})?(\+[0-9a-z][0-9a-z.-]{0,30})?$"
        ),
    ),
]
"""Versión de software que publica la plataforma: ``SemVer`` en minúsculas. El ``SemVer`` del
contrato admite mayúsculas y cuenta como texto libre; la versión objetivo la elige la plataforma,
así que se cierra (también es la que viaja en los eventos, que no admiten texto libre)."""

EnrollmentKey = Annotated[StrictStr, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
"""``source_key`` de ``node_enrolled``: ``node_id`` y ``credential_id`` sin guiones, seguidos."""

IngestRejectionCode = Literal["zone_gate_not_approved", "node_zone_mismatch"]
"""Los dos ``rejection_code`` del contrato que dejan ``ingest_rejected`` (respuesta 17)."""

RejectedAttemptResult = Literal[
    EnrollmentAttemptResult.ENROLLMENT_CODE_USED,
    EnrollmentAttemptResult.ENROLLMENT_CODE_EXPIRED,
    EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID,
    EnrollmentAttemptResult.RATE_LIMITED,
]
"""``enrollment_attempt_result`` sin ``accepted``: el tipo solo registra intentos fallidos."""


def enrollment_source_key(node_id: str, credential_id: str) -> str:
    """``source_key`` de ``node_enrolled`` (A-55): los dos UUID en hexadecimal, sin guiones."""
    return uuid.UUID(node_id).hex + uuid.UUID(credential_id).hex


class NodeCommunicationStateChanged(ContentModel):
    """Cambio del estado de comunicación; el latido nunca significa «zona despejada» (P2)."""

    node_id: UUID
    state: CommunicationState
    since: Timestamp
    last_heartbeat_at: Timestamp | None = None


class NodeEnrolled(ContentModel):
    source_key: EnrollmentKey
    node_id: UUID
    credential_id: UUIDv7
    zone_ids: Annotated[tuple[UUID, ...], Field(min_length=0, max_length=MAX_NODE_ZONES)]
    hardware_fingerprint: HardwareFingerprint
    certificate_serial: CertificateSerial
    enrolled_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != enrollment_source_key(self.node_id, self.credential_id):
            raise ValueError("source_key debe ser node_id y credential_id sin guiones (A-55)")
        if len(set(self.zone_ids)) != len(self.zone_ids):
            raise ValueError("zone_ids no admite zonas repetidas")
        return self


class NodeCredentialRotated(ContentModel):
    node_id: UUID
    credential_id: UUIDv7
    rotated_from: UUIDv7
    certificate_serial: CertificateSerial
    issued_at: Timestamp
    expires_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.rotated_from == self.credential_id:
            raise ValueError("una credencial no rota hacia sí misma")
        return self


class NodeRevoked(ContentModel):
    node_id: UUID
    reason_es: ReasonEs
    revoked_at: Timestamp
    revoked_by: UUID


class UpdateResultReceived(ContentModel):
    """Lo que el nodo reporta tras actualizarse, con ``failed`` (nota de §3.12)."""

    update_result_id: UUIDv7
    node_id: UUID
    target_version: SemVer
    result: UpdateResult
    reported_at: Timestamp


class MaintenanceWindow(ContentModel):
    """Ventana de mantenimiento informativa en el piloto (D-5): no viaja al nodo.

    ``starts_at`` y ``ends_at`` son el ``{from, to}`` del diseño: ``from`` es palabra reservada de
    Python y el contenido no admite alias (``registry.alias_problems``).
    """

    starts_at: Timestamp
    ends_at: Timestamp

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("la ventana de mantenimiento termina después de empezar")
        return self


class NodeTargetVersionPublished(ContentModel):
    publication_id: UUIDv7
    target_version: ReleaseVersion
    node_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_TARGET_NODES)]
    maintenance_window: MaintenanceWindow

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("node_ids no admite nodos repetidos")
        return self


class NodeDecommissioned(ContentModel):
    """Baja de un nodo ya revocado: conserva todo lo que envió (D-14, P4)."""

    node_id: UUID
    reason_es: ReasonEs
    decommissioned_at: Timestamp


class EnrollmentCodeIssued(ContentModel):
    """Emisión de un código de alta: **nunca** el código ni su hash (respuesta 14)."""

    code_id: UUIDv7
    node_id: UUID
    issued_at: Timestamp
    expires_at: Timestamp
    disclosed_at: Timestamp
    issued_by: UUID


class EnrollmentAttemptRejected(ContentModel):
    """Intento de alta fallido: hora, huella y origen como hash, nunca el código (BR-CTR-45)."""

    attempt_id: UUIDv7
    node_id: UUID | None = None
    result: RejectedAttemptResult
    hardware_fingerprint: HardwareFingerprint
    attempted_at: Timestamp
    source_ip_hash: Sha256Hex


class IngestRejected(ContentModel):
    """Rechazo permanente de la ingesta: identificadores y código, nunca el contenido."""

    node_id: UUID
    zone_id: UUID | None = None
    record_kind: RecordKind
    code: IngestRejectionCode
    correlation_id: UUID
    received_at: Timestamp


def _fleet(
    record_type: str,
    model: type[BaseModel],
    *,
    source_key_path: str | None = None,
    free_text_paths: tuple[str, ...] = (),
    evidence_paths: tuple[str, ...] = (),
    outbox_events: tuple[str, ...] = (),
) -> RecordType:
    return RecordType(
        record_type=record_type,
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=model,
        source_key_path=source_key_path,
        free_text_paths=free_text_paths,
        evidence_paths=evidence_paths,
        outbox_events=outbox_events,
    )


CONTRACT_FREE_TEXT_PATHS: Final = (
    "/contract_version",
    "/software_version",
    "/node_time/clock/source",
)
"""Rutas de los tres registros del contrato que el registro cuenta como texto libre."""

CLIP_FREE_TEXT_PATH: Final = "/storage_key"
"""Ruta, dentro de cada clip, de su clave de almacenamiento (texto libre para el registro)."""

FLEET_RECORD_TYPES: Final[tuple[RecordType, ...]] = (
    _fleet(
        "finding_received",
        Finding,
        source_key_path="/finding_id",
        free_text_paths=(*CONTRACT_FREE_TEXT_PATHS, "/cameras[*]/clips[*]" + CLIP_FREE_TEXT_PATH),
        evidence_paths=("/cameras[*]/clips[*]",),
        outbox_events=("finding_received",),
    ),
    _fleet(
        "detection_for_review_received",
        DetectionForReview,
        source_key_path="/detection_id",
        free_text_paths=(*CONTRACT_FREE_TEXT_PATHS, "/cameras[*]/clips[*]" + CLIP_FREE_TEXT_PATH),
        evidence_paths=("/cameras[*]/clips[*]",),
        outbox_events=("detection_for_review_received",),
    ),
    _fleet(
        "observability_event_received",
        ObservabilityEvent,
        source_key_path="/event_id",
        free_text_paths=(*CONTRACT_FREE_TEXT_PATHS, "/evidence[*]" + CLIP_FREE_TEXT_PATH),
        evidence_paths=("/evidence[*]",),
        outbox_events=("observability_event_received",),
    ),
    _fleet("node_communication_state_changed", NodeCommunicationStateChanged),
    _fleet(
        "node_enrolled",
        NodeEnrolled,
        source_key_path="/source_key",
        outbox_events=("node_enrolled",),
    ),
    _fleet("node_credential_rotated", NodeCredentialRotated, source_key_path="/credential_id"),
    _fleet(
        "node_revoked",
        NodeRevoked,
        free_text_paths=("/reason_es",),
        outbox_events=("node_revoked",),
    ),
    _fleet(
        "update_result_received",
        UpdateResultReceived,
        source_key_path="/update_result_id",
        free_text_paths=("/target_version",),
        outbox_events=("update_result_received",),
    ),
    _fleet(
        "node_target_version_published",
        NodeTargetVersionPublished,
        source_key_path="/publication_id",
        outbox_events=("target_version_published",),
    ),
    _fleet(
        "node_decommissioned",
        NodeDecommissioned,
        source_key_path="/node_id",
        free_text_paths=("/reason_es",),
        outbox_events=("node_decommissioned",),
    ),
    _fleet("enrollment_code_issued", EnrollmentCodeIssued, source_key_path="/code_id"),
    _fleet("enrollment_attempt_rejected", EnrollmentAttemptRejected, source_key_path="/attempt_id"),
    _fleet("ingest_rejected", IngestRejected),
)
"""Los trece tipos de la flota y la ingesta, en versión 1 (domain-entities §5)."""


def register_fleet_record_types(registry: RecordTypeRegistry) -> None:
    """Registra los trece tipos de la flota; ``RecordTypeRejected`` si alguno no cumple."""
    for definition in FLEET_RECORD_TYPES:
        registry.register(definition)
