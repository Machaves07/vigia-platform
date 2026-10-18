"""Exportación por HTTP (TASK-317; LC-LAZ §6.7; SUP-U05-21 y 22; A-68).

- ``POST /accreditation/exports`` (``export.create`` sobre **todas** las zonas pedidas):
  ``{plant_id, zone_ids (1 a 20), period {from, to} (≤ 366 días), package_form}`` → ``202`` con el
  estado de la solicitud (``requested``) y su ``export_id``. Rechazos: más de 20 zonas, más de
  366 días o más de 20 GB estimados en la forma con clips, ``400 invalid_request`` con
  ``accreditation_export_scope_too_large``; zonas de otra planta, ``400`` con
  ``accreditation_export_zones_span_plants`` (A-68); una zona inexistente, de otra organización o
  fuera del alcance, ``404 not_found`` (nunca ``forbidden``); por encima de 5 por hora y usuario o
  20 por hora y organización, ``429 rate_limited`` con ``Retry-After``.
- ``GET /accreditation/exports/{export_id}`` (``export.create`` sobre sus zonas): ``export_state``
  (``requested | in_progress | ready | failed | expired``; ``ready`` con la descarga vencida se lee
  ``expired``), ``progress_percent``, ``current_artifact``, ``attempt_count``, ``failure_code``,
  ``expires_at`` y, si está ``ready``, el resumen del manifiesto.

``Cache-Control: no-store``. Las marcas, ISO 8601 UTC con milisegundos y ``Z``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.export.request import (
    ExportRateLimited,
    ExportRequestInvalid,
    ExportRequestRejected,
    ExportScope,
)
from vigia_platform.accreditation.export.state import MAX_ZONES, ExportStatus
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.domain.enums import ExportState, PackageForm
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["ExportStatusOut", "export_router", "export_status_view"]

_CREATE: Final = PermissionKey.EXPORT_CREATE.value
_REQUEST_DETAIL_CODES: Final = (
    AccreditationDetailCode.EXPORT_SCOPE_TOO_LARGE.value,
    AccreditationDetailCode.EXPORT_ZONES_SPAN_PLANTS.value,
)
_MAX_ZONES_PARSED: Final = 4 * MAX_ZONES
"""Tope de análisis del cuerpo: más de 20 zonas es un rechazo de negocio con su código, no un
cuerpo inválido; por encima de esto, ``invalid_request``."""

Services = Annotated[AccreditationHttp, Depends(accreditation_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeriodBody(_Strict):
    """``{from, to}``: ``from`` es palabra reservada de Python, de ahí el alias."""

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime


class ExportRequestBody(_Strict):
    plant_id: uuid.UUID
    zone_ids: Annotated[tuple[uuid.UUID, ...], Field(min_length=1, max_length=_MAX_ZONES_PARSED)]
    period: PeriodBody
    package_form: PackageForm


class PeriodView(_Strict):
    from_: str = Field(serialization_alias="from")
    to: str


class ManifestSummaryView(_Strict):
    """Resumen del manifiesto de un paquete ``ready``."""

    document_sha256: str
    archive_sha256: str
    size_bytes: int
    artifact_count: int
    checkpoint_count: int
    verifier_version: str


class ExportStatusOut(_Strict):
    export_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    period: PeriodView
    package_form: PackageForm
    export_state: ExportState
    progress_percent: int
    current_artifact: str | None
    attempt_count: int
    failure_code: str | None
    requested_at: str
    started_at: str | None
    completed_at: str | None
    expires_at: str
    """La descarga caduca aquí; el paquete no se borra nunca."""
    correlation_id: uuid.UUID
    manifest_summary: ManifestSummaryView | None
    """Solo cuando el paquete está listo."""


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment)


def export_status_view(status: ExportStatus, clock: Clock) -> ExportStatusOut:
    package = status.package
    return ExportStatusOut(
        export_id=status.export_id,
        plant_id=status.plant_id,
        zone_ids=status.zone_ids,
        period=PeriodView(
            from_=format_timestamp(status.period_from), to=format_timestamp(status.period_to)
        ),
        package_form=status.package_form,
        export_state=status.shown_state(clock.now()),
        progress_percent=status.progress_percent,
        current_artifact=status.current_artifact,
        attempt_count=status.attempt_count,
        failure_code=status.failure_code,
        requested_at=format_timestamp(status.requested_at),
        started_at=_stamp(status.started_at),
        completed_at=_stamp(status.completed_at),
        expires_at=format_timestamp(status.expires_at),
        correlation_id=status.correlation_id,
        manifest_summary=(
            None
            if package is None
            else ManifestSummaryView(
                document_sha256=package.document_sha256,
                archive_sha256=package.archive_sha256,
                size_bytes=package.size_bytes,
                artifact_count=package.artifact_count,
                checkpoint_count=package.checkpoint_count,
                verifier_version=package.verifier_version,
            )
        ),
    )


def export_router() -> APIRouter:
    router = APIRouter(tags=["acreditación"])

    @router.post(
        "/accreditation/exports",
        status_code=202,
        dependencies=[
            requires(_CREATE, detail_codes=_REQUEST_DETAIL_CODES),
            Depends(exact_query()),
        ],
        response_model_by_alias=True,
        summary="Solicita una exportación de una planta (1 a 20 zonas, hasta 366 días)",
    )
    async def request_export(
        body: ExportRequestBody, request: Request, response: Response, services: Services
    ) -> ExportStatusOut:
        no_store(response)
        exports = installed(services.exports)
        try:
            status = await exports.request(
                request_context(request),
                ExportScope(
                    plant_id=body.plant_id,
                    zone_ids=body.zone_ids,
                    period_from=body.period.from_,
                    period_to=body.period.to,
                    package_form=body.package_form,
                ),
            )
        except ExportRequestRejected as error:
            raise ApiError(
                ApiErrorCode.INVALID_REQUEST, detail_code=error.detail_code.value
            ) from None
        except ExportRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except ExportRateLimited as error:
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=error.retry_after_seconds
            ) from None
        return export_status_view(status, exports.clock)

    @router.get(
        "/accreditation/exports/{export_id}",
        dependencies=[requires(_CREATE), Depends(exact_query())],
        response_model_by_alias=True,
        summary="Estado y progreso de una exportación, con el resumen del manifiesto si está lista",
    )
    async def export_status(
        export_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ExportStatusOut:
        no_store(response)
        exports = installed(services.exports)
        status = await exports.status(request_context(request), export_id)
        return export_status_view(status, exports.clock)

    return router
