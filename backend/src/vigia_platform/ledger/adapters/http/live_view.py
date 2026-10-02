"""``POST /zones/{zone_id}/live-view-token`` (``live_view.open``; BR-NUC-88 a 90; §9 y §10.2).

``LiveViewTokenPort.emitir_token_vista`` autoriza sobre la zona (``coordinator_sst``,
``administrator``, ``copasst`` y el instalador del proveedor bajo concesión), exige el nodo vigente
de la zona, aplica el límite exacto de 30 emisiones por usuario en 10 minutos, firma el token de
10 minutos con la clave ``live_view_token`` y audita ``live_view_token_issued``. La respuesta es
``{token, live_view_local_url, expires_at}``; ``live_view_local_url`` es nula si el nodo no la ha
anunciado (pendiente nº 31) y la aplicación lo explica. El token nunca se registra ni se guarda.

- Zona inexistente o fuera de alcance: ``not_found``.
- Zona sin nodo vigente: ``zone_without_node``.
- 30 o más emisiones del usuario en la ventana: ``rate_limited`` con ``retry_after_seconds``.
- **Ráfaga del mismo usuario** (seguimiento de VIG-80): la emisión toma una exclusión por usuario;
  si la espera supera ``lock_timeout`` es porque otra emisión del **mismo** usuario la tiene, así
  que se responde ``rate_limited`` (reintento en 1 s) y no ``temporarily_unavailable``. Nunca se
  pasa de 30.
- Clave de firma no disponible: ``temporarily_unavailable``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    LedgerHttp,
    exact_query,
    ledger_http,
    no_store,
    request_context,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.db import ChainLockedTimeout
from vigia_platform.shared.signing.service import SigningKeyUnavailable, SigningNotReady
from vigia_platform.shared.tokens import LiveViewRejection, LiveViewTokenRejected

__all__ = ["BURST_RETRY_AFTER_SECONDS", "LiveViewTokenOut", "live_view_router"]

BURST_RETRY_AFTER_SECONDS = 1
"""``retry_after_seconds`` de una ráfaga que agotó la espera de la exclusión del usuario."""

Services = Annotated[LedgerHttp, Depends(ledger_http)]


class LiveViewTokenOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token: str
    live_view_local_url: str | None
    expires_at: str


def live_view_router() -> APIRouter:
    router = APIRouter(tags=["vista en vivo"])

    @router.post(
        "/zones/{zone_id}/live-view-token",
        dependencies=[requires(PermissionKey.LIVE_VIEW_OPEN.value), Depends(exact_query())],
        summary="Token de 10 minutos para la vista en vivo difuminada de la zona",
    )
    async def live_view_token(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> LiveViewTokenOut:
        no_store(response)
        context = request_context(request)
        try:
            issued = await services.live_view.issue(context, zone_id)
        except LiveViewTokenRejected as rejection:
            if rejection.code is LiveViewRejection.ZONE_WITHOUT_NODE:
                raise ApiError(ApiErrorCode.ZONE_WITHOUT_NODE) from None
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=rejection.retry_after_seconds
            ) from None
        except ChainLockedTimeout:
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=BURST_RETRY_AFTER_SECONDS
            ) from None
        except (SigningKeyUnavailable, SigningNotReady):
            raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE) from None
        body = issued.to_response()
        return LiveViewTokenOut(
            token=issued.token,
            live_view_local_url=issued.live_view_local_url,
            expires_at=str(body["expires_at"]),
        )

    return router
