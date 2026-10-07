"""Ruta de la prueba de oclusión (SCR-06; interfaces §3.3 v1.3; LC-GOB-07).

``POST /walk-tests/{session_id}/occlusion-tests`` (``commissioning.run`` sobre la zona de la
sesión): ``{camera_id, started_at, ended_at, declared_reason_es?}``. ``201`` con ``{test_id,
verification, deadline, correlated_event_ids[]}`` y el resto de la prueba (``camera_id``, la
ventana, ``declared_reason_es`` y ``failure_reason``). La prueba nace ``pending``
(``deadline = ended_at + 5 min``) y se evalúa en el acto y cada vez que se consulta la sesión
(``GET /zones/{zone_id}/walk-tests/current``) o se cierra el acta.

- Cámara ajena al catálogo de la sesión o ventana fuera de ``started_at < ended_at ≤ ahora`` (a
  lo sumo una hora): ``invalid_request``.
- Prueba nueva cuando la última de la cámara no quedó ``failed``, o declaración con eventos
  contados, vencida la fecha límite o sobre una prueba resuelta: ``conflict``.
- Sesión ``incomplete``: ``catalog_walk_test_incomplete``; ``closed``: ``conflict``; motivo que
  no pasa la política de texto libre: ``catalog_free_text_rejected``.

Un recurso inexistente, de otra organización o fuera del alcance responde ``not_found``. La ruta
no acepta filtro, orden ni parámetro de consulta (``exact_query``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, StrictStr

from vigia_platform.catalog.adapters.http.catalog import Strict
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.occlusion import occlusion_test_json
from vigia_platform.catalog.application.walk_test import WalkTestConflict, WalkTestRequestInvalid
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context

__all__ = ["OcclusionTestOut", "occlusion_router"]

_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_DETAIL_CODES: Final = (
    CatalogDetailCode.WALK_TEST_INCOMPLETE.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class OcclusionTestBody(Strict):
    camera_id: uuid.UUID
    started_at: AwareDatetime
    ended_at: AwareDatetime
    declared_reason_es: StrictStr | None = None


class OcclusionTestOut(Strict):
    """``OcclusionTest`` (DE §2.14 y su nota): ``verification`` distingue los cuatro valores."""

    test_id: uuid.UUID
    camera_id: uuid.UUID
    started_at: str
    ended_at: str
    deadline: str
    """``ended_at`` + 5 min: hasta entonces se esperan los eventos del nodo."""
    verification: OcclusionVerification
    correlated_event_ids: list[uuid.UUID]
    declared_reason_es: str | None
    failure_reason: str | None
    """Con ``failed``: ``no_observability_events_in_window`` o ``redundancy_not_verified``."""
    recorded_by: uuid.UUID


def occlusion_router() -> APIRouter:
    router = APIRouter(tags=["comisionamiento"])

    @router.post(
        "/walk-tests/{session_id}/occlusion-tests",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_DETAIL_CODES), Depends(exact_query())],
        summary="Registra la prueba de oclusión de una cámara o declara la pendiente con motivo",
    )
    async def record_occlusion_test(
        session_id: uuid.UUID,
        body: OcclusionTestBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> OcclusionTestOut:
        no_store(response)
        try:
            test = await installed(services.occlusions).record(
                request_context(request),
                session_id,
                body.camera_id,
                body.started_at,
                body.ended_at,
                body.declared_reason_es,
            )
        except CatalogRejected as error:
            raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return OcclusionTestOut.model_validate(occlusion_test_json(test))

    return router
