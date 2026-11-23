"""La ficha de vida de un hallazgo (TASK-585; O-11 y O-12 del recorrido del 22-11; BR-LAZ-01, 06,
09, 12, 39, 46 y 112): la línea de tiempo completa del sujeto en **una** lectura auditada.

``FindingTimelineService.timeline(context, finding_state_id)``, en este orden:

1. **Alcance y compuerta**, los mismos pasos que el detalle (``FindingDetailService
   .visible_subject``): ``findings.read`` sobre la zona del hallazgo y compuerta de uso aprobada en
   el instante del hecho. Inexistente, de otra organización, fuera de alcance o sin acuerdo en el
   instante: ``ResourceNotFound`` (``not_found``); sin compuerta legible, ``FindingGateUnavailable``
   (503, fallo cerrado), sin leer el expediente.
2. **La zona horaria de la planta** (sin auditoría: no es dato del expediente).
3. **Una sola lectura auditada** del expediente (BR-LAZ-09): ``LectorExpediente.subject_history``
   con la operación del detalle (``ledger_detail_read``), el hallazgo como recurso y un tope de
   ``MAX_TIMELINE_MILESTONES`` hitos (``truncated`` si hay más). Trae también los cambios de
   configuración de la planta anteriores a la recepción: son la regla del plazo de clasificación.

``finding_timeline`` es la composición **pura** de lo leído:

- **Un hito por registro del sujeto**, en orden de expediente, de la lista cerrada de
  ``MilestoneKind``; un tipo que no esté en ella sale como ``other`` con su tipo, nunca se oculta.
- **Una sola hora por hito**: la del expediente (``received_at``, que la cadena fija y nunca
  retrocede), **presentada**: agregada al inicio de su ventana si la zona es unipersonal
  (BR-LAZ-06). ``elapsed_ms_since_previous`` se calcula sobre esas horas presentadas. Ninguna hora
  de la respuesta (tampoco las de ``details`` ni la de un plazo) es exacta en zona unipersonal.
- **Quién**: el rol en uso y el nombre del usuario de la plataforma **solo** en los hitos cuyo
  registro los trae (quien clasificó, resolvió, asignó, añadió evidencia, firmó, reabrió o revisó).
  Nunca el identificador del usuario ni un dato de una persona observada (P3).
- **Detalles**: los campos elegidos de cada tipo, uno a uno (selección explícita, BR-APP-40).
- **Plazos**: la **primera** clasificación con la regla de BR-LAZ-112 (la misma función que la
  métrica, ``loop.config.classification_deadline``, con la configuración vigente en la recepción y
  el calendario de la planta); cada acción, cerrada antes de su ``due_at`` congelado (BR-LAZ-39),
  no cumplida si se declaró vencida antes del cierre (BR-LAZ-46) o se cerró tarde, y ``met =
  null`` mientras siga abierta sin vencimiento declarado. La ficha no lee ningún reloj.

``loop.finding_state.classified_within_deadline`` **no** se usa: ningún código la rellena (O-12,
verificado en TASK-585); la ficha aplica la regla de la métrica sobre el historial.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol

from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import ResourceRef
from vigia_platform.ledger.application.reader import (
    LedgerRecordView,
    SubjectHistory,
    SubjectRef,
)
from vigia_platform.loop.application.classification import SUBJECT_RESOURCE_KIND
from vigia_platform.loop.application.detail import FindingDetailService
from vigia_platform.loop.calendar import is_business_day, local_date
from vigia_platform.loop.config.classification_deadline import (
    ClassificationDeadlineRule,
    classified_in_time,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, SubjectKind
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.context import ScopeContext, repository

__all__ = [
    "CONFIGURATION_RECORD_TYPE",
    "MAX_TIMELINE_MILESTONES",
    "FindingTimeline",
    "FindingTimelineService",
    "Milestone",
    "MilestoneActor",
    "MilestoneDeadline",
    "MilestoneDetails",
    "MilestoneKind",
    "TimelineInputs",
    "TimelineSource",
    "TimelineSummary",
    "finding_timeline",
    "timeline_source",
]

MAX_TIMELINE_MILESTONES: Final = 500
"""Hitos por respuesta ``[objetivo propio]``; con más, ``truncated`` (la consola lo dice)."""

CONFIGURATION_RECORD_TYPE: Final = "loop_configuration_changed"
"""Los registros de planta que fijan el plazo de clasificación (BR-LAZ-112)."""


class MilestoneKind(enum.StrEnum):
    """Los hitos de la ficha: un tipo de registro del sujeto cada uno, y ``other`` para el resto."""

    FINDING_RECEIVED = "finding_received"
    LATE_SEGMENT_ATTACHED = "late_segment_attached"
    CLASSIFICATION = "classification"
    REVIEW_RESOLUTION = "review_resolution"
    ACTION_ASSIGNED = "action_assigned"
    ACTION_EVIDENCE_ADDED = "action_evidence_added"
    ACTION_OVERDUE = "action_overdue"
    CLOSURE = "closure"
    CLOSURE_ATTACHMENT_UNRECOVERABLE = "closure_attachment_unrecoverable"
    ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
    REOPENING = "reopening"
    UNCLOSABLE_STATE_DECLARED = "unclosable_state_declared"
    UNCLOSABLE_REVIEW = "unclosable_review"
    OTHER = "other"


_KINDS: Final[Mapping[str, MilestoneKind]] = {
    kind.value: kind for kind in MilestoneKind if kind is not MilestoneKind.OTHER
}

_AUTHOR: Final[Mapping[str, str]] = {
    "classification": "signer",
    "review_resolution": "resolved_by",
    "action_assigned": "assigned_by",
    "action_evidence_added": "added_by",
    "closure": "signer",
    "root_cause_analysis": "signer",
    "reopening": "reopened_by",
    "unclosable_state_declared": "signer",
    "unclosable_review": "reviewer",
}
"""La instantánea del usuario de la plataforma que decidió, por tipo (P8). Los demás tipos los
escribe el nodo o la plataforma: su hito no tiene ``actor``."""


# --- valores de la ficha -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class MilestoneActor:
    """Quién hizo el paso: un usuario de la plataforma, nunca una persona observada."""

    role_in_use: str | None
    display_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MilestoneDeadline:
    """El plazo que aplica al hito y si se cumplió (``None``: aún no se sabe)."""

    due_at: datetime
    met: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MilestoneDetails:
    """Los campos elegidos del registro (BR-APP-40); los que no son de su tipo van vacíos."""

    segment_index: int | None = None
    delay_ms: int | None = None
    outcome: str | None = None
    reason_category: str | None = None
    reason_text: str | None = None
    is_reclassification: bool | None = None
    action_type: str | None = None
    description: str | None = None
    expected_outcome: str | None = None
    assignee_display_name: str | None = None
    """La instantánea del responsable al asignar (un usuario de la plataforma, BR-LAZ-38)."""
    due_at: datetime | None = None
    deadline_days: int | None = None
    performed_at: datetime | None = None
    performed_by_role: str | None = None
    """El cargo de quien ejecutó, nunca su nombre (§6.3)."""
    verification_note: str | None = None
    days_overdue: int | None = None
    escalation_level: str | None = None
    professional_license: str | None = None
    analysis_state: str | None = None
    root_cause_category: str | None = None
    root_cause_statement: str | None = None
    conclusion: str | None = None
    from_state: str | None = None
    compensatory_measure: str | None = None
    review_due_at: datetime | None = None
    review_note: str | None = None
    detected_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Milestone:
    kind: MilestoneKind
    record_type: str
    record_id: uuid.UUID
    """El sello del registro en el expediente."""
    chain_sequence: int
    at: datetime
    """La hora del expediente, presentada (agregada en zona unipersonal)."""
    elapsed_ms_since_previous: int | None
    actor: MilestoneActor | None
    details: MilestoneDetails
    deadline: MilestoneDeadline | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineSummary:
    """Lo que la ficha resume arriba: «Recibido → cerrado en N días hábiles · …»."""

    received_at: datetime | None
    closed_at: datetime | None
    """La hora del último cierre si el hallazgo sigue cerrado; ``None`` si no."""
    business_days_to_close: int | None
    """Días hábiles de la planta entre la fecha local de la recepción y la del cierre."""
    classification_met: bool | None
    action_met: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FindingTimeline:
    finding_state_id: uuid.UUID
    subject_kind: str
    lifecycle_state: FindingLifecycleState
    time_aggregated: bool
    aggregation_window_minutes: int | None
    truncated: bool
    summary: TimelineSummary
    milestones: tuple[Milestone, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineSource:
    """Un registro del expediente tal como lo usa la ficha (``timeline_source``)."""

    record_id: uuid.UUID
    chain_sequence: int
    record_type: str
    received_at: datetime
    document: Mapping[str, Any]
    actor_kind: str | None = None
    actor_id: uuid.UUID | None = None
    actor_role_in_use: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineInputs:
    subject: Mapping[str, Any]
    """La fila de ``loop.finding_state``."""
    records: Sequence[TimelineSource]
    """Los registros del sujeto, en orden de cadena."""
    truncated: bool
    configuration: Sequence[TimelineSource] | None
    """Los ``loop_configuration_changed`` de la planta anteriores a la recepción, en orden de
    cadena; ``None`` si no se pudieron leer completos (los plazos de clasificación no se saben)."""
    timezone: str | None
    """La zona horaria de la planta; ``None`` si no se sabe (tampoco el plazo de clasificación)."""


def timeline_source(record: LedgerRecordView) -> TimelineSource:
    document = record.document()
    return TimelineSource(
        record_id=record.record_id,
        chain_sequence=record.chain_sequence,
        record_type=record.record_type,
        received_at=record.received_at,
        document=document if isinstance(document, Mapping) else {},
        actor_kind=record.actor.kind,
        actor_id=record.actor.id,
        actor_role_in_use=record.actor.role_in_use,
    )


# --- composición pura --------------------------------------------------------------------------


def _instant(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria")
    return moment


def _text(document: Mapping[str, Any], key: str) -> str | None:
    value = document.get(key)
    return None if value is None else str(value)


def _integer(value: Any) -> int | None:
    return None if value is None or isinstance(value, bool) else int(value)


class _Shown:
    """La hora que se devuelve: agregada en zona unipersonal, exacta si no (BR-LAZ-06)."""

    def __init__(self, subject: Mapping[str, Any]) -> None:
        self.single = bool(subject["single_occupancy_zone"])
        self.window = int(subject["aggregation_window_minutes"])

    def __call__(self, moment: Any) -> datetime:
        return presented(_instant(moment), single_occupancy=self.single, window_minutes=self.window)

    def optional(self, moment: Any) -> datetime | None:
        return None if moment is None else self(moment)


def _actor(item: TimelineSource) -> MilestoneActor | None:
    """La instantánea del autor del paso, solo si el registro la trae (``_AUTHOR``)."""
    key = _AUTHOR.get(item.record_type)
    snapshot = None if key is None else item.document.get(key)
    if not isinstance(snapshot, Mapping) or snapshot.get("display_name") is None:
        return None
    role = snapshot.get("role_in_use")
    if (
        role is None
        and item.actor_kind == "user"
        and str(item.actor_id) == str(snapshot.get("user_id"))
    ):
        # Un firmante con licencia no lleva rol: el sobre del registro dice con cuál firmó.
        role = item.actor_role_in_use
    return MilestoneActor(
        role_in_use=None if role is None else str(role),
        display_name=str(snapshot["display_name"]),
    )


def _details(item: TimelineSource, shown: _Shown) -> MilestoneDetails:
    """Los campos elegidos del registro, tipo por tipo (BR-APP-40)."""
    doc = item.document
    match item.record_type:
        case "finding_received":
            episode = doc.get("episode")
            index = episode.get("segment_index") if isinstance(episode, Mapping) else None
            return MilestoneDetails(segment_index=_integer(index))
        case "late_segment_attached":
            return MilestoneDetails(
                segment_index=_integer(doc.get("segment_index")),
                delay_ms=_integer(doc.get("delay_ms")),
            )
        case "classification":
            return MilestoneDetails(
                outcome=_text(doc, "outcome"),
                reason_category=_text(doc, "reason_category"),
                reason_text=_text(doc, "reason_text"),
                is_reclassification=bool(doc.get("is_reclassification", False)),
            )
        case "review_resolution":
            return MilestoneDetails(
                outcome=_text(doc, "outcome"),
                reason_category=_text(doc, "reason_category"),
                reason_text=_text(doc, "reason_text"),
            )
        case "action_assigned":
            return MilestoneDetails(
                action_type=_text(doc, "action_type"),
                description=_text(doc, "action_description"),
                expected_outcome=_text(doc, "expected_outcome"),
                assignee_display_name=_text(doc, "assignee_display_name"),
                due_at=shown.optional(doc.get("due_at")),
                deadline_days=_integer(doc.get("deadline_days")),
            )
        case "action_evidence_added":
            return MilestoneDetails(
                description=_text(doc, "description"),
                performed_at=shown.optional(doc.get("performed_at")),
                performed_by_role=_text(doc, "performed_by_role"),
                verification_note=_text(doc, "verification_note"),
            )
        case "action_overdue":
            return MilestoneDetails(
                due_at=shown.optional(doc.get("due_at")),
                days_overdue=_integer(doc.get("days_overdue")),
                escalation_level=_text(doc, "escalation_level"),
            )
        case "closure":
            signer = doc.get("signer")
            return MilestoneDetails(
                action_type=_text(doc, "action_type"),
                description=_text(doc, "closure_description"),
                verification_note=_text(doc, "verification_note"),
                professional_license=(
                    _text(signer, "professional_license") if isinstance(signer, Mapping) else None
                ),
            )
        case "closure_attachment_unrecoverable":
            return MilestoneDetails(detected_at=shown.optional(doc.get("detected_at")))
        case "root_cause_analysis":
            return MilestoneDetails(
                analysis_state=_text(doc, "state"),
                root_cause_category=_text(doc, "root_cause_category"),
                root_cause_statement=_text(doc, "root_cause_statement"),
                conclusion=_text(doc, "conclusion"),
            )
        case "reopening":
            return MilestoneDetails(
                from_state=_text(doc, "from_state"), reason_text=_text(doc, "reason_text")
            )
        case "unclosable_state_declared":
            return MilestoneDetails(
                compensatory_measure=_text(doc, "compensatory_measure"),
                review_due_at=shown.optional(doc.get("review_due_at")),
            )
        case "unclosable_review":
            return MilestoneDetails(
                outcome=_text(doc, "outcome"),
                review_note=_text(doc, "review_note"),
                review_due_at=shown.optional(doc.get("new_review_due_at")),
            )
    return MilestoneDetails()


@dataclass(slots=True)
class _ActionFate:
    """Lo que el historial dice de una acción: su plazo congelado, si venció y si se cerró."""

    due_at: datetime
    overdue: bool = False
    closed_at: datetime | None = None

    @property
    def met(self) -> bool | None:
        if self.overdue:
            return False  # BR-LAZ-46: el vencimiento declarado antes del cierre
        if self.closed_at is None:
            return None  # abierta y sin vencimiento declarado: aún no se sabe
        return self.closed_at < self.due_at  # BR-LAZ-39: el plazo congelado


def _action_fates(records: Sequence[TimelineSource]) -> dict[str, _ActionFate]:
    fates: dict[str, _ActionFate] = {}
    for item in records:
        action = _text(item.document, "action_id")
        if action is None:
            continue
        if item.record_type == "action_assigned" and item.document.get("due_at") is not None:
            fates.setdefault(action, _ActionFate(_instant(item.document["due_at"])))
        fate = fates.get(action)
        if fate is None or fate.closed_at is not None:
            continue
        if item.record_type == "action_overdue":
            fate.overdue = True
        elif item.record_type == "closure" and item.document.get("signed_at") is not None:
            fate.closed_at = _instant(item.document["signed_at"])
    return fates


@dataclass(frozen=True, slots=True)
class _Rule:
    """La regla del plazo vigente en la recepción y la recepción misma; ``None`` si no se sabe."""

    rule: ClassificationDeadlineRule | None
    received_at: datetime | None
    timezone: str | None

    def deadline(self) -> datetime | None:
        if self.rule is None or self.received_at is None or self.timezone is None:
            return None
        return self.rule.deadline(self.received_at, self.timezone)


def _rule(inputs: TimelineInputs) -> _Rule:
    received = next(
        (r.received_at for r in inputs.records if r.record_type == "finding_received"), None
    )
    if inputs.configuration is None:
        return _Rule(None, received, inputs.timezone)
    rule = ClassificationDeadlineRule.defaults()
    for change in inputs.configuration:
        if change.record_type == CONFIGURATION_RECORD_TYPE:
            rule = rule.after_change(change.document["new_values"])
    return _Rule(rule, received, inputs.timezone)


def _business_days(start: date, end: date, rule: ClassificationDeadlineRule) -> int:
    """Días hábiles en ``(start, end]`` con el calendario de la regla."""
    days = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        days += is_business_day(day, rule.calendar)
    return days


@dataclass(slots=True)
class _Walk:
    shown: _Shown
    fates: Mapping[str, _ActionFate]
    classification_deadline: datetime | None
    milestones: list[Milestone] = field(default_factory=list)
    first_classification: Milestone | None = None

    def deadline(self, item: TimelineSource) -> MilestoneDeadline | None:
        doc = item.document
        if item.record_type == "classification" and self.first_classification is None:
            due = self.classification_deadline
            if due is None:
                return None
            met = classified_in_time(_instant(doc["classified_at"]), due)
            return MilestoneDeadline(due_at=self.shown(due), met=met)
        if item.record_type == "action_assigned":
            fate = self.fates.get(str(doc.get("action_id")))
            if fate is None:
                return None
            return MilestoneDeadline(due_at=self.shown(fate.due_at), met=fate.met)
        return None

    def add(self, item: TimelineSource) -> None:
        at = self.shown(item.received_at)
        previous = self.milestones[-1].at if self.milestones else None
        milestone = Milestone(
            kind=_KINDS.get(item.record_type, MilestoneKind.OTHER),
            record_type=item.record_type,
            record_id=item.record_id,
            chain_sequence=item.chain_sequence,
            at=at,
            elapsed_ms_since_previous=(
                None if previous is None else (at - previous) // timedelta(milliseconds=1)
            ),
            actor=_actor(item),
            details=_details(item, self.shown),
            deadline=self.deadline(item),
        )
        if item.record_type == "classification" and self.first_classification is None:
            self.first_classification = milestone
        self.milestones.append(milestone)


def _summary(
    inputs: TimelineInputs, walk: _Walk, rule: _Rule, state: FindingLifecycleState
) -> TimelineSummary:
    milestones = walk.milestones
    received = next((m.at for m in milestones if m.kind is MilestoneKind.FINDING_RECEIVED), None)
    closures = [m for m in milestones if m.kind is MilestoneKind.CLOSURE]
    closed = closures[-1] if closures and state is FindingLifecycleState.CLOSED else None
    days = None
    if closed is not None and received is not None and rule.rule is not None and rule.timezone:
        days = _business_days(
            local_date(received, rule.timezone), local_date(closed.at, rule.timezone), rule.rule
        )
    actions = {
        str(item.document.get("action_id")): m
        for item, m in zip(inputs.records, milestones, strict=True)
        if m.kind is MilestoneKind.ACTION_ASSIGNED
    }
    cited = None
    if closed is not None:
        cited = actions.get(str(_closure_action(inputs.records, closed.record_id)))
    elif actions:
        cited = list(actions.values())[-1]
    first = walk.first_classification
    return TimelineSummary(
        received_at=received,
        closed_at=None if closed is None else closed.at,
        business_days_to_close=days,
        classification_met=None if first is None or first.deadline is None else first.deadline.met,
        action_met=None if cited is None or cited.deadline is None else cited.deadline.met,
    )


def _closure_action(records: Sequence[TimelineSource], record_id: uuid.UUID) -> str | None:
    for item in records:
        if item.record_id == record_id:
            return _text(item.document, "action_id")
    return None


def finding_timeline(inputs: TimelineInputs) -> FindingTimeline:
    """La ficha de vida con todas sus horas presentadas; lo leído no cambia."""
    subject = inputs.subject
    shown = _Shown(subject)
    rule = _rule(inputs)
    finding = str(subject["subject_kind"]) == SubjectKind.FINDING.value
    walk = _Walk(
        shown=shown,
        fates=_action_fates(inputs.records),
        classification_deadline=rule.deadline() if finding else None,
    )
    for item in inputs.records:
        walk.add(item)
    state = FindingLifecycleState(subject["lifecycle_state"])
    return FindingTimeline(
        finding_state_id=uuid.UUID(str(subject["finding_state_id"])),
        subject_kind=str(subject["subject_kind"]),
        lifecycle_state=state,
        time_aggregated=shown.single,
        aggregation_window_minutes=shown.window if shown.single else None,
        truncated=inputs.truncated,
        summary=_summary(inputs, walk, rule, state),
        milestones=tuple(walk.milestones),
    )


# --- el servicio -------------------------------------------------------------------------------


class SubjectHistoryReader(Protocol):
    """Lo que la ficha usa de ``LectorExpediente``: ``subject_history``, auditada."""

    async def subject_history(
        self,
        context: ScopeContext,
        subject: SubjectRef,
        *,
        limit: int,
        preceding_types: tuple[str, ...] = (),
    ) -> SubjectHistory | None: ...


class PlantTimezones(Protocol):
    async def plant_timezone(self, context: ScopeContext, plant_id: uuid.UUID) -> str | None: ...


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


@repository
class FindingTimelineService:
    """``GET /loop/findings/{finding_state_id}/timeline`` (TASK-585)."""

    def __init__(
        self,
        *,
        subjects: FindingDetailService,
        reader: SubjectHistoryReader,
        timezones: PlantTimezones,
    ) -> None:
        self._subjects = subjects
        self._reader = reader
        self._timezones = timezones

    def __repr__(self) -> str:
        return "FindingTimelineService()"

    async def timeline(self, context: ScopeContext, finding_state_id: uuid.UUID) -> FindingTimeline:
        """La ficha de vida; ``ResourceNotFound`` o ``FindingGateUnavailable`` como el detalle."""
        authorized, subject = await self._subjects.visible_subject(context, finding_state_id)
        plant_id = _uuid(subject["plant_id"])
        timezone = await self._timezones.plant_timezone(authorized, plant_id)
        history = await self._reader.subject_history(
            authorized,
            SubjectRef(
                plant_id=plant_id,
                zone_id=_uuid(subject["zone_id"]),
                subject_id=_uuid(subject["subject_id"]),
                anchor_record_id=_uuid(subject["anchor_record_id"]),
                resource=ResourceRef(SUBJECT_RESOURCE_KIND, _uuid(subject["finding_state_id"])),
            ),
            limit=MAX_TIMELINE_MILESTONES,
            preceding_types=(CONFIGURATION_RECORD_TYPE,),
        )
        if history is None:
            # El expediente manda: un anclaje que el alcance no ve no se inventa.
            raise ResourceNotFound()
        return finding_timeline(
            TimelineInputs(
                subject=subject,
                records=tuple(timeline_source(r) for r in history.records),
                truncated=history.truncated,
                configuration=(
                    tuple(timeline_source(r) for r in history.preceding)
                    if history.preceding_complete
                    else None
                ),
                timezone=timezone,
            )
        )
