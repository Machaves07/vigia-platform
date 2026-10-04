"""Eventos de la ingesta y la flota en la bandeja de salida (interfaces §2; domain-entities §6).

Diez de los quince eventos de U-03; los otros cinco (catálogo y compuertas) están en
``catalog.events``. La carga es la **unión** de los campos de ``interfaces-para-u04-u05.md`` §2 y
de ``domain-entities.md`` §6 (nota T-08: manda interfaces):

- ``observability_event_received``: ``event_id`` es el del sobre del evento, común a los quince;
  el identificador que asignó el nodo viaja como ``event_id_node``.
- ``node_enrolled``, ``node_revoked`` y ``node_decommissioned`` llevan ``replaces_node_id?``.
- ``update_result_received.result`` admite ``failed``; ``target_version_published`` no lleva
  resultado (el ``result?`` de la fila compartida de interfaces es del segundo evento).
- ``fleet_alarm_raised`` y ``fleet_alarm_cleared`` llevan ``alarm_id`` y ``since``; el segundo,
  además, ``cleared_at``. Se publican por transición, nunca por evaluación (respuesta 16).

Solo identificadores, listas cerradas, enteros y marcas, hasta 64 KB (BR-NUC-75): la versión de
software es ``ReleaseVersion`` (``SemVer`` en minúsculas), porque el ``SemVer`` del contrato
admite mayúsculas y contaría como texto libre.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StrictInt
from vigia_contracts.models.common import UUID, Timestamp
from vigia_contracts.models.enumerations import (
    EvidenceTier,
    ObservabilityEventPhase,
    ObservabilityState,
    ObservabilitySubjectKind,
    PredicateFamily,
)

from vigia_platform.fleet.domain.enums import FleetAlarmKind, UpdateResult
from vigia_platform.fleet.record_types import MAX_NODE_ZONES, ReleaseVersion
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = ["FLEET_EVENT_TYPES", "ReviewReason", "register_fleet_event_types"]

ReviewReason = Literal["low_confidence", "ambiguous", "no_bounding_box", "other"]
"""Motivo de la detección para revisión (interfaces §2, D-3)."""

ZoneIds = Annotated[tuple[UUID, ...], Field(min_length=0, max_length=MAX_NODE_ZONES)]


class StandardCitation(PayloadModel):
    standard_id: UUID
    version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]


class FindingReceived(PayloadModel):
    zone_id: UUID
    node_id: UUID
    finding_id: UUID
    platform_record_id: UUID
    received_at: Timestamp
    family: PredicateFamily
    tier: EvidenceTier
    standard: StandardCitation


class DetectionForReviewReceived(PayloadModel):
    zone_id: UUID
    node_id: UUID
    detection_id: UUID
    platform_record_id: UUID
    received_at: Timestamp
    review_reason: ReviewReason
    idempotency_key: UUID


class ObservabilityEventReceived(PayloadModel):
    zone_id: UUID
    node_id: UUID
    event_id_node: UUID
    platform_record_id: UUID
    subject_kind: ObservabilitySubjectKind
    state: ObservabilityState
    phase: ObservabilityEventPhase


class NodeLifecycle(PayloadModel):
    """Carga común de ``node_enrolled``, ``node_revoked`` y ``node_decommissioned``."""

    node_id: UUID
    plant_id: UUID
    zone_ids: ZoneIds
    replaces_node_id: UUID | None = None


class TargetVersionPublished(PayloadModel):
    node_id: UUID
    target_version: ReleaseVersion


class UpdateResultReceived(PayloadModel):
    node_id: UUID
    target_version: ReleaseVersion
    result: UpdateResult


class FleetAlarmRaised(PayloadModel):
    alarm_id: UUID
    alarm_kind: FleetAlarmKind
    node_id: UUID | None = None
    zone_id: UUID | None = None
    since: Timestamp


class FleetAlarmCleared(PayloadModel):
    alarm_id: UUID
    alarm_kind: FleetAlarmKind
    node_id: UUID | None = None
    zone_id: UUID | None = None
    since: Timestamp
    cleared_at: Timestamp


FLEET_EVENT_TYPES: Final[tuple[EventType, ...]] = tuple(
    EventType(
        event_name=name,
        publisher_unit=ActorUnit.U03,
        payload_model=model,
        description_es=description,
    )
    for name, model, description in (
        ("finding_received", FindingReceived, "La ingesta aceptó un hallazgo"),
        (
            "detection_for_review_received",
            DetectionForReviewReceived,
            "La ingesta aceptó una detección para revisión; va a la cola de revisión",
        ),
        (
            "observability_event_received",
            ObservabilityEventReceived,
            "La ingesta aceptó un evento de observabilidad del nodo",
        ),
        ("node_enrolled", NodeLifecycle, "Se aceptó el alta de un nodo"),
        ("node_revoked", NodeLifecycle, "Se revocó un nodo; deja de poder presentar registros"),
        ("node_decommissioned", NodeLifecycle, "Se dio de baja un nodo ya revocado"),
        (
            "target_version_published",
            TargetVersionPublished,
            "Se publicó una versión objetivo de software para un nodo",
        ),
        (
            "update_result_received",
            UpdateResultReceived,
            "El nodo reportó el resultado de su actualización",
        ),
        (
            "fleet_alarm_raised",
            FleetAlarmRaised,
            "Una condición de alarma de flota entró en vigor",
        ),
        (
            "fleet_alarm_cleared",
            FleetAlarmCleared,
            "Una condición de alarma de flota salió de vigor",
        ),
    )
)
"""Los diez eventos de la ingesta y la flota."""


def register_fleet_event_types(registry: EventTypeRegistry) -> None:
    """Registra los diez eventos de la flota al arrancar."""
    for event_type in FLEET_EVENT_TYPES:
        registry.register(event_type)
