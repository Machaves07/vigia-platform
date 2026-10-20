"""Rutas de lectura del paquete mensual (TASK-344; SUP-U05-24, 25 y 41; LC-LAZ-24).

Tres rutas, solo lectura, sobre lo que ``monthly.task.MonthlyPackageTask`` sella:

- ``GET /accreditation/monthly-packages`` (SUP-U05-24): el resumen del paquete sellado de una
  zona y un mes. ``404 not_found`` si la zona no existe o está fuera de alcance; ``409 conflict``
  con ``accreditation_monthly_package_not_available`` si no hay paquete sellado para ese
  ``(zone_id, period)`` (``generating``/``failed``, o no existe fila).
- ``GET /accreditation/monthly-packages/{package_id}/sample`` (SUP-U05-25): los elementos de la
  muestra auditada (``accreditation.audit_sample_item``), con su revisión si ya la hay
  (``outcome``/``comment``/``reviewed_by``/``reviewed_at`` nulos hasta que el coordinador
  registra el resultado, SUP-U05-26 de abajo). ``404`` si el
  paquete no existe o su zona está fuera de alcance.
- ``GET /accreditation/monthly-packages/{plant_id}/{period}/download`` (SUP-U05-41): una URL
  prefirmada de 15 minutos **por zona dentro del alcance del actor** con paquete sellado para
  ``period``; las zonas fuera de alcance, o sin paquete sellado, simplemente no aparecen en la
  lista (nunca un error por ellas): es una ruta de lista, no de un solo recurso. Una planta sin
  ninguna zona visible, o sin ningún paquete sellado, da una lista vacía con ``200``. Cada URL
  deja una entrada de auditoría ``monthly_package_download_granted`` en la misma transacción en
  la que se emite (PAT-LAZ-SEG-04, como ``export.download``).

Y las cuatro escrituras y lecturas de la revisión mensual (TASK-347; SUP-U05-26 a 29), sobre
``monthly.audit.SampleAuditService`` y ``monthly.commitments.CommitmentService``:

- ``POST /accreditation/sample-items/{sample_item_id}/result`` (SUP-U05-26, ``findings.classify``
  sobre la zona): ``{outcome, comment?}`` → ``201`` con el recibo del registro
  ``sample_audit_result``. ``409`` con ``accreditation_sample_already_reviewed`` (un segundo
  resultado) o ``accreditation_monthly_package_not_available`` (paquete sin sellar); ``400`` con
  ``accreditation_sample_cannot_modify_classification`` (el cuerpo intenta alterar o revertir la
  clasificación auditada) o con un código de los validadores del lazo.
- ``POST /accreditation/monthly-packages/{package_id}/commitments`` (SUP-U05-27,
  ``commitments.write`` sobre la zona): ``{commitments: [{text, responsible_user_id, due_date}]}``
  (1 a 20) → ``201`` con el lote registrado (``commitments_recorded`` y su evento).
- ``PATCH /accreditation/commitments/{commitment_id}`` (SUP-U05-28, ``commitments.write``):
  ``{state, note?}`` → ``200`` con el compromiso y el registro nuevo de la actualización.
  En las dos, ``400`` con ``accreditation_commitment_out_of_scope`` (paquete visible fuera del
  alcance de escritura, o responsable sin alcance sobre la zona) y ``409`` con
  ``accreditation_monthly_package_not_available`` (paquete sin sellar, solo el alta).
- ``GET /accreditation/commitments`` (SUP-U05-29, ``review_package.read`` sobre la zona):
  ``zone_id`` y ``period`` (``AAAA-MM``) → los compromisos de los paquetes de ese mes, completos.

Fuera de alcance o inexistente, siempre ``404 not_found`` (nunca ``forbidden``).

``Cache-Control: no-store`` en todas; las marcas, ISO 8601 UTC con milisegundos y ``Z``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Annotated, Any, Final, Protocol

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.monthly.audit import (
    SampleAuditRejected,
    SampleAuditRequestInvalid,
    SampleAuditResult,
)
from vigia_platform.accreditation.monthly.commitments import (
    CommitmentDraft,
    CommitmentRejected,
    CommitmentRequestInvalid,
    CommitmentRow,
)
from vigia_platform.accreditation.monthly.repository import (
    MonthlyPackageRepository,
    MonthlyPackageRow,
    SampleItemRow,
    object_key,
)
from vigia_platform.accreditation.record_types import MAX_COMMITMENTS
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter, ResourceRef
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import PresignedRequest

__all__ = [
    "COMMITMENTS_PATH",
    "COMMITMENT_PATH",
    "MONTHLY_PACKAGE_RESOURCE_KIND",
    "MONTHLY_PACKAGE_URL_TTL",
    "PACKAGE_COMMITMENTS_PATH",
    "SAMPLE_RESULT_PATH",
    "MonthlyPackageDownloadSigner",
    "MonthlyPackageReads",
    "monthly_router",
]

SAMPLE_RESULT_PATH: Final = "/accreditation/sample-items/{sample_item_id}/result"
PACKAGE_COMMITMENTS_PATH: Final = "/accreditation/monthly-packages/{package_id}/commitments"
COMMITMENT_PATH: Final = "/accreditation/commitments/{commitment_id}"
COMMITMENTS_PATH: Final = "/accreditation/commitments"

_PERIOD_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_READ: Final = PermissionKey.REVIEW_PACKAGE_READ.value
_CLASSIFY: Final = PermissionKey.FINDINGS_CLASSIFY.value
_WRITE: Final = PermissionKey.COMMITMENTS_WRITE.value
_NOT_AVAILABLE: Final = AccreditationDetailCode.MONTHLY_PACKAGE_NOT_AVAILABLE.value
_VALIDATORS: Final = (
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
)
_SAMPLE_RESULT_CODES: Final = (
    AccreditationDetailCode.SAMPLE_ALREADY_REVIEWED.value,
    AccreditationDetailCode.SAMPLE_CANNOT_MODIFY_CLASSIFICATION.value,
    _NOT_AVAILABLE,
    *_VALIDATORS,
)
_RECORD_COMMITMENTS_CODES: Final = (
    AccreditationDetailCode.COMMITMENT_OUT_OF_SCOPE.value,
    _NOT_AVAILABLE,
    *_VALIDATORS,
)
_UPDATE_COMMITMENT_CODES: Final = (
    AccreditationDetailCode.COMMITMENT_OUT_OF_SCOPE.value,
    *_VALIDATORS,
)

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
    classification_outcome: str
    reason_category: str
    classified_by_role: str | None
    classified_at: str
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


# --- revisión mensual: cuerpos y vistas (TASK-347) -------------------------------------------


class SampleResultBody(BaseModel):
    """El resultado de un elemento de la muestra.

    Admite campos de más **solo para reconocerlos**: si nombran la clasificación auditada, el
    rechazo es ``sample_cannot_modify_classification`` (BR-LAZ-107); si no, ``invalid_request``.
    Nada de lo que no sea ``outcome`` o ``comment`` llega nunca al registro."""

    model_config = ConfigDict(strict=True, extra="allow")

    outcome: StrictStr | None = None
    """``consistent_criterion``, ``doubtful_criterion`` o ``criterion_error``."""
    comment: StrictStr | None = None
    """Hasta 1000 caracteres; pasa los dos validadores de texto de U-04."""


class SampleResultOut(_Strict):
    sample_item_id: uuid.UUID
    sample_id: uuid.UUID
    package_id: uuid.UUID
    classification_id: uuid.UUID
    outcome: str
    comment: str | None
    record_id: uuid.UUID
    reviewed_at: str
    review_completed: bool


class CommitmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr
    """De 20 a 1000 caracteres; pasa los dos validadores de texto de U-04."""
    responsible_user_id: uuid.UUID
    """Un usuario activo con un rol vigente sobre la zona, su planta o la organización."""
    due_date: StrictStr
    """``AAAA-MM-DD``."""


class CommitmentsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitments: Annotated[
        tuple[CommitmentIn, ...], Field(min_length=1, max_length=MAX_COMMITMENTS)
    ]


class CommitmentUpdateBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    state: StrictStr
    """``open``, ``done`` o ``dropped``."""
    note: StrictStr | None = None
    """Nota de cierre de hasta 500 caracteres, solo con ``done`` o ``dropped``."""


class CommitmentOut(_Strict):
    commitment_id: uuid.UUID
    package_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    text: str
    responsible_user_id: uuid.UUID
    due_date: str
    state: str
    recorded_at: str
    record_id: uuid.UUID
    """El registro ``commitments_recorded`` del alta."""
    closed_note: str | None
    closed_at: str | None


class CommitmentsRecordedOut(_Strict):
    commitment_batch_id: uuid.UUID
    record_id: uuid.UUID
    package_id: uuid.UUID
    zone_id: uuid.UUID
    commitments: tuple[CommitmentOut, ...]


class CommitmentUpdatedOut(_Strict):
    commitment: CommitmentOut
    record_id: uuid.UUID
    """El registro ``commitments_recorded`` (versión 2) de esta actualización."""


def _sample_result_view(result: SampleAuditResult) -> SampleResultOut:
    return SampleResultOut(
        sample_item_id=result.sample_item_id,
        sample_id=result.sample_id,
        package_id=result.package_id,
        classification_id=result.classification_id,
        outcome=result.outcome.value,
        comment=result.comment,
        record_id=result.record_id,
        reviewed_at=format_timestamp(result.reviewed_at),
        review_completed=result.review_completed,
    )


def _commitment_view(row: CommitmentRow) -> CommitmentOut:
    return CommitmentOut(
        commitment_id=row.commitment_id,
        package_id=row.package_id,
        zone_id=row.zone_id,
        period=row.period,
        text=row.text,
        responsible_user_id=row.responsible_user_id,
        due_date=row.due_date.isoformat(),
        state=row.state.value,
        recorded_at=format_timestamp(row.recorded_at),
        record_id=row.record_id,
        closed_note=row.closed_note,
        closed_at=None if row.closed_at is None else format_timestamp(row.closed_at),
    )


def _rejected(error: SampleAuditRejected | CommitmentRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[error.detail_code]
    return ApiError(code, detail_code=error.detail_code.value)


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
        classification_outcome=row.classification_outcome,
        reason_category=row.reason_category,
        classified_by_role=row.classified_by_role,
        classified_at=format_timestamp(row.classified_at),
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

        Las zonas fuera de alcance se omiten en silencio dentro de una lista no vacía: es un
        filtro de una ruta de lista, nunca un ``404``/``403`` de la petición entera. ``plant_id``
        en sí sí se comprueba: una planta de otra organización (o inexistente) responde
        ``not_found``, en vez de una lista vacía que delataría, por el ``plant_id`` que la
        respuesta repite, que el identificador pedido no es el de una planta real (PR-NUC-01).
        Si la lista queda vacía (ninguna zona en alcance tiene un paquete sellado de ``period``),
        ``monthly_package_not_available`` (409), alineado con LC-LAZ-26 §6.7 (nota U04-H-03):
        «un paquete sin sellar responde monthly_package_not_available».
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
        if not grants:
            raise MonthlyPackageNotAvailable()
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
        dependencies=[
            requires(_READ, detail_codes=(_NOT_AVAILABLE,)),
            Depends(exact_query()),
        ],
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
        try:
            zones = await reads.plant_download(
                request_context(request), plant_id=plant_id, period=period
            )
        except MonthlyPackageNotAvailable:
            raise ApiError(ApiErrorCode.CONFLICT, detail_code=_NOT_AVAILABLE) from None
        return MonthlyPackageDownloadOut(plant_id=plant_id, period=period, zones=zones)

    # --- TASK-347: auditoría muestral y compromisos (SUP-U05-26 a 29) ------------------------

    @router.post(
        SAMPLE_RESULT_PATH,
        status_code=201,
        dependencies=[
            requires(_CLASSIFY, detail_codes=_SAMPLE_RESULT_CODES),
            Depends(exact_query()),
        ],
        summary="Registra el resultado de un elemento de la muestra (no toca la clasificación)",
    )
    async def record_sample_result(
        sample_item_id: uuid.UUID,
        body: SampleResultBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> SampleResultOut:
        no_store(response)
        try:
            result = await installed(services.sample_audit).record_result(
                request_context(request),
                sample_item_id,
                outcome=body.outcome,
                comment=body.comment,
                extra_fields=dict(body.model_extra or {}),
            )
        except SampleAuditRejected as error:
            raise _rejected(error) from None
        except SampleAuditRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return _sample_result_view(result)

    @router.post(
        PACKAGE_COMMITMENTS_PATH,
        status_code=201,
        dependencies=[
            requires(_WRITE, detail_codes=_RECORD_COMMITMENTS_CODES),
            Depends(exact_query()),
        ],
        summary="Registra los compromisos del mando de línea sobre un paquete mensual sellado",
    )
    async def record_commitments(
        package_id: uuid.UUID,
        body: CommitmentsBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CommitmentsRecordedOut:
        no_store(response)
        try:
            result = await installed(services.commitments).record(
                request_context(request),
                package_id,
                [
                    CommitmentDraft(
                        text=entry.text,
                        responsible_user_id=entry.responsible_user_id,
                        due_date=entry.due_date,
                    )
                    for entry in body.commitments
                ],
            )
        except CommitmentRejected as error:
            raise _rejected(error) from None
        except CommitmentRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return CommitmentsRecordedOut(
            commitment_batch_id=result.commitment_batch_id,
            record_id=result.record_id,
            package_id=result.package_id,
            zone_id=result.zone_id,
            commitments=tuple(_commitment_view(row) for row in result.commitments),
        )

    @router.patch(
        COMMITMENT_PATH,
        dependencies=[
            requires(_WRITE, detail_codes=_UPDATE_COMMITMENT_CODES),
            Depends(exact_query()),
        ],
        summary="Actualiza el estado y la nota de cierre de un compromiso (registro nuevo)",
    )
    async def update_commitment(
        commitment_id: uuid.UUID,
        body: CommitmentUpdateBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CommitmentUpdatedOut:
        no_store(response)
        try:
            result = await installed(services.commitments).update(
                request_context(request), commitment_id, state=body.state, note=body.note
            )
        except CommitmentRejected as error:
            raise _rejected(error) from None
        except CommitmentRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return CommitmentUpdatedOut(
            commitment=_commitment_view(result.commitment), record_id=result.record_id
        )

    @router.get(
        COMMITMENTS_PATH,
        dependencies=[requires(_READ), Depends(exact_query("zone_id", "period"))],
        summary="Compromisos de los paquetes mensuales de una zona y un mes, con su estado",
    )
    async def list_commitments(
        zone_id: uuid.UUID,
        period: Annotated[str, Query(pattern=_PERIOD_PATTERN)],
        request: Request,
        response: Response,
        services: Services,
    ) -> tuple[CommitmentOut, ...]:
        no_store(response)
        rows = await installed(services.commitments).of_zone(
            request_context(request), zone_id=zone_id, period=period
        )
        return tuple(_commitment_view(row) for row in rows)

    return router
