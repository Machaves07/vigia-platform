"""La ruta de la clasificación (SUP-U05-03; LC-LAZ §6.3; LC-LAZ-07).

``POST /loop/findings/{finding_state_id}/classifications`` (``findings.classify`` sobre la zona del
sujeto): ``outcome`` (``confirmed``, ``authorized_operation`` o ``false_positive``),
``reason_category`` de la lista de esa salida, ``reason_text`` de 20 a 1000 caracteres y
``reinforced_reason`` (obligatorio y verdadero en toda reclasificación). ``201`` con el recibo,
el estado nuevo y el efecto sobre el lazo.

Rechazos de negocio, como ``invalid_request`` con su ``detail_code`` (400, A-68):
``loop_reason_required``, ``loop_reason_too_short``, ``loop_blocked_vocabulary_term`` y
``loop_person_attribution_detected``; ``loop_transition_not_allowed`` viaja como ``conflict``
(409). Un cuerpo que no tiene la forma del esquema (salida desconocida, categoría de otra lista,
motivo de más de 1000 caracteres, campos de más), ``invalid_request`` sin ``detail_code``. Un
sujeto inexistente, de otra organización, fuera del alcance o pedido bajo concesión de proveedor
responde ``not_found``.

En una zona de ocupación unipersonal, ``classified_at`` es el inicio de la ventana de agregación
(``time_aggregated`` verdadero); el expediente conserva la marca exacta (BR-LAZ-06).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.classification import (
    ClassificationRejected,
    ClassificationRequestInvalid,
    ClassificationResult,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["PATH", "ClassificationBody", "ClassificationOut", "classification_router"]

PATH: Final = "/loop/findings/{finding_state_id}/classifications"
_CLASSIFY: Final = PermissionKey.FINDINGS_CLASSIFY.value
_DETAIL_CODES: Final = (
    LoopDetailCode.REASON_REQUIRED.value,
    LoopDetailCode.REASON_TOO_SHORT.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class ClassificationBody(BaseModel):
    """La decisión: salida, categoría, motivo y, al reclasificar, el motivo reforzado."""

    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: StrictStr | None = None
    """``confirmed``, ``authorized_operation`` o ``false_positive``."""
    reason_category: StrictStr | None = None
    """De la lista cerrada de la salida elegida (DE §1)."""
    reason_text: StrictStr | None = None
    """Motivo de 20 a 1000 caracteres; pasa los dos validadores de texto de U-04."""
    reinforced_reason: StrictBool = False
    """Verdadero en toda reclasificación (la segunda y posteriores sobre el sujeto)."""


class ClassificationOut(BaseModel):
    finding_state_id: uuid.UUID
    classification_id: uuid.UUID
    record_id: uuid.UUID
    sequence_in_subject: int
    outcome: str
    reason_category: str
    is_reclassification: bool
    reinforced_reason: bool
    effect_on_loop: str
    lifecycle_state: str
    classification_count: int
    closed_with_later_different_classification: bool
    classified_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool


def view(result: ClassificationResult) -> ClassificationOut:
    return ClassificationOut(
        finding_state_id=result.finding_state_id,
        classification_id=result.classification_id,
        record_id=result.record_id,
        sequence_in_subject=result.sequence_in_subject,
        outcome=result.outcome.value,
        reason_category=result.reason_category,
        is_reclassification=result.is_reclassification,
        reinforced_reason=result.reinforced_reason,
        effect_on_loop=result.effect_on_loop.value,
        lifecycle_state=result.lifecycle_state.value,
        classification_count=result.classification_count,
        closed_with_later_different_classification=(
            result.closed_with_later_different_classification
        ),
        classified_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
    )


def classification_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        PATH,
        status_code=201,
        dependencies=[requires(_CLASSIFY, detail_codes=_DETAIL_CODES), Depends(exact_query())],
        summary="Clasifica o reclasifica un hallazgo con motivo validado (registro nuevo)",
    )
    async def classify(
        finding_state_id: uuid.UUID,
        body: ClassificationBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ClassificationOut:
        no_store(response)
        try:
            result = await installed(services.classification).classify(
                request_context(request),
                finding_state_id,
                outcome=body.outcome,
                reason_category=body.reason_category,
                reason_text=body.reason_text,
                reinforced_reason=body.reinforced_reason,
            )
        except ClassificationRejected as rejected:
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
            raise ApiError(
                code, detail_code=rejected.detail_code.value, rejection=rejected.hint
            ) from None
        except ClassificationRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return view(result)

    return router
