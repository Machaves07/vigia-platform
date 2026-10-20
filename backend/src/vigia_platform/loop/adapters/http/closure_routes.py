"""Las rutas del cierre y de la reapertura (SUP-U05-11 y 14; LC-LAZ §6.5; LC-LAZ-09; VIG-233).

- ``POST /loop/findings/{finding_state_id}/closure`` (``findings.close_and_sign`` sobre la zona
  del hallazgo): ``closure_description`` (20 a 2000), ``verification_note`` (opcional, hasta
  1000) y ``professional_license`` (declarada, no verificada externamente; ausente o en blanco,
  ``signer_license_missing``). Solo desde ``action_assigned`` u ``overdue`` y con al menos una
  entrada de evidencia de la acción vigente; si no, ``closure_requires_assigned_action`` o
  ``closure_evidence_missing``. ``201`` con el cierre y el estado del hallazgo.
- ``POST /loop/findings/{finding_state_id}/reopening`` (``actions.manage`` sobre la zona del
  hallazgo): ``reason_text`` (20 a 2000) y la misma acción correctiva completa que
  ``POST /loop/findings/{id}/actions`` (SUP-U05-07). Solo desde ``closed`` o ``unclosable``; sin
  motivo, ``reopening_reason_required``; desde cualquier otro estado, ``transition_not_allowed``.
  ``201`` con la reapertura, el enlace con lo que se reabre y la acción nueva.

Rechazos de negocio, como ``invalid_request`` con su ``detail_code`` (400, A-68):
``loop_closure_evidence_missing``, ``loop_signer_license_missing``,
``loop_reopening_reason_required``, ``loop_action_type_not_allowed``,
``loop_action_on_person_forbidden``, ``loop_deadline_out_of_range``,
``loop_blocked_vocabulary_term`` y ``loop_person_attribution_detected``;
``loop_closure_requires_assigned_action`` y ``loop_transition_not_allowed`` viajan como
``conflict`` (409). Un cuerpo que no tiene la forma del esquema es ``invalid_request`` sin
``detail_code``. Un hallazgo inexistente, de otra organización, fuera de alcance o pedido bajo
concesión de proveedor responde ``not_found``, sin escribir nada.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.actions import (
    DESCRIPTION_MAX_CHARS,
    DESCRIPTION_MIN_CHARS,
    EXPECTED_OUTCOME_MAX_CHARS,
    ActionRejected,
    ActionRequestInvalid,
)
from vigia_platform.loop.application.closure import (
    DESCRIPTION_MAX_CHARS as CLOSURE_DESCRIPTION_MAX_CHARS,
)
from vigia_platform.loop.application.closure import (
    DESCRIPTION_MIN_CHARS as CLOSURE_DESCRIPTION_MIN_CHARS,
)
from vigia_platform.loop.application.closure import (
    LICENSE_MAX_CHARS,
    VERIFICATION_NOTE_MAX_CHARS,
    ClosureRejected,
    ClosureRequestInvalid,
    ClosureResult,
)
from vigia_platform.loop.application.reopening import (
    ReopeningRejected,
    ReopeningRequestInvalid,
    ReopeningResult,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLOSURE_PATH",
    "REOPENING_PATH",
    "ClosureBody",
    "ClosureOut",
    "ReopeningBody",
    "ReopeningOut",
    "closure_router",
]

CLOSURE_PATH: Final = "/loop/findings/{finding_state_id}/closure"
REOPENING_PATH: Final = "/loop/findings/{finding_state_id}/reopening"
_CLOSE: Final = PermissionKey.FINDINGS_CLOSE_AND_SIGN.value
_MANAGE: Final = PermissionKey.ACTIONS_MANAGE.value
_CLOSURE_CODES: Final = (
    LoopDetailCode.CLOSURE_EVIDENCE_MISSING.value,
    LoopDetailCode.SIGNER_LICENSE_MISSING.value,
    LoopDetailCode.CLOSURE_REQUIRES_ASSIGNED_ACTION.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)
_REOPENING_CODES: Final = (
    LoopDetailCode.REOPENING_REASON_REQUIRED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
    LoopDetailCode.ACTION_TYPE_NOT_ALLOWED.value,
    LoopDetailCode.ACTION_ON_PERSON_FORBIDDEN.value,
    LoopDetailCode.DEADLINE_OUT_OF_RANGE.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClosureBody(_Strict):
    """La descripción del cierre, su nota de verificación y la licencia del firmante."""

    closure_description: StrictStr = Field(
        min_length=CLOSURE_DESCRIPTION_MIN_CHARS, max_length=CLOSURE_DESCRIPTION_MAX_CHARS
    )
    verification_note: StrictStr | None = Field(
        default=None, min_length=1, max_length=VERIFICATION_NOTE_MAX_CHARS
    )
    professional_license: StrictStr | None = Field(default=None, max_length=LICENSE_MAX_CHARS)
    """Declarada por quien firma; ausente o en blanco, ``signer_license_missing``."""


class ClosureOut(BaseModel):
    finding_state_id: uuid.UUID
    closure_id: uuid.UUID
    record_id: uuid.UUID
    action_id: uuid.UUID
    evidence_entry_ids: list[uuid.UUID]
    lifecycle_state: str
    signed_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool


class ReopeningBody(_Strict):
    """El motivo de la reapertura y la acción correctiva nueva (la de SUP-U05-07).

    ``reason_text`` es opcional a nivel de esquema (como ``reason_text`` de
    ``ClassificationBody``): ausente o en blanco no es ``invalid_request``, es
    ``reopening_reason_required`` (BR-LAZ-146bis), y solo el servicio lo distingue."""

    reason_text: StrictStr | None = None
    action_type: StrictStr = Field(max_length=64)
    action_description: StrictStr = Field(
        min_length=DESCRIPTION_MIN_CHARS, max_length=DESCRIPTION_MAX_CHARS
    )
    expected_outcome: StrictStr | None = Field(
        default=None, min_length=1, max_length=EXPECTED_OUTCOME_MAX_CHARS
    )
    assignee_user_id: uuid.UUID
    deadline_days: StrictInt | None = None


class ReopeningOut(BaseModel):
    finding_state_id: uuid.UUID
    reopening_id: uuid.UUID
    record_id: uuid.UUID
    from_state: str
    previous_closure_id: uuid.UUID | None
    previous_declaration_id: uuid.UUID | None
    supersedes_action_id: uuid.UUID | None
    reopened_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool
    action_id: uuid.UUID
    due_at: str
    lifecycle_state: str


def closure_view(result: ClosureResult) -> ClosureOut:
    return ClosureOut(
        finding_state_id=result.finding_state_id,
        closure_id=result.closure_id,
        record_id=result.record_id,
        action_id=result.action_id,
        evidence_entry_ids=list(result.evidence_entry_ids),
        lifecycle_state=result.lifecycle_state.value,
        signed_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
    )


def reopening_view(result: ReopeningResult) -> ReopeningOut:
    return ReopeningOut(
        finding_state_id=result.finding_state_id,
        reopening_id=result.reopening_id,
        record_id=result.record_id,
        from_state=result.from_state.value,
        previous_closure_id=result.previous_closure_id,
        previous_declaration_id=result.previous_declaration_id,
        supersedes_action_id=result.supersedes_action_id,
        reopened_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
        action_id=result.action.action_id,
        due_at=format_timestamp(result.action.due_at),
        lifecycle_state=result.action.lifecycle_state.value,
    )


def _closure_rejected(error: ClosureRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[error.detail_code]
    return ApiError(code, detail_code=error.detail_code.value)


def _reopening_rejected(error: ReopeningRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[error.detail_code]
    return ApiError(code, detail_code=error.detail_code.value)


def closure_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        CLOSURE_PATH,
        status_code=201,
        dependencies=[requires(_CLOSE, detail_codes=_CLOSURE_CODES), Depends(exact_query())],
        summary="Cierra (firma) un hallazgo con acción asignada y evidencia",
    )
    async def close(
        finding_state_id: uuid.UUID,
        body: ClosureBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ClosureOut:
        no_store(response)
        try:
            result = await installed(services.closure).close(
                request_context(request),
                finding_state_id,
                closure_description=body.closure_description,
                verification_note=body.verification_note,
                professional_license=body.professional_license,
            )
        except ClosureRejected as rejected:
            raise _closure_rejected(rejected) from None
        except (ActionRequestInvalid, ClosureRequestInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return closure_view(result)

    @router.post(
        REOPENING_PATH,
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_REOPENING_CODES), Depends(exact_query())],
        summary="Reabre un hallazgo cerrado o incerrable con una acción correctiva nueva",
    )
    async def reopen(
        finding_state_id: uuid.UUID,
        body: ReopeningBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ReopeningOut:
        no_store(response)
        try:
            result = await installed(services.reopening).reopen(
                request_context(request),
                finding_state_id,
                reason_text=body.reason_text,
                action_type=body.action_type,
                action_description=body.action_description,
                expected_outcome=body.expected_outcome,
                assignee_user_id=body.assignee_user_id,
                deadline_days=body.deadline_days,
            )
        except (ReopeningRejected, ActionRejected) as rejected:
            if isinstance(rejected, ReopeningRejected):
                raise _reopening_rejected(rejected) from None
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
            raise ApiError(code, detail_code=rejected.detail_code.value) from None
        except (ActionRequestInvalid, ReopeningRequestInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return reopening_view(result)

    return router
