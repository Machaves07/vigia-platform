"""Rutas de lectura del paquete mensual (TASK-344; SUP-U05-24, 25 y 41; LC-LAZ-24).

Tres rutas, solo lectura, sobre lo que ``monthly.task.MonthlyPackageTask`` sella:

- ``GET /accreditation/monthly-packages`` (SUP-U05-24): el resumen del paquete sellado de una
  zona y un mes. ``404 not_found`` si la zona no existe o está fuera de alcance; ``409 conflict``
  con ``accreditation_monthly_package_not_available`` si no hay paquete sellado para ese
  ``(zone_id, period)`` (``generating``/``failed``, o no existe fila).
- ``GET /accreditation/monthly-packages/{package_id}/sample`` (SUP-U05-25): los elementos de la
  muestra auditada (``accreditation.audit_sample_item``), con su revisión si ya la hay
  (``outcome``/``comment``/``reviewed_by``/``reviewed_at`` nulos hasta TASK-347). ``404`` si el
  paquete no existe o su zona está fuera de alcance.
- ``GET /accreditation/monthly-packages/{plant_id}/{period}/download`` (SUP-U05-41): una URL
  prefirmada de 15 minutos **por zona dentro del alcance del actor** con paquete sellado para
  ``period``; las zonas fuera de alcance, o sin paquete sellado, simplemente no aparecen en la
  lista (nunca un error por ellas): es una ruta de lista, no de un solo recurso. Una planta sin
  ninguna zona visible, o sin ningún paquete sellado, da una lista vacía con ``200``. Cada URL
  deja una entrada de auditoría ``monthly_package_download_granted`` en la misma transacción en
  la que se emite (PAT-LAZ-SEG-04, como ``export.download``).

``Cache-Control: no-store`` en las tres; las marcas, ISO 8601 UTC con milisegundos y ``Z``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Annotated, Any, Final, Protocol

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.monthly.repository import (
    MonthlyPackageRepository,
    MonthlyPackageRow,
    SampleItemRow,
    object_key,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter, ResourceRef
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import PresignedRequest

__all__ = [
    "MONTHLY_PACKAGE_RESOURCE_KIND",
    "MONTHLY_PACKAGE_URL_TTL",
    "MonthlyPackageDownloadSigner",
    "MonthlyPackageReads",
    "monthly_router",
]

_PERIOD_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_READ: Final = PermissionKey.REVIEW_PACKAGE_READ.value
_NOT_AVAILABLE: Final = AccreditationDetailCode.MONTHLY_PACKAGE_NOT_AVAILABLE.value

MONTHLY_PACKAGE_URL_TTL: Final = timedelta(minutes=15)
"""Vigencia de la URL de descarga (``[objetivo propio]``, como la de la exportación)."""
MONTHLY_PACKAGE_RESOURCE_KIND: Final = "monthly_package"
"""``resource_ref.kind`` de las entradas de auditoría de la descarga."""

Services = Annotated[AccreditationHttp, Depends(accreditation_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- vistas de respuesta ---------------------------------------------------------------------


class FindingsSummaryOut(_Strict):
    received: int
    classified: int
    confirmed: int
    discarded: int
    closed: int
    overdue: int
    unclosable: int
    reopened: int


class RecurrenceEntryOut(_Strict):
    family: str
    episodes_after_closure: int
    reference_closure_ids: tuple[str, ...]


class CoverageShareOut(_Strict):
    state: str
    duration_ms: int


class CommitmentStatusOut(_Strict):
    commitment_id: str
    state: str


class MonthlyPackageOut(_Strict):
    package_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    state: str
    generated_at: str
    findings_summary: FindingsSummaryOut
    recurrence_summary: tuple[RecurrenceEntryOut, ...]
    coverage_summary: tuple[CoverageShareOut, ...]
    commitments_from_previous_period: tuple[CommitmentStatusOut, ...]
    audit_sample_id: uuid.UUID | None
    record_id: uuid.UUID | None


class SampleItemOut(_Strict):
    sample_item_id: uuid.UUID
    classification_id: uuid.UUID
    subject_id: uuid.UUID
    outcome: str | None
    comment: str | None
    reviewed_by: Mapping[str, Any] | None
    reviewed_at: str | None


class ZoneDownloadOut(_Strict):
    zone_id: uuid.UUID
    url: str
    expires_at: str


class MonthlyPackageDownloadOut(_Strict):
    plant_id: uuid.UUID
    period: str
    zones: tuple[ZoneDownloadOut, ...]


def _package_view(row: MonthlyPackageRow) -> MonthlyPackageOut:
    return MonthlyPackageOut(
        package_id=row.package_id,
        zone_id=row.zone_id,
        period=row.period,
        state=row.state,
        generated_at=format_timestamp(row.generated_at),
        findings_summary=FindingsSummaryOut(**row.findings_summary),
        recurrence_summary=tuple(RecurrenceEntryOut(**entry) for entry in row.recurrence_summary),
        # ``coverage_summary`` es un objeto jsonb en la fila (``monthly_package_coverage``), no
        # una lista: hoy siempre vacío (TASK-321/322 no están en ``main``), nada que convertir.
        coverage_summary=(),
        commitments_from_previous_period=tuple(
            CommitmentStatusOut(**entry) for entry in row.commitments_from_previous_period
        ),
        audit_sample_id=row.audit_sample_id,
        record_id=row.record_id,
    )


def _sample_item_view(row: SampleItemRow) -> SampleItemOut:
    return SampleItemOut(
        sample_item_id=row.sample_item_id,
        classification_id=row.classification_id,
        subject_id=row.subject_id,
        outcome=row.outcome,
        comment=row.comment,
        reviewed_by=row.reviewed_by,
        reviewed_at=None if row.reviewed_at is None else format_timestamp(row.reviewed_at),
    )


# --- servicio: lecturas + autorización + firma + auditoría -----------------------------------


class MonthlyPackageNotAvailable(Exception):
    """No hay paquete sellado para ``(zone_id, period)``: 409 con el código de la lista cerrada."""


class MonthlyPackageDownloadSigner(Protocol):
    """Lo único que la descarga usa del almacén: firmar la lectura de un objeto (como en
    ``accreditation.export.download.ExportDownloadSigner``)."""

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = ..., *, version_id: str | None = None
    ) -> PresignedRequest: ...


class TransactionSource(Protocol):
    def transaction(self, context: ScopeContext) -> AbstractAsyncContextManager[Transaction]: ...


@repository
class MonthlyPackageReads:
    """Lecturas de ``accreditation.monthly_package`` autorizadas por zona (SUP-U05-24, 25 y 41)."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        repository: MonthlyPackageRepository,
        authorizer: Authorizer,
        audit: AuditWriter,
        storage: MonthlyPackageDownloadSigner,
    ) -> None:
        self._database = database
        self._repository = repository
        self._authorizer = authorizer
        self._audit = audit
        self._storage = storage

    async def package(
        self, context: ScopeContext, *, zone_id: uuid.UUID, period: str
    ) -> MonthlyPackageRow:
        """El paquete de ``(zone_id, period)``.

        ``ResourceNotFound`` si la zona no existe o está fuera de alcance;
        ``MonthlyPackageNotAvailable`` si no hay paquete sellado para ese periodo.
        """
        async with self._database.transaction(context) as transaction:
            plant_id = await self._repository.zone_plant(transaction, zone_id)
        if plant_id is None:
            raise ResourceNotFound()
        resource = Resource.zone(context.organization_id, plant_id, zone_id)
        authorized = await self._authorizer.authorize(
            context, PermissionKey.REVIEW_PACKAGE_READ, resource
        )
        async with self._database.transaction(authorized) as transaction:
            row = await self._repository.find_package(transaction, zone_id=zone_id, period=period)
        if row is None or row.state != "sealed":
            raise MonthlyPackageNotAvailable()
        return row

    async def sample(
        self, context: ScopeContext, package_id: uuid.UUID
    ) -> tuple[SampleItemRow, ...]:
        """Los elementos de la muestra del paquete ``package_id``; ``ResourceNotFound`` si no
        existe o su zona está fuera de alcance."""
        async with self._database.transaction(context) as transaction:
            package = await self._repository.package_by_id(transaction, package_id)
        if package is None:
            raise ResourceNotFound()
        resource = Resource.zone(context.organization_id, package.plant_id, package.zone_id)
        authorized = await self._authorizer.authorize(
            context, PermissionKey.REVIEW_PACKAGE_READ, resource
        )
        if package.audit_sample_id is None:
            return ()
        sample_id = package.audit_sample_id
        async with self._database.transaction(authorized) as transaction:
            return await self._repository.sample_items(transaction, sample_id=sample_id)

    async def plant_download(
        self, context: ScopeContext, *, plant_id: uuid.UUID, period: str
    ) -> tuple[ZoneDownloadOut, ...]:
        """Una URL por zona de ``plant_id`` dentro del alcance del actor con paquete sellado.

        Las zonas fuera de alcance, o sin paquete sellado, se omiten en silencio: es un filtro de
        una ruta de lista, nunca un ``404``/``403`` de la petición entera. ``plant_id`` en sí sí
        se comprueba: una planta de otra organización (o inexistente) responde ``not_found``, en
        vez de una lista vacía que delataría, por el ``plant_id`` que la respuesta repite, que el
        identificador pedido no es el de una planta real (PR-NUC-01).
        """
        async with self._database.transaction(context) as transaction:
            if not await self._repository.plant_in_scope(transaction, plant_id):
                raise ResourceNotFound()
            packages = await self._repository.sealed_packages_of_plant(
                transaction, plant_id=plant_id, period=period
            )
        grants: list[ZoneDownloadOut] = []
        for package in packages:
            try:
                authorized = await self._authorizer.authorize(
                    context,
                    PermissionKey.REVIEW_PACKAGE_READ,
                    Resource.zone(context.organization_id, package.plant_id, package.zone_id),
                )
            except ResourceNotFound:
                continue
            key = object_key(
                organization_id=package.organization_id,
                plant_id=package.plant_id,
                zone_id=package.zone_id,
                period=period,
            )
            async with self._database.transaction(authorized) as transaction:
                presigned = await self._storage.presign_attachment_get(key, MONTHLY_PACKAGE_URL_TTL)
                await self._audit.append(
                    authorized,
                    AuditOperation.MONTHLY_PACKAGE_DOWNLOAD_GRANTED,
                    plant_id=package.plant_id,
                    zone_id=package.zone_id,
                    resource=ResourceRef(MONTHLY_PACKAGE_RESOURCE_KIND, package.package_id),
                    filters={
                        "artifact": "package.zip",
                        "expires_at": format_timestamp(presigned.expires_at),
                    },
                    result_count=1,
                    transaction=transaction,
                )
            grants.append(
                ZoneDownloadOut(
                    zone_id=package.zone_id,
                    url=presigned.url,
                    expires_at=format_timestamp(presigned.expires_at),
                )
            )
        return tuple(grants)


def monthly_router() -> APIRouter:
    router = APIRouter(tags=["acreditación"])

    @router.get(
        "/accreditation/monthly-packages",
        dependencies=[
            requires(_READ, detail_codes=(_NOT_AVAILABLE,)),
            Depends(exact_query("zone_id", "period")),
        ],
        response_model_by_alias=True,
        summary="Resumen del paquete mensual sellado de una zona y un mes",
    )
    async def get_monthly_package(
        zone_id: uuid.UUID,
        period: Annotated[str, Query(pattern=_PERIOD_PATTERN)],
        request: Request,
        response: Response,
        services: Services,
    ) -> MonthlyPackageOut:
        no_store(response)
        reads = installed(services.monthly_packages)
        try:
            row = await reads.package(request_context(request), zone_id=zone_id, period=period)
        except MonthlyPackageNotAvailable:
            raise ApiError(ApiErrorCode.CONFLICT, detail_code=_NOT_AVAILABLE) from None
        return _package_view(row)

    @router.get(
        "/accreditation/monthly-packages/{package_id}/sample",
        dependencies=[requires(_READ), Depends(exact_query())],
        response_model_by_alias=True,
        summary="Elementos de la muestra auditada de un paquete mensual sellado",
    )
    async def get_monthly_package_sample(
        package_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> tuple[SampleItemOut, ...]:
        no_store(response)
        reads = installed(services.monthly_packages)
        items = await reads.sample(request_context(request), package_id)
        return tuple(_sample_item_view(item) for item in items)

    @router.get(
        "/accreditation/monthly-packages/{plant_id}/{period}/download",
        dependencies=[requires(_READ), Depends(exact_query())],
        response_model_by_alias=True,
        summary="Una URL de descarga de 15 min por zona de la planta con paquete sellado",
    )
    async def get_monthly_package_plant_download(
        plant_id: uuid.UUID,
        period: Annotated[str, Path(pattern=_PERIOD_PATTERN)],
        request: Request,
        response: Response,
        services: Services,
    ) -> MonthlyPackageDownloadOut:
        no_store(response)
        reads = installed(services.monthly_packages)
        zones = await reads.plant_download(
            request_context(request), plant_id=plant_id, period=period
        )
        return MonthlyPackageDownloadOut(plant_id=plant_id, period=period, zones=zones)

    return router
