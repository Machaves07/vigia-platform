"""Eventos de la acreditación en la bandeja (domain-entities.md de U-04, §11.3).

Cuatro de los 25 eventos de U-04: ``export_available``, ``monthly_package_available``,
``commitments_recorded`` y ``adoption_low``. Las reglas de la carga son las de
``loop.events``: solo identificadores, listas cerradas, enteros y marcas, hasta 64 KB.

Además, un evento **interno** que no es de §11.3 (TASK-317): ``export_requested``, el trabajo que
la solicitud de exportación publica en su transacción de admisión para el consumidor
``export_worker`` («publicar el trabajo en la bandeja de salida», business-logic-model §6 y
LC-LAZ §8.2). Es el patrón de ``integrity_verification_requested`` de U-02: ningún consumidor de
notificaciones lo escucha. Se publica en la partición de la **organización** (``plant_id`` nulo):
el despachador entrega en orden y sin solapar las cabezas de una partición, así que las
exportaciones de una organización se componen de una en una (PAT-LAZ-RES-01).
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, StrictInt, StrictStr
from vigia_contracts.models.common import UUID, Timestamp, UUIDv7

from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry, PayloadModel

__all__ = [
    "ACCREDITATION_EVENT_TYPES",
    "ACCREDITATION_INTERNAL_EVENT_TYPES",
    "EXPORT_REQUESTED",
    "ExportRequested",
    "register_accreditation_event_types",
]

EXPORT_REQUESTED: Final = "export_requested"

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


class ExportRequested(PayloadModel):
    """Trabajo de composición de una exportación admitida (interno, TASK-317)."""

    export_id: UUIDv7
    plant_id: UUID
    zone_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=20)]
    package_form: PackageForm


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

ACCREDITATION_INTERNAL_EVENT_TYPES: Final[tuple[EventType, ...]] = (
    EventType(
        event_name=EXPORT_REQUESTED,
        publisher_unit=ActorUnit.U04,
        payload_model=ExportRequested,
        description_es="Se admitió una exportación; el worker la compone",
    ),
)
"""El trabajo de la exportación (TASK-317): interno, fuera de los 25 de §11.3."""


def register_accreditation_event_types(registry: EventTypeRegistry) -> None:
    """Registra los cuatro eventos de la acreditación y el trabajo interno al arrancar."""
    for event_type in (*ACCREDITATION_EVENT_TYPES, *ACCREDITATION_INTERNAL_EVENT_TYPES):
        registry.register(event_type)
