"""La ficha de vida del hallazgo (TASK-585; O-11 y O-12; ``loop.application.timeline``).

``GET /loop/findings/{finding_state_id}/timeline`` (``findings.read`` sobre la zona del hallazgo,
sin parámetros de consulta): la línea de tiempo completa del sujeto en **una** lectura auditada
del expediente (``ledger_detail_read``, BR-LAZ-09), con un hito por registro en orden de
expediente, la hora presentada (agregada en zona unipersonal, BR-LAZ-06), el tiempo desde el hito
anterior, quién (rol y nombre del usuario de la plataforma, solo si el registro lo trae), los
campos elegidos de cada registro y el plazo que aplica con si se cumplió; arriba, el resumen. El
hito de detección lleva el resumen de sus valores de proceso (``process``, A-129, TASK-595). Cada
evidencia y cada cierre llevan las referencias de sus adjuntos (``attachments``, TASK-609):
``attachment_id``, tipo y tamaño, sin URL, ``storage_key``, suma ni ``filename``; la URL se pide
al abrir el adjunto (``POST /loop/attachments/{attachment_id}/read-url``, auditada aparte).

Inexistente, de otra organización, fuera del alcance o en una zona sin compuerta de uso aprobada
en el instante del hecho: ``not_found``. Sin compuerta legible, **503 ``temporarily_unavailable``
con ``loop_gate_state_unavailable``**, como el detalle (fallo cerrado, BR-LAZ-12). Un tope de
``MAX_TIMELINE_MILESTONES`` hitos (``truncated``). ``Cache-Control: no-store``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.fleet.adapters.http.process_context import (
    ProcessVariableSummaryOut,
    process_summary_out,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.findings_routes import FindingPhotoOut, finding_photo_out
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.detail import FindingGateUnavailable
from vigia_platform.loop.application.timeline import (
    FindingTimeline,
    Milestone,
    MilestoneDetails,
    MilestoneKind,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["TIMELINE_PATH", "FindingTimelineOut", "timeline_router", "timeline_view"]

TIMELINE_PATH: Final = "/loop/findings/{finding_state_id}/timeline"
_READ: Final = PermissionKey.FINDINGS_READ.value
_GATE_UNAVAILABLE: Final = LoopDetailCode.GATE_STATE_UNAVAILABLE

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimelineActorOut(_Strict):
    """Quién hizo el paso: el usuario de la plataforma (P8), nunca una persona observada."""

    role_in_use: str | None
    display_name: str


class TimelineDeadlineOut(_Strict):
    due_at: str
    """El fin del plazo (presentado: agregado en zona unipersonal)."""
    met: bool | None
    """``true`` a tiempo, ``false`` fuera de plazo, ``null`` si aún no se sabe."""


class TimelineDetailsOut(_Strict):
    """Los campos elegidos del registro por tipo; los que no son de su tipo van en ``null``."""

    outcome: str | None
    reason_category: str | None
    reason_text: str | None
    action_type: str | None
    description: str | None
    expected_outcome: str | None
    assignee_display_name: str | None
    """La instantánea del responsable de la acción al asignarla (usuario de la plataforma)."""
    due_at: str | None
    performed_at: str | None
    performed_by_role: str | None
    verification_note: str | None
    days_overdue: int | None
    escalation_level: str | None
    professional_license: str | None
    root_cause_category: str | None
    root_cause_statement: str | None
    conclusion: str | None
    from_state: str | None
    compensatory_measure: str | None
    review_due_at: str | None
    review_note: str | None


class TimelineAttachmentOut(_Strict):
    """La referencia de un adjunto del cierre: lo justo para pintarlo o descargarlo a demanda."""

    attachment_id: uuid.UUID
    content_type: str
    """El MIME del registro (``image/jpeg``, ``image/png`` o ``application/pdf``): de transporte,
    sin etiqueta (A-42)."""
    size_bytes: int


class TimelineMilestoneOut(_Strict):
    kind: MilestoneKind
    record_type: str
    """El tipo del registro (el de un hito ``other``, que nunca se oculta)."""
    record_id: uuid.UUID
    chain_sequence: int
    at: str
    """La hora del expediente, agregada si la zona es unipersonal."""
    elapsed_ms_since_previous: int | None
    actor: TimelineActorOut | None
    details: TimelineDetailsOut
    deadline: TimelineDeadlineOut | None
    photos: tuple[FindingPhotoOut, ...]
    """Las fotos del tramo en ``finding_received`` y ``late_segment_attached`` (A-128), con la
    forma del detalle; vacía en los demás hitos."""
    process: tuple[ProcessVariableSummaryOut, ...]
    """En el hito de detección, una entrada por variable de proceso del registro (A-129): nombre,
    unidad, decimales, par de estados y el valor o estado en el instante del hecho con su hora
    (agregada en zona unipersonal); sin la serie. Vacía en los demás hitos."""
    attachments: tuple[TimelineAttachmentOut, ...]
    """En ``action_evidence_added``, sus adjuntos en el orden del registro; en ``closure``, los
    que cierra, sin repetir (TASK-609). Sin URL: se pide al abrir, con su propia auditoría. Vacía
    en los demás hitos."""


class TimelineSummaryOut(_Strict):
    received_at: str | None
    closed_at: str | None
    business_days_to_close: int | None
    classification_met: bool | None
    action_met: bool | None


class FindingTimelineOut(_Strict):
    finding_state_id: uuid.UUID
    subject_kind: str
    plant_id: uuid.UUID
    """La planta del hallazgo: su zona horaria para mostrar las horas (UI-25)."""
    zone_id: uuid.UUID
    family: str
    lifecycle_state: FindingLifecycleState
    time_aggregated: bool
    aggregation_window_minutes: int | None
    truncated: bool
    """Hay más hitos que el tope de la respuesta."""
    summary: TimelineSummaryOut
    milestones: tuple[TimelineMilestoneOut, ...]


def _stamp(moment: datetime) -> str:
    return format_timestamp(moment.astimezone(UTC))


def _optional(moment: datetime | None) -> str | None:
    return None if moment is None else _stamp(moment)


def _details(details: MilestoneDetails) -> TimelineDetailsOut:
    return TimelineDetailsOut(
        outcome=details.outcome,
        reason_category=details.reason_category,
        reason_text=details.reason_text,
        action_type=details.action_type,
        description=details.description,
        expected_outcome=details.expected_outcome,
        assignee_display_name=details.assignee_display_name,
        due_at=_optional(details.due_at),
        performed_at=_optional(details.performed_at),
        performed_by_role=details.performed_by_role,
        verification_note=details.verification_note,
        days_overdue=details.days_overdue,
        escalation_level=details.escalation_level,
        professional_license=details.professional_license,
        root_cause_category=details.root_cause_category,
        root_cause_statement=details.root_cause_statement,
        conclusion=details.conclusion,
        from_state=details.from_state,
        compensatory_measure=details.compensatory_measure,
        review_due_at=_optional(details.review_due_at),
        review_note=details.review_note,
    )


def _milestone(milestone: Milestone) -> TimelineMilestoneOut:
    actor, deadline = milestone.actor, milestone.deadline
    return TimelineMilestoneOut(
        kind=milestone.kind,
        record_type=milestone.record_type,
        record_id=milestone.record_id,
        chain_sequence=milestone.chain_sequence,
        at=_stamp(milestone.at),
        elapsed_ms_since_previous=milestone.elapsed_ms_since_previous,
        actor=(
            None
            if actor is None
            else TimelineActorOut(role_in_use=actor.role_in_use, display_name=actor.display_name)
        ),
        details=_details(milestone.details),
        deadline=(
            None
            if deadline is None
            else TimelineDeadlineOut(due_at=_stamp(deadline.due_at), met=deadline.met)
        ),
        photos=tuple(finding_photo_out(photo) for photo in milestone.photos),
        process=process_summary_out(milestone.process),
        attachments=tuple(
            TimelineAttachmentOut(
                attachment_id=attachment.attachment_id,
                content_type=attachment.content_type,
                size_bytes=attachment.size_bytes,
            )
            for attachment in milestone.attachments
        ),
    )


def timeline_view(timeline: FindingTimeline) -> FindingTimelineOut:
    summary = timeline.summary
    return FindingTimelineOut(
        finding_state_id=timeline.finding_state_id,
        subject_kind=timeline.subject_kind,
        plant_id=timeline.plant_id,
        zone_id=timeline.zone_id,
        family=timeline.family,
        lifecycle_state=timeline.lifecycle_state,
        time_aggregated=timeline.time_aggregated,
        aggregation_window_minutes=timeline.aggregation_window_minutes,
        truncated=timeline.truncated,
        summary=TimelineSummaryOut(
            received_at=_optional(summary.received_at),
            closed_at=_optional(summary.closed_at),
            business_days_to_close=summary.business_days_to_close,
            classification_met=summary.classification_met,
            action_met=summary.action_met,
        ),
        milestones=tuple(_milestone(m) for m in timeline.milestones),
    )


def timeline_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        TIMELINE_PATH,
        dependencies=[
            requires(_READ, detail_codes=(_GATE_UNAVAILABLE.value,)),
            Depends(exact_query()),
        ],
        summary="Ficha de vida del hallazgo: su línea de tiempo en una lectura auditada",
    )
    async def get_timeline(
        finding_state_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> FindingTimelineOut:
        no_store(response)
        try:
            timeline = await installed(services.timeline).timeline(
                request_context(request), finding_state_id
            )
        except FindingGateUnavailable:
            # Fallo cerrado (BR-LAZ-12): solo el error, ningún campo del hallazgo.
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[_GATE_UNAVAILABLE]
            raise ApiError(code, detail_code=_GATE_UNAVAILABLE.value) from None
        return timeline_view(timeline)

    return router
