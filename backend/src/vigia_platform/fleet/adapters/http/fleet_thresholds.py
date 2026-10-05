"""Umbrales del panel de flota por planta (TASK-224; interfaces §3.4; DE §3.10; BR-GOB-79).

- ``GET /plants/{plant_id}/fleet-thresholds`` (``fleet.read`` sobre la planta) → ``{plant_id,
  queue_pending_threshold, queue_age_threshold_minutes, clock_drift_threshold_ms, configured,
  updated_by, updated_at}``; sin fila, 100, 30 y 5 000 `[estimación propia]` con ``configured =
  false``.
- ``PUT /plants/{plant_id}/fleet-thresholds`` (``fleet.manage`` sobre la planta) con los tres
  enteros: ``200`` con los umbrales nuevos y su entrada de auditoría ``fleet_thresholds_changed``;
  un valor menor que 1 (o mayor que 2 147 483 647) responde ``invalid_request`` con
  ``detail_code = fleet_threshold_invalid``; un valor que no es entero, ``invalid_request``.

Planta inexistente, de otra organización o fuera del alcance: ``not_found``. ``Cache-Control:
no-store``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds, ThresholdInvalid
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query
from vigia_platform.shared.api.declarations import body_limit, requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["BODY_LIMIT_BYTES", "fleet_thresholds_router"]

BODY_LIMIT_BYTES: Final = 1_024
"""Cuerpo máximo del ``PUT`` `[objetivo propio]`: tres enteros caben en menos de 200 bytes."""
_READ: Final = PermissionKey.FLEET_READ.value
_MANAGE: Final = PermissionKey.FLEET_MANAGE.value
_PUT_CODES: Final = (FleetDetailCode.THRESHOLD_INVALID.value,)

Services = Annotated[FleetHttp, Depends(fleet_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FleetThresholdsBody(_Strict):
    queue_pending_threshold: StrictInt
    queue_age_threshold_minutes: StrictInt
    clock_drift_threshold_ms: StrictInt


class FleetThresholdsView(_Strict):
    plant_id: uuid.UUID
    queue_pending_threshold: int
    queue_age_threshold_minutes: int
    clock_drift_threshold_ms: int
    configured: bool
    """``false``: la planta no tiene fila y rigen los valores por defecto `[estimación propia]`."""
    updated_by: uuid.UUID | None
    updated_at: str | None


def _view(thresholds: FleetThresholds) -> FleetThresholdsView:
    return FleetThresholdsView(
        plant_id=thresholds.plant_id,
        queue_pending_threshold=thresholds.queue_pending_threshold,
        queue_age_threshold_minutes=thresholds.queue_age_threshold_minutes,
        clock_drift_threshold_ms=thresholds.clock_drift_threshold_ms,
        configured=not thresholds.is_default,
        updated_by=thresholds.updated_by,
        updated_at=(
            None if thresholds.updated_at is None else format_timestamp(thresholds.updated_at)
        ),
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def fleet_thresholds_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.get(
        "/plants/{plant_id}/fleet-thresholds",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Umbrales del panel de flota de la planta (valores por defecto si no hay)",
    )
    async def plant_fleet_thresholds(
        plant_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> FleetThresholdsView:
        _no_store(response)
        found = await installed(services.thresholds).thresholds(request_context(request), plant_id)
        return _view(found)

    @router.put(
        "/plants/{plant_id}/fleet-thresholds",
        dependencies=[
            requires(_MANAGE, detail_codes=_PUT_CODES),
            body_limit(BODY_LIMIT_BYTES),
            Depends(exact_query()),
        ],
        summary="Fija los umbrales del panel de flota de la planta",
    )
    async def put_plant_fleet_thresholds(
        plant_id: uuid.UUID,
        body: FleetThresholdsBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> FleetThresholdsView:
        _no_store(response)
        try:
            saved = await installed(services.thresholds).put_thresholds(
                request_context(request),
                plant_id,
                queue_pending_threshold=body.queue_pending_threshold,
                queue_age_threshold_minutes=body.queue_age_threshold_minutes,
                clock_drift_threshold_ms=body.clock_drift_threshold_ms,
            )
        except ThresholdInvalid:
            raise ApiError(
                ApiErrorCode.INVALID_REQUEST, detail_code=FleetDetailCode.THRESHOLD_INVALID.value
            ) from None
        return _view(saved)

    return router
