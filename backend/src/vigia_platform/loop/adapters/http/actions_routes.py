"""Las rutas de la acción correctiva y de su evidencia (SUP-U05-07 y 08; LC-LAZ §6.5; LC-LAZ-09).

- ``POST /loop/findings/{finding_state_id}/actions`` (``actions.manage`` sobre la zona del
  hallazgo): ``action_type`` (``engineering`` o ``process``), ``action_description`` (20 a 2000),
  ``expected_outcome`` (opcional, hasta 1000), ``assignee_user_id``, ``deadline_days`` (opcional:
  el de la configuración de la planta) y, para **reasignar** la acción vigente,
  ``supersedes_action_id``. ``201`` con la acción, su plazo congelado y el estado del hallazgo.
- ``POST /loop/actions/{action_id}/evidence`` (``actions.manage`` sobre la zona de la acción):
  ``description`` (20 a 2000), ``performed_at``, ``performed_by_role`` (un cargo),
  ``verification_note`` (opcional) y ``attachments`` (opcionales, ya subidos con la concesión de
  SUP-U05-09). ``201`` con la entrada.

Rechazos de negocio, como ``invalid_request`` con su ``detail_code`` (400, A-68):
``loop_action_type_not_allowed``, ``loop_action_on_person_forbidden``,
``loop_deadline_out_of_range``, ``loop_blocked_vocabulary_term``,
``loop_person_attribution_detected`` y los de los adjuntos (``loop_attachment_*``);
``loop_transition_not_allowed`` viaja como ``conflict`` (409). Un cuerpo que no tiene la forma del
esquema (sin descripción estructurada, campos de más, textos fuera de sus límites, una marca que
no es la del contrato) es ``invalid_request`` sin ``detail_code``. Un hallazgo, una acción o un
responsable inexistente, de otra organización o fuera del alcance responde ``not_found``. Con
adjuntos y el almacén caído, ``503 storage_unavailable``; sin adjuntos el almacén no se toca.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.attachments.rules import (
    MAX_ATTACHMENTS_PER_FINDING,
    AttachmentRejected,
    AttachmentSchemaInvalid,
)
from vigia_platform.loop.adapters.attachments.verify import AttachmentClaim
from vigia_platform.loop.adapters.http.attachment_routes import (
    ATTACHMENTS_STATE_KEY,
    rejection_error,
    unavailable_error,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.actions import (
    DESCRIPTION_MAX_CHARS,
    DESCRIPTION_MIN_CHARS,
    EXPECTED_OUTCOME_MAX_CHARS,
    ActionRejected,
    ActionRequestInvalid,
    AssignmentResult,
)
from vigia_platform.loop.application.attachments import ClosureAttachmentService
from vigia_platform.loop.application.evidence import (
    ROLE_MAX_CHARS,
    VERIFICATION_NOTE_MAX_CHARS,
    AttachmentVerifier,
    EvidenceResult,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "ACTIONS_PATH",
    "EVIDENCE_PATH",
    "CorrectiveActionBody",
    "CorrectiveActionOut",
    "EvidenceEntryBody",
    "EvidenceEntryOut",
    "actions_router",
]

ACTIONS_PATH: Final = "/loop/findings/{finding_state_id}/actions"
EVIDENCE_PATH: Final = "/loop/actions/{action_id}/evidence"
_MANAGE: Final = PermissionKey.ACTIONS_MANAGE.value
_ACTION_CODES: Final = (
    LoopDetailCode.ACTION_TYPE_NOT_ALLOWED.value,
    LoopDetailCode.ACTION_ON_PERSON_FORBIDDEN.value,
    LoopDetailCode.DEADLINE_OUT_OF_RANGE.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)
_EVIDENCE_CODES: Final = (
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
    LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED.value,
    LoopDetailCode.ATTACHMENT_METADATA_NOT_ALLOWED.value,
    LoopDetailCode.ATTACHMENT_TOO_LARGE.value,
    LoopDetailCode.ATTACHMENT_HASH_MISMATCH.value,
    LoopDetailCode.ATTACHMENT_DECLARATION_MISSING.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CorrectiveActionBody(_Strict):
    """La acción: tipo, descripción, resultado esperado, responsable y plazo."""

    action_type: StrictStr = Field(max_length=64)
    """``engineering`` o ``process`` (``action_type``); otro valor, ``action_type_not_allowed``."""
    action_description: StrictStr = Field(
        min_length=DESCRIPTION_MIN_CHARS, max_length=DESCRIPTION_MAX_CHARS
    )
    """Una intervención sobre la zona; pasa los dos validadores de texto de U-04."""
    expected_outcome: StrictStr | None = Field(
        default=None, min_length=1, max_length=EXPECTED_OUTCOME_MAX_CHARS
    )
    assignee_user_id: uuid.UUID
    """Un usuario activo de la organización con alcance sobre la zona."""
    deadline_days: StrictInt | None = None
    """Días hábiles; sin él, ``action_deadline_default_days`` de la planta."""
    supersedes_action_id: uuid.UUID | None = None
    """La acción vigente que esta sustituye (reasignación)."""


class CorrectiveActionOut(BaseModel):
    finding_state_id: uuid.UUID
    action_id: uuid.UUID
    record_id: uuid.UUID
    action_type: str
    assignee_user_id: uuid.UUID
    deadline_days: int
    due_at: str
    supersedes_action_id: uuid.UUID | None
    lifecycle_state: str
    assigned_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool


class EvidenceAttachmentBody(_Strict):
    """Un adjunto ya subido; el tipo, el tamaño, la suma y la declaración se verifican después."""

    attachment_id: uuid.UUID
    content_type: StrictStr = Field(min_length=1, max_length=255)
    size_bytes: StrictInt = Field(ge=1)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    filename: StrictStr = Field(min_length=1, max_length=128)
    no_identifiable_person_declared: StrictBool = False
    """La declaración firmada de que no contiene persona identificable (sin ella, rechazo)."""


class EvidenceEntryBody(_Strict):
    """La descripción estructurada (obligatoria) y los adjuntos (opcionales)."""

    description: StrictStr = Field(
        min_length=DESCRIPTION_MIN_CHARS, max_length=DESCRIPTION_MAX_CHARS
    )
    """Qué se hizo."""
    performed_at: StrictStr = Field(min_length=24, max_length=24)
    """Cuándo: marca ISO 8601 UTC con milisegundos, no posterior a la entrada."""
    performed_by_role: StrictStr = Field(min_length=1, max_length=ROLE_MAX_CHARS)
    """Con qué cargo (un cargo, jamás un nombre)."""
    verification_note: StrictStr | None = Field(
        default=None, min_length=1, max_length=VERIFICATION_NOTE_MAX_CHARS
    )
    """Cómo se verificó."""
    attachments: list[EvidenceAttachmentBody] = Field(
        default_factory=list, max_length=MAX_ATTACHMENTS_PER_FINDING
    )


class EvidenceEntryOut(BaseModel):
    finding_state_id: uuid.UUID
    action_id: uuid.UUID
    evidence_entry_id: uuid.UUID
    record_id: uuid.UUID
    attachment_ids: list[uuid.UUID]
    lifecycle_state: str
    added_at: str
    performed_at: str
    time_aggregated: bool


def action_view(result: AssignmentResult) -> CorrectiveActionOut:
    return CorrectiveActionOut(
        finding_state_id=result.finding_state_id,
        action_id=result.action_id,
        record_id=result.record_id,
        action_type=result.action_type.value,
        assignee_user_id=result.assignee_user_id,
        deadline_days=result.deadline_days,
        due_at=format_timestamp(result.due_at),
        supersedes_action_id=result.supersedes_action_id,
        lifecycle_state=result.lifecycle_state.value,
        assigned_at=format_timestamp(result.presented_at),
        time_aggregated=result.time_aggregated,
    )


def evidence_view(result: EvidenceResult) -> EvidenceEntryOut:
    return EvidenceEntryOut(
        finding_state_id=result.finding_state_id,
        action_id=result.action_id,
        evidence_entry_id=result.evidence_entry_id,
        record_id=result.record_id,
        attachment_ids=list(result.attachment_ids),
        lifecycle_state=result.lifecycle_state.value,
        added_at=format_timestamp(result.presented_at),
        performed_at=format_timestamp(result.presented_performed_at),
        time_aggregated=result.time_aggregated,
    )


def _rejected(error: ActionRejected) -> ApiError:
    code, _status = HTTP_STATUS_BY_DETAIL_CODE[error.detail_code]
    return ApiError(code, detail_code=error.detail_code.value)


def _verifier(request: Request, body: EvidenceEntryBody) -> AttachmentVerifier | None:
    """``ClosureAttachmentPort`` de la aplicación (TASK-310), solo si hay adjuntos; sin él,
    ``internal_error`` (nunca se escribe una entrada con adjuntos sin verificar)."""
    if not body.attachments:
        return None
    port = getattr(request.app.state, ATTACHMENTS_STATE_KEY, None)
    if not isinstance(port, ClosureAttachmentService):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return port


def _claims(body: EvidenceEntryBody) -> tuple[AttachmentClaim, ...]:
    return tuple(
        AttachmentClaim(
            attachment_id=item.attachment_id,
            content_type=item.content_type,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            filename=item.filename,
            no_identifiable_person_declared=item.no_identifiable_person_declared,
        )
        for item in body.attachments
    )


def actions_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.post(
        ACTIONS_PATH,
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_ACTION_CODES), Depends(exact_query())],
        summary="Asigna (o reasigna) la acción correctiva de un hallazgo con plazo congelado",
    )
    async def assign(
        finding_state_id: uuid.UUID,
        body: CorrectiveActionBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CorrectiveActionOut:
        no_store(response)
        try:
            result = await installed(services.actions).assign(
                request_context(request),
                finding_state_id,
                action_type=body.action_type,
                action_description=body.action_description,
                expected_outcome=body.expected_outcome,
                assignee_user_id=body.assignee_user_id,
                deadline_days=body.deadline_days,
                supersedes_action_id=body.supersedes_action_id,
            )
        except ActionRejected as rejected:
            raise _rejected(rejected) from None
        except ActionRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return action_view(result)

    @router.post(
        EVIDENCE_PATH,
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_EVIDENCE_CODES), Depends(exact_query())],
        summary="Añade una entrada de evidencia (descripción estructurada y adjuntos verificados)",
    )
    async def add_evidence(
        action_id: uuid.UUID,
        body: EvidenceEntryBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> EvidenceEntryOut:
        no_store(response)
        try:
            result = await installed(services.evidence).add(
                request_context(request),
                action_id,
                description=body.description,
                performed_at=body.performed_at,
                performed_by_role=body.performed_by_role,
                verification_note=body.verification_note,
                attachments=_claims(body),
                verifier=_verifier(request, body),
            )
        except ActionRejected as rejected:
            raise _rejected(rejected) from None
        except AttachmentRejected as error:
            raise rejection_error(error) from None
        except (ActionRequestInvalid, AttachmentSchemaInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except StorageUnavailable as error:
            raise unavailable_error(error) from None
        return evidence_view(result)

    return router
