"""``POST /evidence/{evidence_id}/read-url`` (``evidence.read``; BR-NUC-66; §10.2).

La única forma de leer un clip: ``EvidencePort.url_lectura`` con el contexto de la sesión. Devuelve
``{evidence_id, url, expires_at}`` con una URL prefirmada de **solo lectura**, fijada a la versión
verificada y vigente a lo sumo 5 minutos; cada concesión queda auditada como
``evidence_read_granted`` antes de que la URL salga del servicio. No existe listado de objetos ni
URL permanente.

- Evidencia inexistente, de otra organización o fuera del alcance de ``evidence.read``:
  ``not_found`` (auditada como ``denied``), nunca ``forbidden``.
- Objeto ausente, con otros bytes que los verificados o sin versión en el almacén: ``conflict``
  (auditada como ``error``): la plataforma no sirve bytes que no son los del expediente.
- Almacén caído: ``storage_unavailable`` con ``retry_after_seconds``.

``POST`` y no ``GET``: cada llamada concede un acceso nuevo y auditado; la respuesta lleva
``Cache-Control: no-store`` (la URL es un secreto de corta vida y nunca se registra).
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
from vigia_platform.ledger.application.evidence_read import (
    EvidenceNotFound,
    EvidenceQueryInvalid,
    EvidenceUnreadable,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["EvidenceReadUrlResponse", "evidence_router"]

Services = Annotated[LedgerHttp, Depends(ledger_http)]


class EvidenceReadUrlResponse(BaseModel):
    """``EvidenceReadGrant`` (domain-entities §3.5): no se persiste."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: uuid.UUID
    url: str
    expires_at: str


def evidence_router() -> APIRouter:
    router = APIRouter(tags=["evidencias"])

    @router.post(
        "/evidence/{evidence_id}/read-url",
        dependencies=[requires(PermissionKey.EVIDENCE_READ.value), Depends(exact_query())],
        summary="URL de solo lectura de un clip, vigente 5 minutos (concesión auditada)",
    )
    async def read_url(
        evidence_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> EvidenceReadUrlResponse:
        no_store(response)
        context = request_context(request)
        try:
            grant = await services.evidence.url_lectura(context, evidence_id)
        except EvidenceNotFound:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        except EvidenceUnreadable:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except EvidenceQueryInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return EvidenceReadUrlResponse(
            evidence_id=grant.evidence_id,
            url=grant.url,
            expires_at=format_timestamp(grant.expires_at),
        )

    return router
