"""Las rutas del asistente de causa raíz (SUP-U05-19 y 20; LC-LAZ §6.6; LC-LAZ-13; TASK-316).

Las dos exigen ``root_cause.analyze`` sobre la zona del análisis (coordinación SST):

- ``POST /loop/root-cause-analyses/{analysis_id}/assistant-requests``: ``202`` con la solicitud
  ``pending``; el flujo guiado continúa sin esperarla. Con el cortacircuito del asistente
  abierto, o con el tope mensual de fichas de la organización agotado, ``503
  temporarily_unavailable`` con ``loop_assistant_unavailable`` **de inmediato**, sin llamada
  externa y sin crear nada (``Retry-After``: el intervalo de sonda con el circuito abierto, una
  hora con el tope de fichas agotado).
  Por encima de 20 por hora y usuario o 200 por día y organización, ``429 rate_limited`` con
  ``Retry-After``.
- ``GET /loop/assistant-requests/{request_id}``: ``assistant_state`` (``pending``, ``ready`` o
  ``unavailable`` con ``unavailable_cause``: ``no_api_key``, ``circuit_open``, ``timeout``,
  ``provider_error`` o ``budget_exceeded``) y, si está lista, el **borrador marcado como tal**
  (``is_draft``) con sus citas (alias de sesión, hecho citado y la referencia real si la hay) y el
  modelo **de la respuesta**. Una salida que no pasó los validadores responde ``400
  invalid_request`` con ``loop_assistant_output_rejected`` (A-68): su texto nunca se guardó. Si
  la rechazó uno de los dos validadores, lleva ``rejection.code`` (``blocked_vocabulary_term`` o
  ``person_attribution_detected``, VIG-344), sin término ni sugerencia.

Una solicitud o un análisis inexistente, de otra organización, fuera del alcance o pedido bajo
concesión de proveedor responde ``not_found``. ``Cache-Control: no-store``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.ledger.free_text import FreeTextHint
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.assistant.service import (
    AssistantOutputRejected,
    AssistantRateLimited,
    AssistantRequestView,
    AssistantUnavailable,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AssistantUnavailableCause
from vigia_platform.loop.validators.attribution import PERSON_ATTRIBUTION_DETECTED
from vigia_platform.loop.validators.vocabulary import BLOCKED_VOCABULARY_TERM
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import MAX_RETRY_AFTER_SECONDS, ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.outbox.breaker import PROBE_INTERVAL
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "REQUEST_PATH",
    "STATUS_PATH",
    "AssistantRequestOut",
    "assistant_router",
]

REQUEST_PATH: Final = "/loop/root-cause-analyses/{analysis_id}/assistant-requests"
STATUS_PATH: Final = "/loop/assistant-requests/{request_id}"
_ANALYZE: Final = PermissionKey.ROOT_CAUSE_ANALYZE.value
_VALIDATOR_REASONS: Final = frozenset({BLOCKED_VOCABULARY_TERM, PERSON_ATTRIBUTION_DETECTED})
"""Los motivos que se muestran: los de los dos validadores de U-04 (BR-APP-49); son los mismos
valores que ``RejectionReason`` del consumidor (una prueba lo exige)."""

Services = Annotated[LoopHttp, Depends(loop_http)]


class CitationOut(BaseModel):
    kind: str
    """``record``, ``clip``, ``metric`` o ``coverage``."""
    id: str
    """El alias de sesión que citó el borrador."""
    quoted_fact_es: str
    reference_id: uuid.UUID | None
    """El registro real que el alias nombra, si lo hay (el alias nunca lo reveló al proveedor)."""


class DraftOut(BaseModel):
    """El borrador del asistente: una propuesta, nunca una decisión (P8)."""

    is_draft: bool
    """Siempre ``true``: no entra al expediente sin firma humana (BR-LAZ-73)."""
    draft_text: str
    citations: list[CitationOut]
    insufficient_evidence_declared: bool
    proposed_cause_category: str | None
    model: str
    """El modelo que produjo la respuesta (puede no ser el configurado)."""
    model_version: str
    created_at: str


class AssistantRequestOut(BaseModel):
    request_id: uuid.UUID
    analysis_id: uuid.UUID
    zone_id: uuid.UUID
    assistant_state: str
    """``pending``, ``ready`` o ``unavailable``."""
    unavailable_cause: str | None
    requested_at: str
    resolved_at: str | None
    attempt_count: int
    draft: DraftOut | None


def view(result: AssistantRequestView) -> AssistantRequestOut:
    draft = result.draft
    return AssistantRequestOut(
        request_id=result.request_id,
        analysis_id=result.analysis_id,
        zone_id=result.zone_id,
        assistant_state=result.state.value,
        unavailable_cause=(
            None if result.unavailable_cause is None else result.unavailable_cause.value
        ),
        requested_at=format_timestamp(result.requested_at),
        resolved_at=None if result.resolved_at is None else format_timestamp(result.resolved_at),
        attempt_count=result.attempt_count,
        draft=(
            None
            if draft is None
            else DraftOut(
                is_draft=True,
                draft_text=draft.draft_text,
                citations=[_citation(citation) for citation in draft.citations],
                insufficient_evidence_declared=draft.insufficient_evidence_declared,
                proposed_cause_category=(
                    None
                    if draft.proposed_cause_category is None
                    else draft.proposed_cause_category.value
                ),
                model=draft.model_name,
                model_version=draft.model_version,
                created_at=format_timestamp(draft.created_at),
            )
        ),
    )


def _citation(citation: Any) -> CitationOut:
    reference = citation.get("reference_id")
    return CitationOut(
        kind=str(citation["kind"]),
        id=str(citation["id"]),
        quoted_fact_es=str(citation["quoted_fact_es"]),
        reference_id=None if reference is None else uuid.UUID(str(reference)),
    )


def _rejection(error: AssistantOutputRejected) -> FreeTextHint | None:
    """``rejection`` de una salida rechazada (VIG-344): solo el motivo, y solo si es de uno de
    los dos validadores. Ni término ni sugerencia: saldrían del texto rechazado, que nunca se
    guardó (A-68, PR-LAZ-20)."""
    if error.reason not in _VALIDATOR_REASONS:
        return None
    return FreeTextHint(code=error.reason)


def _unavailable(error: AssistantUnavailable) -> ApiError:
    """Con el circuito abierto, la espera de la sonda (60 s); con el tope de fichas agotado, la
    mayor que admite la interfaz: el tope no se libera hasta el mes siguiente."""
    retry = (
        int(PROBE_INTERVAL.total_seconds())
        if error.cause is AssistantUnavailableCause.CIRCUIT_OPEN
        else MAX_RETRY_AFTER_SECONDS
    )
    return ApiError(
        ApiErrorCode.TEMPORARILY_UNAVAILABLE,
        detail_code=LoopDetailCode.ASSISTANT_UNAVAILABLE.value,
        retry_after_seconds=retry,
    )


def assistant_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        REQUEST_PATH,
        status_code=202,
        dependencies=[
            requires(_ANALYZE, detail_codes=(LoopDetailCode.ASSISTANT_UNAVAILABLE.value,)),
            Depends(exact_query()),
        ],
        summary="Pide al asistente un borrador de hipótesis para el análisis (asíncrono)",
    )
    async def request_assistant(
        analysis_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> AssistantRequestOut:
        no_store(response)
        try:
            result = await installed(services.assistant).request(
                request_context(request), analysis_id
            )
        except AssistantUnavailable as error:
            raise _unavailable(error) from None
        except AssistantRateLimited as error:
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=error.retry_after_seconds
            ) from None
        return view(result)

    @router.get(
        STATUS_PATH,
        dependencies=[
            requires(_ANALYZE, detail_codes=(LoopDetailCode.ASSISTANT_OUTPUT_REJECTED.value,)),
            Depends(exact_query()),
        ],
        summary="Estado de una solicitud al asistente y, si está lista, su borrador con citas",
    )
    async def assistant_status(
        request_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> AssistantRequestOut:
        no_store(response)
        try:
            result = await installed(services.assistant).status(
                request_context(request), request_id
            )
        except AssistantOutputRejected as rejected:
            raise ApiError(
                ApiErrorCode.INVALID_REQUEST,
                detail_code=LoopDetailCode.ASSISTANT_OUTPUT_REJECTED.value,
                rejection=_rejection(rejected),
            ) from None
        return view(result)

    return router
