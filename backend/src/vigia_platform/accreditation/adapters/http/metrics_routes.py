"""SUP-U05-30: siete métricas y sus estados por zona o planta; nunca por personas."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.accreditation.adapters.http.services import (
    AccreditationHttp,
    accreditation_http,
    installed,
)
from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.metrics.model import ProgramMetricSnapshot
from vigia_platform.accreditation.metrics.service import MetricsRequestInvalid
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context


class MetricsOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plant_id: uuid.UUID
    period: str
    zones: tuple[ProgramMetricSnapshot, ...]


def metrics_router() -> APIRouter:
    router = APIRouter(tags=["acreditación"])

    @router.get(
        "/accreditation/metrics",
        dependencies=[
            requires(
                PermissionKey.METRICS_READ.value,
                detail_codes=(AccreditationDetailCode.METRIC_NOT_COMPUTABLE.value,),
            ),
            Depends(exact_query("zone_id", "plant_id", "period")),
        ],
        summary="Métricas del programa por zona y mes, con su estado",
    )
    async def metrics(
        request: Request,
        response: Response,
        services: Annotated[AccreditationHttp, Depends(accreditation_http)],
        period: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")],
        zone_id: uuid.UUID | None = None,
        plant_id: uuid.UUID | None = None,
    ) -> MetricsOut:
        no_store(response)
        try:
            report = await installed(services.metrics).read(
                request_context(request), zone_id=zone_id, plant_id=plant_id, period=period
            )
        except MetricsRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return MetricsOut(plant_id=report.plant_id, period=report.period, zones=report.zones)

    return router
