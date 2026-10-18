"""Eventos de la acreditación en la bandeja (domain-entities.md de U-04, §11.3).

Cuatro de los 25 eventos de U-04: ``export_available``, ``monthly_package_available``,
``commitments_recorded`` y ``adoption_low``. Las reglas de la carga son las de
``loop.events``: solo identificadores, listas cerradas, enteros y marcas, hasta 64 KB.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, StrictInt, StrictStr
from vigia_contracts.models.common import UUID, Timestamp, UUIDv7

from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = ["ACCREDITATION_EVENT_TYPES", "register_accreditation_event_types"]

Month = Annotated[
    StrictStr, Field(min_length=7, max_length=7, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
]


class ExportAvailable(PayloadModel):
    """El paquete está listo para descargar hasta ``expires_at`` (el paquete no se borra)."""

    export_id: UUIDv7
    record_id: UUID
    zone_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=20)]
    package_form: PackageForm
    expires_at: Timestamp


class MonthlyPackageAvailable(PayloadModel):
    package_id: UUIDv7
    record_id: UUID
    zone_id: UUID
    period: Month


class CommitmentsRecorded(PayloadModel):
    commitment_batch_id: UUIDv7
    record_id: UUID
    package_id: UUIDv7
    zone_id: UUID
    commitment_count: Annotated[StrictInt, Field(ge=1, le=20)]


class AdoptionLow(PayloadModel):
    """Una zona lleva ``silent_days`` sin actividad del lazo: se cuenta por zona, nunca por
    usuario (H-33)."""

    zone_id: UUID
    silent_days: Annotated[StrictInt, Field(ge=1, le=3_660)]
    last_activity_at: Timestamp | None = None


ACCREDITATION_EVENT_TYPES: Final[tuple[EventType, ...]] = tuple(
    EventType(
        event_name=name,
        publisher_unit=ActorUnit.U04,
        payload_model=model,
        description_es=description,
    )
    for name, model, description in (
        ("export_available", ExportAvailable, "Una exportación está lista para descargar"),
        (
            "monthly_package_available",
            MonthlyPackageAvailable,
            "El paquete mensual de una zona quedó sellado",
        ),
        (
            "commitments_recorded",
            CommitmentsRecorded,
            "Se registraron los compromisos de la revisión mensual de una zona",
        ),
        (
            "adoption_low",
            AdoptionLow,
            "Una zona lleva varios días sin actividad del lazo",
        ),
    )
)
"""Los cuatro eventos de la acreditación, en el orden de §11.3."""


def register_accreditation_event_types(registry: EventTypeRegistry) -> None:
    """Registra los cuatro eventos de la acreditación al arrancar."""
    for event_type in ACCREDITATION_EVENT_TYPES:
        registry.register(event_type)
