"""Adopción del lazo por zona (SUP-U05-32 ``consultar_adopcion_por_zona``; LC-LAZ-29; TASK-323).

``GET /accreditation/adoption`` (``metrics.read``, planta o zona) con ``plant_id`` **o**
``zone_id`` (uno solo), ``from`` y ``to`` (fechas ``AAAA-MM-DD`` locales de la planta, ambas
incluidas, a lo sumo 366 días). Responde ``{plant_id, period_from, period_to, evaluated_day,
threshold_days, usage_notice_es, zones}``: por zona, si está activa, sus días de silencio de hoy
(``null`` si no está activa), el inicio de la racha, la última actividad y la actividad de cada
día con alguna, por clase. **Por zona, nunca por persona** (BR-LAZ-135): ningún parámetro de
persona, equipo ni turno, y el texto fijo ``usage_notice_es``.

Una planta o una zona inexistente, de otra organización o fuera del alcance responde ``not_found``
(nunca ``forbidden``); un parámetro desconocido, repetido, con formato inválido o un periodo fuera
de rango, ``invalid_request``. ``Cache-Control: no-store``. Sin auditoría de consulta.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.adoption.reads import (
    USAGE_NOTICE_ES,
    AdoptionReport,
    AdoptionRequestInvalid,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.calendar import CalendarInvalid, parse_date
from vigia_platform.loop.domain.enums import LoopActivityKind
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["PATH", "AdoptionReportOut", "adoption_router", "report_view"]

PATH: Final = "/accreditation/adoption"
_READ: Final = PermissionKey.METRICS_READ.value
_DATE_CHARS: Final = 10

Services = Annotated[AccreditationHttp, Depends(accreditation_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdoptionDayOut(_Strict):
    day: str
    """Fecha local de la planta (``AAAA-MM-DD``)."""
    activity_counts: dict[LoopActivityKind, int]
    """Actividades del día por clase (las seis; cero las que no hubo)."""
    activity_count: int


class AdoptionZoneOut(_Strict):
    zone_id: uuid.UUID
    zone_active: bool
    """``true`` si la zona está activa (montaje y uso aprobados): solo entonces puede alertar."""
    silent_days: int | None
    """Días completos sin actividad hasta ayer; ``null`` si la zona no está activa."""
    streak_start: str | None
    last_activity_at: str | None
    days: tuple[AdoptionDayOut, ...]
    """Los días del periodo con alguna actividad; uno que no aparece no tuvo ninguna."""


class AdoptionReportOut(_Strict):
    plant_id: uuid.UUID
    period_from: str
    period_to: str
    evaluated_day: str
    """Hoy en la planta: el día de los días de silencio."""
    threshold_days: int
    """``adoption_silence_days`` vigente de la planta."""
    usage_notice_es: str
    zones: tuple[AdoptionZoneOut, ...]


def _instant(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment)


def report_view(report: AdoptionReport) -> AdoptionReportOut:
    return AdoptionReportOut(
        plant_id=report.plant_id,
        period_from=report.period_from.isoformat(),
        period_to=report.period_to.isoformat(),
        evaluated_day=report.evaluated_day.isoformat(),
        threshold_days=report.threshold_days,
        usage_notice_es=USAGE_NOTICE_ES,
        zones=tuple(
            AdoptionZoneOut(
                zone_id=zone.zone_id,
                zone_active=zone.active,
                silent_days=zone.silent_days,
                streak_start=None if zone.streak_start is None else zone.streak_start.isoformat(),
                last_activity_at=_instant(zone.last_activity_at),
                days=tuple(
                    AdoptionDayOut(
                        day=day.day.isoformat(),
                        activity_counts=dict(day.activity_counts),
                        activity_count=day.activity_count,
                    )
                    for day in zone.days
                ),
            )
            for zone in report.zones
        ),
    )


def _date(value: str) -> date:
    try:
        return parse_date(value)
    except CalendarInvalid:
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def adoption_router() -> APIRouter:
    router = APIRouter(tags=["acreditación"])

    @router.get(
        PATH,
        dependencies=[
            requires(_READ),
            Depends(exact_query("plant_id", "zone_id", "from", "to")),
        ],
        summary="Actividad del lazo por zona y día, días de silencio y umbral vigente",
    )
    async def get_adoption(
        request: Request,
        response: Response,
        services: Services,
        from_: Annotated[str, Query(alias="from", max_length=_DATE_CHARS)],
        to: Annotated[str, Query(max_length=_DATE_CHARS)],
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
    ) -> AdoptionReportOut:
        no_store(response)
        reads = installed(services.adoption)
        try:
            report = await reads.report(
                request_context(request),
                plant_id=plant_id,
                zone_id=zone_id,
                period_from=_date(from_),
                period_to=_date(to),
            )
        except AdoptionRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return report_view(report)

    return router
