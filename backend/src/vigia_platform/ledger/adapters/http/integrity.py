"""``POST /integrity/verify``, ``GET /integrity/results`` y ``GET /integrity/checkpoints``.

``integrity.verify`` (``coordinator_sst``, ``plant_manager``, ``administrator`` y, en la proveedora,
``platform_operator``); ``business-logic-model.md`` §5 y §10.2; LC-NUC-13; BR-NUC-53 a 57; H-36.

Cada cadena se autoriza como su recurso: la de una planta, con un alcance que cubra esa planta; la
de la organización y la de auditoría, con alcance de organización (``identity.authz.decide``).

- ``POST /integrity/verify`` con ``{"kind": "ledger" | "audit", "plant_id": uuid | null}``: la
  verificación a demanda corre **en el worker** (``IntegrityRequests`` publica
  ``integrity_verification_requested``; el consumidor ``integrity_on_demand`` la ejecuta en modo
  ``on_demand`` y la audita). Responde ``202`` con el identificador de la petición; el resultado se
  consulta en ``GET /integrity/results``. Una cadena que no existe o fuera de alcance:
  ``not_found`` (auditado como ``authorization_denied`` si es por alcance).
- ``GET /integrity/results``: el último resultado de cada cadena visible (``intact`` o ``broken``
  con la primera secuencia rota y su registro).
- ``GET /integrity/checkpoints``: el último punto de control firmado de cada cadena visible, con
  lo necesario para que un verificador sin red compruebe la firma con las claves públicas de
  ``/.well-known/vigia-checkpoint-keys`` y lo use como «paquete anterior» (H-36).

Nada de esto lleva contenido de registros: cadenas, secuencias, hashes y firmas.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.authorize import Resource, decide
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    LedgerHttp,
    exact_query,
    ledger_http,
    no_store,
    provider_access,
    request_context,
)
from vigia_platform.ledger.application.integrity_requests import ChainNotFound
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain, StoredCheckpoint
from vigia_platform.ledger.chain.verify import IntegrityResult
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CheckpointsOut",
    "IntegrityResultsOut",
    "VerificationAccepted",
    "VerifyRequest",
    "integrity_router",
]

Services = Annotated[LedgerHttp, Depends(ledger_http)]
_KEY = PermissionKey.INTEGRITY_VERIFY


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChainOut(_Strict):
    kind: Literal["ledger", "audit"]
    plant_id: uuid.UUID | None


class VerifyRequest(_Strict):
    """La cadena a verificar: ``ledger`` con ``plant_id`` (planta) o sin él (organización), o
    ``audit`` (sin planta). Sin campos de más; ``plant_id`` es un UUID en texto."""

    kind: Literal["ledger", "audit"]
    plant_id: uuid.UUID | None = None


class VerificationAccepted(_Strict):
    request_id: uuid.UUID
    chain: ChainOut
    status: Literal["queued"]
    requested_at: str


class ResultOut(_Strict):
    chain: ChainOut
    mode: str
    result: str
    from_sequence: int
    to_sequence: int
    head_sequence: int
    broken_sequence: int | None
    broken_entry_id: uuid.UUID | None
    reason: str | None
    verified_at: str | None


class IntegrityResultsOut(_Strict):
    results: tuple[ResultOut, ...]


class CheckpointOut(_Strict):
    chain: ChainOut
    sequence: int
    entry_id: uuid.UUID
    entry_hash: str
    covered_sequence: int
    covered_hash: str
    taken_at: str
    key_id: str
    signature: str


class CheckpointsOut(_Strict):
    organization_id: uuid.UUID
    checkpoints: tuple[CheckpointOut, ...]


def _chain_out(chain: CheckpointChain) -> ChainOut:
    kind: Literal["ledger", "audit"] = "audit" if chain.kind is ChainKind.AUDIT else "ledger"
    return ChainOut(kind=kind, plant_id=chain.plant_id)


def _resource(context: ScopeContext, chain: CheckpointChain) -> Resource:
    if chain.plant_id is not None:
        return Resource.plant(context.organization_id, chain.plant_id)
    return Resource.organization(context.organization_id)


def _visible(services: LedgerHttp, context: ScopeContext, chain: CheckpointChain) -> bool:
    decision = decide(
        context,
        _KEY,
        _resource(context, chain),
        provider_organization_id=services.provider_organization_id,
    )
    return decision.granted


def _result_out(result: IntegrityResult) -> ResultOut:
    return ResultOut(
        chain=_chain_out(result.chain),
        mode=result.mode.value,
        result=result.status.value,
        from_sequence=result.from_sequence,
        to_sequence=result.to_sequence,
        head_sequence=result.head_sequence,
        broken_sequence=result.broken_sequence,
        broken_entry_id=result.broken_entry_id,
        reason=result.reason,
        verified_at=None if result.verified_at is None else format_timestamp(result.verified_at),
    )


def _checkpoint_out(checkpoint: StoredCheckpoint) -> CheckpointOut:
    content = checkpoint.content
    return CheckpointOut(
        chain=_chain_out(checkpoint.chain),
        sequence=checkpoint.sequence,
        entry_id=checkpoint.entry_id,
        entry_hash=checkpoint.entry_hash,
        covered_sequence=content.covered_sequence,
        covered_hash=content.covered_hash,
        taken_at=content.taken_at,
        key_id=content.key_id,
        signature=content.signature,
    )


def integrity_router() -> APIRouter:
    router = APIRouter(tags=["integridad"])

    @router.post(
        "/integrity/verify",
        status_code=202,
        dependencies=[requires(_KEY.value), Depends(exact_query())],
        summary="Pide verificar una cadena a demanda; la verificación corre en el worker",
    )
    async def verify(
        body: VerifyRequest, request: Request, response: Response, services: Services
    ) -> VerificationAccepted:
        no_store(response)
        context = request_context(request)
        if body.kind == "audit" and body.plant_id is not None:
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        chain = CheckpointChain(ChainKind(body.kind), body.plant_id)
        # Audita authorization_denied y responde not_found si la cadena no está a su alcance.
        authorized = await services.authorizer.authorize(context, _KEY, _resource(context, chain))
        try:
            accepted = await services.integrity_requests.request(authorized, chain)
        except ChainNotFound:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        await provider_access(request, services, context, "write")
        return VerificationAccepted(
            request_id=accepted.request_id,
            chain=_chain_out(chain),
            status="queued",
            requested_at=accepted.requested_at,
        )

    @router.get(
        "/integrity/results",
        dependencies=[requires(_KEY.value), Depends(exact_query())],
        summary="Último resultado de la verificación de cada cadena a su alcance",
    )
    async def results(
        request: Request, response: Response, services: Services
    ) -> IntegrityResultsOut:
        no_store(response)
        context = request_context(request)
        found = await services.integrity_results.last_results(context)
        visible = tuple(r for r in found if _visible(services, context, r.chain))
        await provider_access(request, services, context, "read")
        return IntegrityResultsOut(results=tuple(_result_out(r) for r in visible))

    @router.get(
        "/integrity/checkpoints",
        dependencies=[requires(_KEY.value), Depends(exact_query())],
        summary="Último punto de control firmado de cada cadena a su alcance",
    )
    async def checkpoints(
        request: Request, response: Response, services: Services
    ) -> CheckpointsOut:
        no_store(response)
        context = request_context(request)
        found = await services.checkpoints.latest_checkpoints(context)
        visible = tuple(c for c in found if _visible(services, context, c.chain))
        await provider_access(request, services, context, "read")
        return CheckpointsOut(
            organization_id=context.organization_id,
            checkpoints=tuple(_checkpoint_out(c) for c in visible),
        )

    return router
