"""La imagen de la foto del hallazgo (TASK-589; A-128 §6; ``loop.application.finding_photo``).

``GET /loop/findings/{finding_state_id}/photos/{photo_id}/image``: el JPEG difuminado de una foto
de uno de los registros del hallazgo, del mismo origen que la aplicación (la CSP es
``img-src 'self'``; la web la pide con ``fetch`` y la pinta en un ``<canvas>``, TASK-590), con
``Content-Type: image/jpeg``, ``Cache-Control: private, no-store``,
``X-Content-Type-Options: nosniff`` (de la cadena), ``Cross-Origin-Resource-Policy: same-origin``
y **sin** ``Content-Disposition`` de descarga.

``findings.read`` en la cadena (la clave de ``SUP-U05-02``, de la que es otra lectura: PR-LAZ-62
solo admite claves de U-04 en ``/loop/*``) y, en el servicio, ``findings.read`` sobre la zona del
hallazgo para verlo y ``evidence.read`` sobre esa zona para la imagen, como ``POST
/evidence/{evidence_id}/read-url``. Hallazgo invisible, foto que no es de uno de sus registros,
``commissioning``, vencida o sin imagen: ``not_found`` (nunca ``forbidden``, BR-NUC-09). Bytes
que no tienen su ``sha256``: ``internal_error`` sin cuerpo de imagen. Sin compuerta legible: 503
como el detalle.

**Una** entrada ``finding_photo_read`` por petición, también la que la cadena deniega antes de la
ruta (sin ``findings.read``): de esa se encarga ``_gate_audit``, declarada antes que ``requires``,
que ve la denegación de la cadena y escribe ``denied`` solo si la ruta no llegó a correr.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response

from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query
from vigia_platform.loop.adapters.http.services import (
    LOOP_STATE_KEY,
    LoopHttp,
    installed,
    loop_http,
)
from vigia_platform.loop.application.detail import FindingGateUnavailable
from vigia_platform.loop.application.finding_photo import FindingPhotoIntegrityFailed
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context, request_session

__all__ = [
    "IMAGE_HEADERS",
    "IMAGE_PATH",
    "JPEG_MEDIA_TYPE",
    "finding_photo_router",
]

IMAGE_PATH: Final = "/loop/findings/{finding_state_id}/photos/{photo_id}/image"
JPEG_MEDIA_TYPE: Final = "image/jpeg"
IMAGE_HEADERS: Final = {
    "Cache-Control": "private, no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
"""Cabeceras propias de la imagen; ``X-Content-Type-Options: nosniff`` la fija la cadena en toda
respuesta. Sin ``Content-Disposition``: la imagen no se ofrece como descarga."""
_READ: Final = PermissionKey.FINDINGS_READ.value
_GATE_UNAVAILABLE: Final = LoopDetailCode.GATE_STATE_UNAVAILABLE

Services = Annotated[LoopHttp, Depends(loop_http)]


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
        services = getattr(request.app.state, LOOP_STATE_KEY, None)
        reader = None if not isinstance(services, LoopHttp) else services.finding_photos
        if not tracker.reached and session is not None and reader is not None:
            await reader.gate_denied(session.context, photo_id)
        raise


ImageRequest = Annotated[_ImageRequest, Depends(_gate_audit)]


def finding_photo_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        IMAGE_PATH,
        dependencies=[
            Depends(_gate_audit),
            requires(_READ, detail_codes=(_GATE_UNAVAILABLE.value,)),
            Depends(exact_query()),
        ],
        response_class=Response,
        responses={
            200: {
                "description": (
                    "JPEG difuminado de la foto del hallazgo (mismo origen, private, no-store); la "
                    "sha256 se verifica antes de servirlo"
                ),
                "content": {JPEG_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
            }
        },
        summary="Imagen difuminada de una foto del hallazgo (auditada en cada petición)",
    )
    async def finding_photo_image(
        finding_state_id: uuid.UUID,
        photo_id: uuid.UUID,
        request: Request,
        services: Services,
        image_request: ImageRequest,
    ) -> Response:
        image_request.reached = True
        reader = installed(services.finding_photos)
        try:
            served = await reader.image(request_context(request), finding_state_id, photo_id)
        except FindingGateUnavailable:
            # Fallo cerrado (BR-LAZ-12): solo el error, ningún byte.
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[_GATE_UNAVAILABLE]
            raise ApiError(code, detail_code=_GATE_UNAVAILABLE.value) from None
        except FindingPhotoIntegrityFailed:
            raise ApiError(ApiErrorCode.INTERNAL_ERROR) from None
        return Response(content=served.data, media_type=JPEG_MEDIA_TYPE, headers=IMAGE_HEADERS)

    return router
