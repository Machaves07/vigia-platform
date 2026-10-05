"""``GET /zones/{zone_id}/commissioning-clips``: clips de verificación de la zona (nº 32).

Interfaces para U-04 y U-05, «Versión 1.5» (precisión (d)): ``catalog.read`` sobre la zona, con
cursor → ``VerificationClip[] {clip_id, zone_id, node_id, received_at, sha256,
blur_check_result}``, el selector de U-05 para el ``evidence_ref`` del pase. ``blur_check_result``
es ``null`` hasta que la guarda de cierre del acta lo escribe (TASK-216).

- Páginas de hasta 200, el más reciente primero; ``after`` es el ``next_after`` de la página
  anterior (el ``clip_id`` del último servido). Un cursor que no es un clip de la zona responde
  ``invalid_request``.
- Una zona inexistente, de otra organización o fuera del alcance responde ``not_found``, nunca
  ``forbidden`` (BR-NUC-09).
- Servir un clip por primera vez marca su ``first_served_at`` (TASK-216, tramo 3b). No hay URL de
  lectura ni listado de objetos del depósito (BR-GOB §11).
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.application.clip_confirmation import (
    MAX_PAGE_SIZE,
    CommissioningClipsRequestInvalid,
)
from vigia_platform.fleet.domain.verification_clip import VerificationClip
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["CommissioningClipOut", "CommissioningClipsOut", "commissioning_clips_router"]

_READ: Final = PermissionKey.CATALOG_READ.value
_CANONICAL_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Services = Annotated[FleetHttp, Depends(fleet_http)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommissioningClipOut(Strict):
    """Un ``VerificationClip`` de la zona (forma de la «Versión 1.5»)."""

    clip_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    received_at: str
    sha256: str
    blur_check_result: dict[str, Any] | None


class CommissioningClipsOut(Strict):
    clips: tuple[CommissioningClipOut, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def _view(clip: VerificationClip) -> CommissioningClipOut:
    return CommissioningClipOut(
        clip_id=clip.clip_id,
        zone_id=clip.zone_id,
        node_id=clip.node_id,
        received_at=format_timestamp(clip.received_at),
        sha256=clip.sha256,
        blur_check_result=(
            None if clip.blur_check_result is None else dict(clip.blur_check_result)
        ),
    )


def _cursor(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if _CANONICAL_UUID.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return uuid.UUID(value)


def commissioning_clips_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.get(
        "/zones/{zone_id}/commissioning-clips",
        dependencies=[requires(_READ), Depends(exact_query("after", "limit"))],
        summary="Clips de verificación del difuminado de la zona, el más reciente primero",
    )
    async def commissioning_clips(
        zone_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=36)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
    ) -> CommissioningClipsOut:
        no_store(response)
        try:
            page = await installed(services.commissioning_clips).page(
                request_context(request), zone_id, after=_cursor(after), limit=limit
            )
        except CommissioningClipsRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return CommissioningClipsOut(
            clips=tuple(_view(clip) for clip in page.clips),
            next_after=None if page.next_after is None else str(page.next_after),
        )

    return router
