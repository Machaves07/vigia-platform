"""Las rutas del estado SIN ALTERNATIVA SEGURA DISPONIBLE (SUP-U05-12 y 13; LC-LAZ §6.5;
LC-LAZ-09; TASK-312).

- ``POST /loop/findings/{finding_state_id}/unclosable`` (``findings.close_and_sign`` sobre la zona
  del hallazgo): los **cinco campos** (``evaluated_alternative``, ``rejection_reason``,
  ``responsible_user_id``, ``decision_date`` y ``compensatory_measure``), la ``review_due_at``
  (opcional: 6 meses por defecto, máximo 12, de la configuración de la planta) y
  ``professional_license``. ``201`` con la declaración y la política con la que se declaró.
- ``POST /loop/unclosable/{declaration_id}/reviews`` (``findings.close_and_sign`` sobre la zona de
  la declaración; con ``converted_to_action``, además ``actions.manage``): ``outcome``
  (``renewed`` o ``converted_to_action``), ``review_note`` (20 a 2000 caracteres, validada),
  ``new_review_due_at`` (solo al renovar) o ``action`` (solo al convertir: el cuerpo de una
  asignación de TASK-311) y ``professional_license``. ``201`` con la revisión y el estado nuevo.

Rechazos de negocio, como ``invalid_request`` con su ``detail_code`` (400, A-68):
``loop_unclosable_fields_incomplete``, ``loop_unclosable_policy_not_signed``,
``loop_unclosable_policy_revoked``, ``loop_unclosable_review_date_out_of_range``,
``loop_signer_license_missing``, ``loop_blocked_vocabulary_term``,
``loop_person_attribution_detected`` y los de la acción (``loop_action_type_not_allowed``,
``loop_action_on_person_forbidden``, ``loop_deadline_out_of_range``);
``loop_transition_not_allowed`` viaja como ``conflict`` (409). Con el puerto de la política sin
respuesta, ``loop_unclosable_policy_unavailable`` como ``temporarily_unavailable`` (503) y **nada
escrito** (BR-LAZ-54: la duda se resuelve cerrada). Un cuerpo que no tiene la forma del esquema es
``invalid_request`` sin ``detail_code``. Un hallazgo, una declaración o un responsable inexistente,
de otra organización o fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.actions import (
    DESCRIPTION_MAX_CHARS,
    DESCRIPTION_MIN_CHARS,
    EXPECTED_OUTCOME_MAX_CHARS,
)
from vigia_platform.loop.application.unclosable import (
    DeclarationResult,
    ReviewResult,
    UnclosableRejected,
    UnclosableRequestInvalid,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "DECLARE_PATH",
    "REVIEW_PATH",
    "UnclosableDeclarationBody",
    "UnclosableDeclarationOut",
    "UnclosableReviewBody",
    "UnclosableReviewOut",
    "unclosable_router",
]

DECLARE_PATH: Final = "/loop/findings/{finding_state_id}/unclosable"
REVIEW_PATH: Final = "/loop/unclosable/{declaration_id}/reviews"
_CLOSE_AND_SIGN: Final = PermissionKey.FINDINGS_CLOSE_AND_SIGN.value
_DECLARE_CODES: Final = (
    LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE.value,
    LoopDetailCode.UNCLOSABLE_POLICY_NOT_SIGNED.value,
    LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE.value,
    LoopDetailCode.UNCLOSABLE_POLICY_REVOKED.value,
    LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE.value,
    LoopDetailCode.SIGNER_LICENSE_MISSING.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)
_REVIEW_CODES: Final = (
    LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE.value,
    LoopDetailCode.SIGNER_LICENSE_MISSING.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
    LoopDetailCode.ACTION_TYPE_NOT_ALLOWED.value,
    LoopDetailCode.ACTION_ON_PERSON_FORBIDDEN.value,
    LoopDetailCode.DEADLINE_OUT_OF_RANGE.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnclosableDeclarationBody(_Strict):
    """Los cinco campos, la fecha de revisión y la licencia del firmante (SUP-U05-12)."""

    evaluated_alternative: StrictStr | None = None
    """Campo 1: la alternativa de ingeniería que se evaluó (20 a 2000 caracteres)."""
    rejection_reason: StrictStr | None = None
    """Campo 2: por qué se descartó (20 a 2000 caracteres)."""
    responsible_user_id: uuid.UUID | None = None
    """Campo 3: quién responde de la decisión; un usuario activo de la organización."""
    decision_date: StrictStr | None = None
    """Campo 4: la fecha de la decisión, ``AAAA-MM-DD``."""
    compensatory_measure: StrictStr | None = None
    """Campo 5: la medida compensatoria (20 a 2000 caracteres)."""
    review_due_at: StrictStr | None = None
    """Fecha de revisión; sin ella, ``unclosable_review_default_months`` de la planta (6)."""
    professional_license: StrictStr | None = None
    """Licencia profesional del firmante (hasta 64 caracteres); sin ella no hay firma."""


class ReviewActionBody(_Strict):
    """La acción nueva de la conversión (T12): el cuerpo de una asignación de TASK-311."""

    action_type: StrictStr | None = None
    """``engineering`` o ``process``."""
    action_description: StrictStr | None = Field(default=None, max_length=DESCRIPTION_MAX_CHARS)
    """Una intervención sobre la zona; pasa los dos validadores de texto de U-04."""
    expected_outcome: StrictStr | None = Field(default=None, max_length=EXPECTED_OUTCOME_MAX_CHARS)
    assignee_user_id: uuid.UUID | None = None
    """Un usuario activo de la organización con alcance sobre la zona."""
    deadline_days: StrictInt | None = None
    """Días hábiles; sin él, ``action_deadline_default_days`` de la planta."""


class UnclosableReviewBody(_Strict):
    """La revisión: renovar la declaración (T11) o convertirla en acción (T12)."""

    outcome: StrictStr | None = None
    """``renewed`` o ``converted_to_action``."""
    review_note: StrictStr | None = None
    """Nota de la revisión (20 a 2000 caracteres); pasa los dos validadores de texto."""
    new_review_due_at: StrictStr | None = None
    """Fecha nueva, solo al renovar: ``(ahora, ahora + unclosable_review_max_months]``."""
    action: ReviewActionBody | None = None
    """La acción nueva, solo al convertir (T12)."""
    professional_license: StrictStr | None = None
    """Licencia profesional del firmante; sin ella no hay revisión."""


class UnclosableDeclarationOut(BaseModel):
    finding_state_id: uuid.UUID
    declaration_id: uuid.UUID
    record_id: uuid.UUID
    review_due_at: str
    policy_id: uuid.UUID
    policy_version: int
    policy_document_sha256: str
    lifecycle_state: str
    declared_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool


class UnclosableReviewOut(BaseModel):
    declaration_id: uuid.UUID
    finding_state_id: uuid.UUID
    review_id: uuid.UUID
    record_id: uuid.UUID
    outcome: str
    new_review_due_at: str | None
    new_action_id: uuid.UUID | None
    lifecycle_state: str
    reviewed_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool


def declaration_view(result: DeclarationResult) -> UnclosableDeclarationOut:
    return UnclosableDeclarationOut(
        finding_state_id=result.finding_state_id,
        declaration_id=result.declaration_id,
        record_id=result.record_id,
        review_due_at=format_timestamp(result.review_due_at),
        policy_id=result.policy_id,
        policy_version=result.policy_version,
        policy_document_sha256=result.policy_document_sha256,
        lifecycle_state=result.lifecycle_state.value,
        declared_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
    )


def review_view(result: ReviewResult) -> UnclosableReviewOut:
    return UnclosableReviewOut(
        declaration_id=result.declaration_id,
        finding_state_id=result.finding_state_id,
        review_id=result.review_id,
        record_id=result.record_id,
        outcome=result.outcome.value,
        new_review_due_at=(
            None if result.new_review_due_at is None else format_timestamp(result.new_review_due_at)
        ),
        new_action_id=result.new_action_id,
        lifecycle_state=result.lifecycle_state.value,
        reviewed_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
    )


def _rejected(rejected: UnclosableRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
    return ApiError(code, detail_code=rejected.detail_code.value)


def _action_body(body: ReviewActionBody | None) -> dict[str, Any] | None:
    """La acción de la conversión tal cual llega (la valida el servicio de TASK-311)."""
    if body is None:
        return None
    return {
        "action_type": body.action_type,
        "action_description": body.action_description,
        "expected_outcome": body.expected_outcome,
        "assignee_user_id": body.assignee_user_id,
        "deadline_days": body.deadline_days,
    }


def unclosable_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        DECLARE_PATH,
        status_code=201,
        dependencies=[
            requires(_CLOSE_AND_SIGN, detail_codes=_DECLARE_CODES),
            Depends(exact_query()),
        ],
        summary="Declara SIN ALTERNATIVA SEGURA DISPONIBLE con los cinco campos y la política",
    )
    async def declare(
        finding_state_id: uuid.UUID,
        body: UnclosableDeclarationBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> UnclosableDeclarationOut:
        no_store(response)
        try:
            result = await installed(services.unclosable).declare(
                request_context(request),
                finding_state_id,
                evaluated_alternative=body.evaluated_alternative,
                rejection_reason=body.rejection_reason,
                responsible_user_id=body.responsible_user_id,
                decision_date=body.decision_date,
                compensatory_measure=body.compensatory_measure,
                review_due_at=body.review_due_at,
                professional_license=body.professional_license,
            )
        except UnclosableRejected as rejected:
            raise _rejected(rejected) from None
        except UnclosableRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return declaration_view(result)

    @router.post(
        REVIEW_PATH,
        status_code=201,
        dependencies=[
            requires(_CLOSE_AND_SIGN, detail_codes=_REVIEW_CODES),
            Depends(exact_query()),
        ],
        summary="Revisa una declaración de incerrable: la renueva o la convierte en acción",
    )
    async def review(
        declaration_id: uuid.UUID,
        body: UnclosableReviewBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> UnclosableReviewOut:
        no_store(response)
        try:
            result = await installed(services.unclosable).review(
                request_context(request),
                declaration_id,
                outcome=body.outcome,
                review_note=body.review_note,
                new_review_due_at=body.new_review_due_at,
                action=_action_body(body.action),
                professional_license=body.professional_license,
            )
        except UnclosableRejected as rejected:
            raise _rejected(rejected) from None
        except UnclosableRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return review_view(result)

    return router


_ = DESCRIPTION_MIN_CHARS
