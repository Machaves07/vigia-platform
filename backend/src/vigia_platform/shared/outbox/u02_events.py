"""Tipos de evento propios de U-02 (domain-entities §4.3, eventos iniciales de U-02).

Catorce eventos, todos publicados por U-02: los trece iniciales y
``evidence_marker_verification_failed`` (pendiente nº 21, adenda A-14; TASK-121). Cada carga
lleva solo identificadores, enumeraciones y marcas (BR-NUC-75): nunca un nombre, un correo, un
motivo ni otro texto libre. La organización, la planta, la partición, la secuencia del registro
y ``correlation_id`` van en el propio evento, no en la carga. Es la versión inicial: quien
publica cada evento (TASK-113, 118, 119, 122, 124, 126, 127, 129) solo puede **ampliarla** con
campos opcionales (``OutboxCatalog.synchronize`` rechaza retirar o estrechar). Los eventos de
U-03 y U-04 los registran esas unidades.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StrictInt, StrictStr
from vigia_contracts.models.common import UUID, Sha256Hex, Timestamp

from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = ["U02_EVENT_TYPES", "register_u02_event_types"]

SnakeCode = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
]
"""Nombre de consumidor o código de error: ``snake_case`` ≤ 64 (``last_error_code``)."""

Sequence64 = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
Attempts = Annotated[StrictInt, Field(ge=1, le=8)]

PlatformKeyId = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
]
"""Identificador de una clave de la plataforma (el de ``ledger.record_types.u02``)."""

Role = Literal[
    "coordinator_sst",
    "line_manager",
    "plant_manager",
    "administrator",
    "provider_installer",
    "copasst",
    "platform_operator",
]
ScopeLevel = Literal["organization", "plant", "zone"]
SigningPurpose = Literal["catalog", "gate", "live_view_token", "key_set", "checkpoint"]
ChainKind = Literal["ledger", "audit"]

SecurityAlertKind = Literal[
    "login_failures_account",
    "login_failures_origin",
    "context_absent_attempt",
    "unknown_token_reported",
    "evidence_marker_mismatch",
    "authorization_denied_repeated",
]
"""Causa de ``security_alert``: BR-NUC-02 y 24, BR-NUC-89, RNF-PRI (muestra de clips) y
NFR-NUC-28 (``authorization_denied`` repetido)."""


# --- identidad ------------------------------------------------------------------------------


class UserInvited(PayloadModel):
    user_id: UUID
    invitation_id: UUID
    invited_by: UUID
    expires_at: Timestamp


class UserActivated(PayloadModel):
    user_id: UUID
    activated_at: Timestamp


class UserDeactivated(PayloadModel):
    user_id: UUID
    deactivated_by: UUID
    deactivated_at: Timestamp


class RoleAssignmentChanged(PayloadModel):
    user_id: UUID
    assignment_id: UUID
    change: Literal["assigned", "removed"]
    role: Role
    scope_level: ScopeLevel
    scope_id: UUID
    changed_by: UUID
    changed_at: Timestamp


# --- concesión del proveedor --------------------------------------------------------------


class ConcessionGranted(PayloadModel):
    concession_id: UUID
    provider_organization_id: UUID
    scope_level: Literal["organization", "plant"]
    scope_id: UUID
    granted_by: UUID
    expires_at: Timestamp


class ConcessionRevoked(PayloadModel):
    concession_id: UUID
    revoked_by: UUID
    revoked_at: Timestamp


class ConcessionExpired(PayloadModel):
    concession_id: UUID
    expired_at: Timestamp


# --- seguridad, integridad y claves -------------------------------------------------------


class SecurityAlert(PayloadModel):
    alert_kind: SecurityAlertKind
    resource_kind: SnakeCode | None = None
    resource_id: UUID | None = None
    occurred_at: Timestamp


class IntegrityCompromised(PayloadModel):
    chain_kind: ChainKind
    first_failed_sequence: Sequence64
    verification_mode: Literal["incremental", "full", "on_demand"]
    detected_at: Timestamp


class CheckpointWritten(PayloadModel):
    chain_kind: ChainKind
    covered_sequence: Sequence64
    covered_hash: Sha256Hex
    taken_at: Timestamp


class KeySetPublished(PayloadModel):
    publication_id: UUID
    signing_key_id: PlatformKeyId
    published_at: Timestamp


class KeyRotationDue(PayloadModel):
    key_id: PlatformKeyId
    purpose: SigningPurpose
    valid_until: Timestamp


class DeadLetterCreated(PayloadModel):
    event_id: UUID
    consumer_name: SnakeCode
    attempts: Attempts
    last_error_code: SnakeCode
    failed_at: Timestamp


# --- evidencias -------------------------------------------------------------------------------

VerificationMethod = Literal["object_metadata", "full_read"]
"""``Evidence.verification_method`` (nota del 2026-09-20 de ``domain-entities.md`` §3.5)."""

MarkerFailureReason = Literal["marker_missing", "marker_unverifiable", "metadata_unreadable"]
"""``failure_reason`` cerrado (A-14): marca ausente, marca no verificable, metadato ilegible."""


class EvidenceMarkerVerificationFailed(PayloadModel):
    """Pendiente nº 21 (A-14): la muestra diaria no encontró la marca en el contenedor."""

    evidence_id: UUID
    record_id: UUID
    organization_id: UUID
    plant_id: UUID
    zone_id: UUID
    node_id: UUID
    clip_id: UUID
    verification_method: VerificationMethod
    container_marker_sampled_at: Timestamp
    failure_reason: MarkerFailureReason


U02_EVENT_TYPES: Final[tuple[EventType, ...]] = tuple(
    EventType(
        event_name=name,
        publisher_unit=ActorUnit.U02,
        payload_model=model,
        description_es=description,
    )
    for name, model, description in (
        ("user_invited", UserInvited, "Se invitó a un usuario; el enlace lo envía solo U-02"),
        ("user_activated", UserActivated, "Un usuario activó su cuenta"),
        ("user_deactivated", UserDeactivated, "Un usuario fue desactivado y sus sesiones cerradas"),
        (
            "role_assignment_changed",
            RoleAssignmentChanged,
            "Se asignó o retiró un rol sobre un alcance",
        ),
        ("concession_granted", ConcessionGranted, "Se concedió acceso temporal al proveedor"),
        ("concession_revoked", ConcessionRevoked, "Se revocó una concesión del proveedor"),
        ("concession_expired", ConcessionExpired, "Venció una concesión del proveedor"),
        ("security_alert", SecurityAlert, "Alerta de seguridad; la observabilidad la eleva"),
        (
            "integrity_compromised",
            IntegrityCompromised,
            "La verificación encontró una cadena rota (máxima severidad)",
        ),
        ("checkpoint_written", CheckpointWritten, "Se escribió un punto de control firmado"),
        ("key_set_published", KeySetPublished, "Se publicó un conjunto de claves firmado"),
        ("key_rotation_due", KeyRotationDue, "Una clave de firma debe rotarse en 45 días"),
        (
            "dead_letter_created",
            DeadLetterCreated,
            "Una entrega agotó sus ocho intentos y pasó a la cola muerta",
        ),
        (
            "evidence_marker_verification_failed",
            EvidenceMarkerVerificationFailed,
            "La muestra diaria no encontró la marca de anonimización en el contenedor de un "
            "clip; el hallazgo se conserva y se declara",
        ),
    )
)


def register_u02_event_types(registry: EventTypeRegistry) -> None:
    """Registra los catorce eventos de U-02 al arrancar."""
    for event_type in U02_EVENT_TYPES:
        registry.register(event_type)
