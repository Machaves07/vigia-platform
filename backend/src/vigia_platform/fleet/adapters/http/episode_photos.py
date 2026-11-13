"""Episodios de la sombra para la instaladora en SCR-06 (TASK-573; A-112; QF10; BR-GOB-30).

Las dos rutas exigen ``commissioning.run`` (en la matriz, solo ``provider_installer``, bajo
concesión vigente, A-69). Una foto o una zona inexistente, de otra organización o planta, fuera de
alcance, vencida o con la zona ya en ``productive`` responde ``not_found``, nunca ``forbidden``
(BR-NUC-09). La lógica está en ``fleet.application.episode_photo_read``.

- ``GET /zones/{zone_id}/episode-photos``: el listado por clave, la foto más reciente primero por el
  inicio del tramo (``started_at``), 50 por defecto y 200 como mucho; ``after`` es el
  ``next_after`` de la página anterior (el ``photo_id`` del último servido) y ``from``/``to``
  acotan ``started_at`` (``[from, to)``). Solo metadatos e ``image_absent_reason``: **sin bytes,
  suma ni URL**. Sin auditoría propia (bajo concesión, ``fleet_read`` como las demás lecturas del
  proveedor).
- ``GET /episode-photos/{photo_id}/image``: el JPEG difuminado, del mismo origen que la aplicación
  (la política de contenido es ``img-src 'self'``), con ``Content-Type: image/jpeg``,
  ``Cache-Control: no-store``, ``X-Content-Type-Options: nosniff`` (de la cadena) y
  ``Content-Disposition: inline``. Antes de servir se recalcula la ``sha256``: si no coincide con
  la guardada, no sale nada (``internal_error``). **Una** entrada ``episode_photo_read`` por
  petición, también la que la cadena deniega antes de la ruta (sin ``commissioning.run``): de esa
  se encarga la dependencia
  ``_gate_audit``, declarada antes que ``requires``, que ve la denegación de la cadena y escribe la
  entrada ``denied`` solo si la ruta no llegó a correr (si corrió, la escribió el servicio).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.fleet.adapters.http.services import (
    FLEET_STATE_KEY,
    FleetHttp,
    fleet_http,
    installed,
)
from vigia_platform.fleet.adapters.postgres.episode_photo_queries import EpisodePhotoSummary
from vigia_platform.fleet.application.episode_photo_read import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    EpisodePhotoIntegrityFailed,
    EpisodePhotosRequestInvalid,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store, parse_instant
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context, request_session
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "IMAGE_HEADERS",
    "JPEG_MEDIA_TYPE",
    "SERVED_HEADERS",
    "EpisodePhotoImageOut",
    "EpisodePhotoOut",
    "EpisodePhotoStandardOut",
    "EpisodePhotosOut",
    "episode_photos_router",
]

JPEG_MEDIA_TYPE: Final = "image/jpeg"
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_INSTANT_CHARS: Final = 40
_CURSOR_CHARS: Final = 36
IMAGE_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Content-Disposition": "inline",
}
"""Cabeceras propias de la imagen. ``X-Content-Type-Options: nosniff`` (y el resto de las de
seguridad) la fija la cadena en toda respuesta y sustituye cualquiera que ponga una ruta
(``shared.api.middleware.headers``): repetirla aquí no cambiaría nada."""
SERVED_HEADERS: Final = {
    "Content-Type": JPEG_MEDIA_TYPE,
    **IMAGE_HEADERS,
    "X-Content-Type-Options": "nosniff",
}
"""Las cuatro cabeceras con las que sale la imagen (las propias y las de la cadena)."""

Services = Annotated[FleetHttp, Depends(fleet_http)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EpisodePhotoStandardOut(Strict):
    standard_id: uuid.UUID
    version: int


class EpisodePhotoImageOut(Strict):
    """Lo declarado de la imagen guardada: nunca sus bytes, su suma ni una URL."""

    width: int
    height: int
    size_bytes: int
    blur_params_version: str


class EpisodePhotoOut(Strict):
    """Un episodio de la sombra: sus metadatos y la imagen o el motivo de su ausencia."""

    photo_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    episode_id: uuid.UUID
    segment_index: int
    classified_as: str
    """``finding`` o ``detection_for_review``: lo que habría sido en ``productive``."""
    family: str
    standard: EpisodePhotoStandardOut
    max_confidence: float
    started_at: str
    ended_at: str
    received_at: str
    expires_at: str
    camera_id: uuid.UUID | None
    frame_at: str | None
    image: EpisodePhotoImageOut | None
    """``null`` si la foto llegó sin imagen; entonces ``image_absent_reason`` dice por qué."""
    image_absent_reason: str | None
    """``frame_unavailable``, ``too_large``, ``disk_pressure`` o ``pending_cap`` (contrato
    v1.5.0), o ``null`` si hay imagen."""


class EpisodePhotosOut(Strict):
    photos: tuple[EpisodePhotoOut, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def _view(photo: EpisodePhotoSummary) -> EpisodePhotoOut:
    image = None
    if (
        photo.width is not None
        and photo.height is not None
        and photo.size_bytes is not None
        and photo.blur_params_version is not None
    ):
        image = EpisodePhotoImageOut(
            width=photo.width,
            height=photo.height,
            size_bytes=photo.size_bytes,
            blur_params_version=photo.blur_params_version,
        )
    return EpisodePhotoOut(
        photo_id=photo.photo_id,
        zone_id=photo.zone_id,
        node_id=photo.node_id,
        episode_id=photo.episode_id,
        segment_index=photo.segment_index,
        classified_as=photo.classified_as,
        family=photo.family,
        standard=EpisodePhotoStandardOut(
            standard_id=photo.standard_id, version=photo.standard_version
        ),
        max_confidence=photo.max_confidence,
        started_at=format_timestamp(photo.started_at),
        ended_at=format_timestamp(photo.ended_at),
        received_at=format_timestamp(photo.received_at),
        expires_at=format_timestamp(photo.expires_at),
        camera_id=photo.camera_id,
        frame_at=None if photo.frame_at is None else format_timestamp(photo.frame_at),
        image=image,
        image_absent_reason=photo.image_absent_reason,
    )


def _cursor(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if str(parsed) != value:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return parsed


class _ImageRequest:
    """Si la petición de imagen llegó a la ruta (entonces su entrada la escribe el servicio)."""

    __slots__ = ("reached",)

    def __init__(self) -> None:
        self.reached = False


async def _gate_audit(request: Request, photo_id: uuid.UUID) -> AsyncIterator[_ImageRequest]:
    """La entrada ``denied`` de una petición de imagen que la cadena deniega antes de la ruta."""
    tracker = _ImageRequest()
    try:
        yield tracker
    except ResourceNotFound:
        session = request_session(request)
        services = getattr(request.app.state, FLEET_STATE_KEY, None)
        reader = None if not isinstance(services, FleetHttp) else services.episode_photos
        if not tracker.reached and session is not None and reader is not None:
            await reader.gate_denied(session.context, photo_id)
        raise


ImageRequest = Annotated[_ImageRequest, Depends(_gate_audit)]


def episode_photos_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.get(
        "/zones/{zone_id}/episode-photos",
        dependencies=[requires(_RUN), Depends(exact_query("after", "limit", "from", "to"))],
        summary="Episodios de la sombra de la zona (metadatos sin imagen), el más reciente primero",
    )
    async def episode_photos(
        zone_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        from_: Annotated[str | None, Query(alias="from", max_length=_INSTANT_CHARS)] = None,
        to: Annotated[str | None, Query(max_length=_INSTANT_CHARS)] = None,
    ) -> EpisodePhotosOut:
        no_store(response)
        since = None if from_ is None else parse_instant(from_)
        until = None if to is None else parse_instant(to)
        try:
            page = await installed(services.episode_photos).page(
                request_context(request),
                zone_id,
                since=since,
                until=until,
                after=_cursor(after),
                limit=limit,
            )
        except EpisodePhotosRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return EpisodePhotosOut(
            photos=tuple(_view(photo) for photo in page.photos),
            next_after=None if page.next_after is None else str(page.next_after),
        )

    @router.get(
        "/episode-photos/{photo_id}/image",
        dependencies=[Depends(_gate_audit), requires(_RUN), Depends(exact_query())],
        response_class=Response,
        responses={
            200: {
                "description": (
                    "JPEG difuminado del episodio (mismo origen, no-store); la sha256 se verifica "
                    "antes de servirlo"
                ),
                "content": {JPEG_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
            }
        },
        summary="Imagen difuminada de un episodio de la sombra (auditada en cada petición)",
    )
    async def episode_photo_image(
        photo_id: uuid.UUID, request: Request, services: Services, image_request: ImageRequest
    ) -> Response:
        image_request.reached = True
        reader = installed(services.episode_photos)
        try:
            served = await reader.image(request_context(request), photo_id)
        except EpisodePhotoIntegrityFailed:
            raise ApiError(ApiErrorCode.INTERNAL_ERROR) from None
        return Response(content=served.data, media_type=JPEG_MEDIA_TYPE, headers=IMAGE_HEADERS)

    return router
