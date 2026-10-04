"""Eventos del catálogo y las compuertas en la bandeja (interfaces §2; domain-entities §6).

Cinco de los quince eventos de U-03; los otros diez (ingesta y flota) están en ``fleet.events``.
La carga de cada uno es la **unión** de los campos de ``interfaces-para-u04-u05.md`` §2 y de
``domain-entities.md`` §6 (nota T-08: donde difieren, manda interfaces). Solo identificadores,
valores de listas cerradas, enteros y marcas, hasta 64 KB (BR-NUC-75,
``shared.outbox.publish.MAX_PAYLOAD_BYTES``): nunca texto libre, contenido de registros ni datos
de personas. ``gate_state_changed`` dice si hubo motivo (``reason_es_present``), nunca cuál.
La organización, la planta, ``event_id``, ``occurred_at`` y ``correlation_id`` van en el propio
evento, no en la carga.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StrictBool, StrictInt
from vigia_contracts.models.common import UUID, TechnicalId, Timestamp
from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from vigia_platform.catalog.domain.enums import CatalogChangedField, GateKind, RegressionCause
from vigia_platform.catalog.record_types import MAX_MATRIX_ROWS
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = ["CATALOG_EVENT_TYPES", "register_catalog_event_types"]

CatalogVersion = Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]

AffectedRows = (
    Annotated[tuple[UUID, ...], Field(min_length=1, max_length=MAX_MATRIX_ROWS)] | Literal["all"]
)


class GateStateChanged(PayloadModel):
    zone_id: UUID
    gate: GateKind
    status: GateStatus
    resulting_mode: ZoneMode
    record_id: UUID
    reason_es_present: StrictBool


class ZoneActivated(PayloadModel):
    zone_id: UUID
    activated_at: Timestamp
    agreement_id: UUID
    commissioning_record_id: UUID


class CatalogUpdated(PayloadModel):
    zone_id: UUID
    catalog_version: CatalogVersion
    changed_fields: Annotated[tuple[CatalogChangedField, ...], Field(min_length=1, max_length=8)]


class RegressionMarked(PayloadModel):
    zone_id: UUID
    cause: RegressionCause
    catalog_version: CatalogVersion | None = None
    model_version: TechnicalId | None = None
    affected_row_ids: AffectedRows


class RegressionCleared(PayloadModel):
    zone_id: UUID
    cleared_by_session_id: UUID
    cause: RegressionCause
    catalog_version: CatalogVersion | None = None
    model_version: TechnicalId | None = None
    affected_row_ids: AffectedRows


CATALOG_EVENT_TYPES: Final[tuple[EventType, ...]] = tuple(
    EventType(
        event_name=name,
        publisher_unit=ActorUnit.U03,
        payload_model=model,
        description_es=description,
    )
    for name, model, description in (
        (
            "gate_state_changed",
            GateStateChanged,
            "Se aprobó o se revocó una compuerta de la zona; cambia su modo resultante",
        ),
        (
            "zone_activated",
            ZoneActivated,
            "La compuerta de uso quedó aprobada con acta cerrada: la zona pasa a productiva",
        ),
        (
            "catalog_updated",
            CatalogUpdated,
            "Se publicó una versión nueva del catálogo firmado de la zona",
        ),
        (
            "regression_marked",
            RegressionMarked,
            "Un cambio afecta filas de la matriz del walk-test: el acta deja de estar vigente",
        ),
        (
            "regression_cleared",
            RegressionCleared,
            "Una reejecución cubrió las filas afectadas: el acta vuelve a estar vigente",
        ),
    )
)
"""Los cinco eventos del catálogo y las compuertas."""


def register_catalog_event_types(registry: EventTypeRegistry) -> None:
    """Registra los cinco eventos del catálogo al arrancar."""
    for event_type in CATALOG_EVENT_TYPES:
        registry.register(event_type)
