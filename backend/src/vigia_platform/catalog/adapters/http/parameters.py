"""Las cinco rutas de parámetros del catálogo (SCR-04; interfaces-para-u04-u05 v1.5; LC-GOB-01).

Todas con ``catalog.manage`` sobre la zona y ``reason_es`` obligatorio (10 a 500, H-44). Cada una
responde ``200`` con una **versión nueva del catálogo firmado**, aplica BR-GOB-11 y 12 y deja la
regresión de la zona pendiente con la matriz completa (nota de business-rules §1, BR-GOB-52):

- ``PUT /zones/{zone_id}/cameras``: ``cameras``, 1 a 8 ``ZoneCamera`` → ``cameras``;
- ``PUT /zones/{zone_id}/minimum-coverage``: ``{required_count, required_camera_ids}`` →
  ``minimum_coverage``;
- ``PUT /zones/{zone_id}/signals``: ``signals``, 0 a 32 ``SignalDeclaration`` → ``signals``;
- ``PUT /zones/{zone_id}/thresholds``: ``{review, publication}`` con ``0 < review < publication
  ≤ 1`` → ``thresholds``;
- ``PUT /zones/{zone_id}/windows``: ``{clip_window?, episode?}``, al menos uno → ``clip_window``
  y/o ``episode``.

``ZoneCamera`` es ``{camera_id, code, role_in_zone, declared_min_fps, stream_reference}``:
``code`` es el código legible que el contrato exige en ``ZoneCatalog.cameras`` (U-02 no guarda
cámaras); ``stream_reference`` va a la configuración inicial del nodo y **nunca** al catálogo
firmado. ``declared_min_fps`` es el declarado, de 1 a 60: nunca se ajusta con la tasa medida
(BR-GOB-12). Una misma cámara puede declararse en dos zonas (una cámara que ve las dos; la
proyección ``zone_camera`` es por par zona-cámara).

``grouping_window_ms`` es 3 000 por defecto (D-11) y ``max_segment_ms`` 900 000; la ventana de
clip, 10 y 10 segundos (BR-CTR-39). Una zona sin catálogo no tiene parámetros que cambiar: su
primera versión la crea el primer estándar (``catalog_zone_without_cameras``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, StrictFloat, StrictInt, StrictStr
from vigia_contracts.models.enumerations import CameraRoleInZone

from vigia_platform.catalog.adapters.http.catalog import (
    PUBLICATION_DETAIL_CODES,
    CatalogVersionOut,
    Strict,
    published,
)
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    CatalogChange,
    SetCameras,
    SetMinimumCoverage,
    SetSignals,
    SetThresholds,
    SetWindows,
)
from vigia_platform.catalog.domain.zone_camera import MAX_CAMERAS, ZoneCamera
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context

__all__ = [
    "MAX_SIGNALS",
    "CameraBody",
    "ClipWindowBody",
    "CoverageValues",
    "EpisodeBody",
    "ThresholdValues",
    "cameras_of",
    "parameters_router",
]

_MANAGE: Final = PermissionKey.CATALOG_MANAGE.value
_DETAIL_CODES: Final = (
    *PUBLICATION_DETAIL_CODES,
    CatalogDetailCode.ZONE_WITHOUT_CAMERAS.value,
)
MAX_SIGNALS: Final = 32
"""``ZoneCatalog.signals``: de 0 a 32."""
CLIP_SECONDS_DEFAULT: Final = 10
GROUPING_WINDOW_MS_DEFAULT: Final = 3_000
MAX_SEGMENT_MS_DEFAULT: Final = 900_000

Services = Annotated[CatalogHttp, Depends(catalog_http)]


# --- Cuerpos -----------------------------------------------------------------------------------


class CameraBody(Strict):
    camera_id: uuid.UUID
    code: StrictStr
    role_in_zone: CameraRoleInZone
    declared_min_fps: StrictFloat
    stream_reference: StrictStr


class CoverageValues(Strict):
    required_count: StrictInt
    required_camera_ids: list[uuid.UUID] = Field(max_length=MAX_CAMERAS)


class ThresholdValues(Strict):
    review: Annotated[StrictFloat, Field(gt=0.0, lt=1.0)]
    publication: Annotated[StrictFloat, Field(gt=0.0, le=1.0)]


class ClipWindowBody(Strict):
    pre_seconds: StrictInt = CLIP_SECONDS_DEFAULT
    post_seconds: StrictInt = CLIP_SECONDS_DEFAULT


class EpisodeBody(Strict):
    grouping_window_ms: StrictInt = GROUPING_WINDOW_MS_DEFAULT
    max_segment_ms: StrictInt = MAX_SEGMENT_MS_DEFAULT


class CamerasBody(Strict):
    cameras: list[CameraBody] = Field(min_length=1, max_length=MAX_CAMERAS)
    reason_es: StrictStr


class MinimumCoverageBody(CoverageValues):
    reason_es: StrictStr


class SignalsBody(Strict):
    signals: list[dict[str, Any]] = Field(max_length=MAX_SIGNALS)
    """``SignalDeclaration`` del contrato; el lector estricto del contrato la valida al publicar."""
    reason_es: StrictStr


class ThresholdsBody(ThresholdValues):
    reason_es: StrictStr


class WindowsBody(Strict):
    clip_window: ClipWindowBody | None = None
    episode: EpisodeBody | None = None
    reason_es: StrictStr


def cameras_of(bodies: Sequence[CameraBody]) -> tuple[ZoneCamera, ...]:
    """Las ``ZoneCamera`` del cuerpo; una que incumple sus límites es ``invalid_request``."""
    try:
        return tuple(
            ZoneCamera(
                camera_id=body.camera_id,
                code=body.code,
                role_in_zone=body.role_in_zone,
                declared_min_fps=body.declared_min_fps,
                stream_reference=body.stream_reference,
            )
            for body in bodies
        )
    except (TypeError, ValueError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def parameters_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])
    dependencies = [requires(_MANAGE, detail_codes=_DETAIL_CODES), Depends(exact_query())]

    async def publish(
        request: Request,
        services: CatalogHttp,
        zone_id: uuid.UUID,
        change: CatalogChange,
        reason: str,
    ) -> CatalogVersionOut:
        service = installed(services.catalog)
        return await published(
            lambda: service.publish_catalog_version(
                request_context(request), zone_id, change, reason
            )
        )

    @router.put(
        "/zones/{zone_id}/cameras",
        dependencies=dependencies,
        summary="Cámaras de la zona: versión nueva del catálogo y regresión pendiente",
    )
    async def cameras(
        zone_id: uuid.UUID,
        body: CamerasBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        change = SetCameras(cameras=cameras_of(body.cameras))
        return await publish(request, services, zone_id, change, body.reason_es)

    @router.put(
        "/zones/{zone_id}/minimum-coverage",
        dependencies=dependencies,
        summary="Cobertura mínima de la zona: versión nueva del catálogo y regresión pendiente",
    )
    async def minimum_coverage(
        zone_id: uuid.UUID,
        body: MinimumCoverageBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        change = SetMinimumCoverage(
            required_count=body.required_count,
            required_camera_ids=tuple(body.required_camera_ids),
        )
        return await publish(request, services, zone_id, change, body.reason_es)

    @router.put(
        "/zones/{zone_id}/signals",
        dependencies=dependencies,
        summary="Señales declaradas de la zona: versión nueva del catálogo y regresión pendiente",
    )
    async def signals(
        zone_id: uuid.UUID,
        body: SignalsBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        change = SetSignals(signals=tuple(body.signals))
        return await publish(request, services, zone_id, change, body.reason_es)

    @router.put(
        "/zones/{zone_id}/thresholds",
        dependencies=dependencies,
        summary="Umbrales de revisión y publicación: versión nueva y regresión pendiente",
    )
    async def thresholds(
        zone_id: uuid.UUID,
        body: ThresholdsBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        change = SetThresholds(thresholds={"review": body.review, "publication": body.publication})
        return await publish(request, services, zone_id, change, body.reason_es)

    @router.put(
        "/zones/{zone_id}/windows",
        dependencies=dependencies,
        summary="Ventana de clip y parámetros de episodio: versión nueva y regresión pendiente",
    )
    async def windows(
        zone_id: uuid.UUID,
        body: WindowsBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        if body.clip_window is None and body.episode is None:
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        change = SetWindows(
            clip_window=None if body.clip_window is None else body.clip_window.model_dump(),
            episode=None if body.episode is None else body.episode.model_dump(),
        )
        return await publish(request, services, zone_id, change, body.reason_es)

    return router
