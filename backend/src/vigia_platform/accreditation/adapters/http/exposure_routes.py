"""Ruta del indicador de exposición (TASK-322; SUP-U05-31; LC-LAZ-28; BR-LAZ-118 a 123).

``GET /accreditation/exposure?zone_id=…&period=AAAA-MM`` (``exposure.read``, zona):

- ``200``: las **tres cifras** (minutos confirmados, minutos pendientes de clasificación, que se
  declaran aparte y nunca se suman, y porcentaje de cobertura observable del periodo), el estado
  (``computed`` o ``partial``, con su motivo), la **línea de tiempo de cobertura** del mismo
  periodo, que acompaña siempre a la cifra (BR-LAZ-121), la distribución horaria (cubos por su
  minuto de inicio y su ancho, sin ningún nombre; BR-LAZ-122) y el texto fijo de uso
  (BR-LAZ-123);
- ``400 invalid_request`` con ``accreditation_exposure_not_computable`` si la zona no declara
  señal dura (BR-LAZ-120, H-41; A-68): nunca un cero en su lugar; ``400 invalid_request`` sin
  ``detail_code`` si el mes aún no empezó;
- ``404 not_found`` si la zona no existe o está fuera del alcance (nunca ``403``);
- ``503`` con ``accreditation_coverage_unavailable`` o ``loop_gate_state_unavailable`` si falta
  la línea de tiempo de U-02 o la historia de la compuerta de U-03 (fallo cerrado).

``Cache-Control: no-store``; las marcas, ISO 8601 UTC con milisegundos y ``Z``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.exposure.indicator import ExposureIndicator
from vigia_platform.accreditation.exposure.service import (
    ExposureInputMissing,
    ExposurePeriodInvalid,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.domain.enums import ExposureStatus
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["ExposureOut", "exposure_router", "exposure_view"]

_PERIOD_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_READ: Final = PermissionKey.EXPOSURE_READ.value
_NOT_COMPUTABLE: Final = AccreditationDetailCode.EXPOSURE_NOT_COMPUTABLE
_DETAIL_CODES: Final = (
    _NOT_COMPUTABLE.value,
    AccreditationDetailCode.COVERAGE_UNAVAILABLE.value,
    LoopDetailCode.GATE_STATE_UNAVAILABLE.value,
)

Services = Annotated[AccreditationHttp, Depends(accreditation_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExposureHourlyBucketOut(_Strict):
    """Un cubo de la distribución horaria: solo su inicio en el día local y su ancho."""

    start_minute_of_day: int
    width_minutes: int
    confirmed_minutes: float


class ExposureTimelineRangeOut(_Strict):
    from_: str = Field(serialization_alias="from")
    to: str


class ExposureCoverageIntervalOut(_Strict):
    from_: str = Field(serialization_alias="from")
    to: str
    state: str
    causes: tuple[str, ...]
    layer: str
    duration_ms: int


class ExposureCoverageSummaryOut(_Strict):
    observable_ms: int
    degraded_ms: int
    not_observable_ms: int
    no_communication_ms: int
    never_reported_ms: int
    zone_not_active_ms: int
    not_elapsed_ms: int
    total_ms: int


class ExposureCoverageAnnotationOut(_Strict):
    """Un tramo de compuerta de uso revocada (o de acta no vigente) sobre la línea de tiempo."""

    kind: str
    from_: str = Field(serialization_alias="from")
    to: str
    cause: str | None


class ExposureCoverageTimelineOut(_Strict):
    period: ExposureTimelineRangeOut
    intervals: tuple[ExposureCoverageIntervalOut, ...]
    summary: ExposureCoverageSummaryOut
    annotations: tuple[ExposureCoverageAnnotationOut, ...]


class ExposureOut(_Strict):
    zone_id: uuid.UUID
    period: str
    period_from: str
    period_to: str
    status: str
    confirmed_minutes: float
    pending_classification_minutes: float
    observable_coverage_percent: float
    partial_reason_es: str | None
    usage_notice_es: str
    hourly_distribution: tuple[ExposureHourlyBucketOut, ...]
    coverage_timeline: ExposureCoverageTimelineOut
    materialized_at: str
    recomputed_at: str | None


def _timeline_view(timeline: Mapping[str, Any]) -> ExposureCoverageTimelineOut:
    period = timeline["period"]
    return ExposureCoverageTimelineOut(
        period=ExposureTimelineRangeOut(from_=period["from"], to=period["to"]),
        intervals=tuple(
            ExposureCoverageIntervalOut(
                from_=interval["from"],
                to=interval["to"],
                state=interval["state"],
                causes=tuple(interval["causes"]),
                layer=interval["layer"],
                duration_ms=interval["duration_ms"],
            )
            for interval in timeline["intervals"]
        ),
        summary=ExposureCoverageSummaryOut(**timeline["summary"]),
        annotations=tuple(
            ExposureCoverageAnnotationOut(
                kind=note["kind"], from_=note["from"], to=note["to"], cause=note["cause"]
            )
            for note in timeline["annotations"]
        ),
    )


def exposure_view(indicator: ExposureIndicator) -> ExposureOut:
    """La respuesta de un indicador con cifras; ``not_computable`` no llega aquí (es un 400)."""
    if (
        indicator.status is ExposureStatus.NOT_COMPUTABLE
        or indicator.confirmed_minutes is None
        or indicator.pending_classification_minutes is None
        or indicator.observable_coverage_percent is None
        or indicator.coverage_timeline is None
        or indicator.materialized_at is None
    ):
        raise ApiError(ApiErrorCode.INVALID_REQUEST, detail_code=_NOT_COMPUTABLE.value)
    return ExposureOut(
        zone_id=indicator.zone_id,
        period=indicator.period,
        period_from=format_timestamp(indicator.period_from),
        period_to=format_timestamp(indicator.period_to),
        status=indicator.status.value,
        confirmed_minutes=indicator.confirmed_minutes,
        pending_classification_minutes=indicator.pending_classification_minutes,
        observable_coverage_percent=indicator.observable_coverage_percent,
        partial_reason_es=indicator.partial_reason_es,
        usage_notice_es=indicator.usage_notice_es,
        hourly_distribution=tuple(
            ExposureHourlyBucketOut(
                start_minute_of_day=bucket.start_minute,
                width_minutes=bucket.width_minutes,
                confirmed_minutes=bucket.confirmed_minutes,
            )
            for bucket in indicator.hourly_distribution
        ),
        coverage_timeline=_timeline_view(indicator.coverage_timeline),
        materialized_at=format_timestamp(indicator.materialized_at),
        recomputed_at=(
            None if indicator.recomputed_at is None else format_timestamp(indicator.recomputed_at)
        ),
    )


def exposure_router() -> APIRouter:
    router = APIRouter(tags=["acreditación"])

    @router.get(
        "/accreditation/exposure",
        dependencies=[
            requires(_READ, detail_codes=_DETAIL_CODES),
            Depends(exact_query("zone_id", "period")),
        ],
        response_model_by_alias=True,
        summary="Indicador de exposición de una zona y un mes, con su cobertura",
    )
    async def get_exposure(
        zone_id: uuid.UUID,
        period: Annotated[str, Query(pattern=_PERIOD_PATTERN)],
        request: Request,
        response: Response,
        services: Services,
    ) -> ExposureOut:
        no_store(response)
        exposure = installed(services.exposure)
        try:
            indicator = await exposure.read(
                request_context(request), zone_id=zone_id, period=period
            )
        except ExposurePeriodInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except ExposureInputMissing as missing:
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[missing.detail_code]
            raise ApiError(code, detail_code=missing.detail_code.value) from None
        return exposure_view(indicator)

    return router
