"""Regresión del walk-test de la zona (SCR-04 y SCR-07; interfaces §1.1, §3.1 y v1.5; LC-GOB-09).

- ``GET /zones/{zone_id}/regression`` (``catalog.read``): ``regression_state`` ``{state: current |
  pending, marked_at?, cause?, catalog_version?, model_version?, affected_row_ids[] | all}``. Una
  zona que nunca se marcó responde ``current``. La marca no bloquea nada: la zona sigue operando
  (BR-GOB-53).
- ``POST /zones/{zone_id}/framing-recaptures`` (``commissioning.run``; A-55): ``{camera_id,
  captured_at, reason_es}``. El instalador recapturó la línea base del par cámara-zona: marca la
  regresión con ``framing_recaptured`` y la matriz completa (nota U03-H-14), escribe
  ``walk_test_regression_marked`` con el motivo y publica ``regression_marked``. No crea versión
  del catálogo: el encuadre no es campo suyo. ``201`` con el estado nuevo. Una cámara que no está
  en el catálogo vigente de la zona o una captura futura, ``invalid_request``; una zona sin
  catálogo, ``conflict`` con ``catalog_zone_without_cameras``.

Una zona inexistente, de otra organización o fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, StrictStr

from vigia_platform.catalog.adapters.http.catalog import Strict
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.regression import (
    RegressionRequestInvalid,
    RegressionWriteFailed,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.enums import RegressionCause, RegressionState
from vigia_platform.catalog.domain.regression import ALL_ROWS, WalkTestRegression
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["RegressionOut", "regression_router", "regression_view"]

_READ: Final = PermissionKey.CATALOG_READ.value
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_FRAMING_DETAIL_CODES: Final = (
    CatalogDetailCode.ZONE_WITHOUT_CAMERAS.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class FramingRecaptureBody(Strict):
    camera_id: uuid.UUID
    captured_at: AwareDatetime
    reason_es: StrictStr


class RegressionOut(Strict):
    """``regression_state`` de la zona (``CatalogQueryPort.regression_state``)."""

    zone_id: uuid.UUID
    state: RegressionState
    marked_at: str | None
    """Primer instante del periodo pendiente."""
    cause: RegressionCause | None
    catalog_version: int | None
    model_version: str | None
    affected_row_ids: tuple[uuid.UUID, ...] | Literal["all"] | None


def regression_view(regression: WalkTestRegression) -> RegressionOut:
    pending = regression.pending
    rows = regression.affected_row_ids if pending else None
    return RegressionOut(
        zone_id=regression.zone_id,
        state=regression.state,
        marked_at=(
            format_timestamp(regression.marked_at)
            if pending and regression.marked_at is not None
            else None
        ),
        cause=regression.cause if pending else None,
        catalog_version=regression.catalog_version if pending else None,
        model_version=regression.model_version if pending else None,
        affected_row_ids=ALL_ROWS if rows == ALL_ROWS else rows,
    )


def regression_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])

    @router.get(
        "/zones/{zone_id}/regression",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Estado de la regresión del walk-test de la zona (no bloquea la operación)",
    )
    async def regression_state(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> RegressionOut:
        no_store(response)
        regression = await installed(services.regression).regression_state(
            request_context(request), zone_id
        )
        return regression_view(regression)

    @router.post(
        "/zones/{zone_id}/framing-recaptures",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_FRAMING_DETAIL_CODES), Depends(exact_query())],
        summary="Recaptura del encuadre de una cámara: regresión con la matriz completa",
    )
    async def framing_recapture(
        zone_id: uuid.UUID,
        body: FramingRecaptureBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> RegressionOut:
        no_store(response)
        try:
            regression = await installed(services.regression).mark_framing_recaptured(
                request_context(request),
                zone_id,
                body.camera_id,
                body.reason_es,
                captured_at=body.captured_at,
            )
        except CatalogRejected as error:
            raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
        except RegressionRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except RegressionWriteFailed as error:
            raise from_ledger_rejection(error.rejection) from None
        return regression_view(regression)

    return router
