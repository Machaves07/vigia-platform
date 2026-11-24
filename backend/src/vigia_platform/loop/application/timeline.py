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

- **Fotos del hallazgo** (A-128, TASK-589): los hitos ``finding_received`` y
  ``late_segment_attached`` llevan las fotos de su tramo, con la forma y las marcas presentadas del
  detalle (``FindingDetailService.record_photos``: una consulta a ``fleet``, sin auditoría propia;
  la ficha sigue con su única entrada). El tramo tardío toma el ``record_id`` del nodo de la
  recepción que cita (``segment_record_id``) si está en la ficha; si no, va sin fotos.

``loop.finding_state.classified_within_deadline`` **no** se usa: ningún código la rellena (O-12,
verificado en TASK-585); la ficha aplica la regla de la métrica sobre el historial.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol

from vigia_platform.fleet.application.finding_photos import FindingPhoto, FindingPhotoKey
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import ResourceRef
from vigia_platform.ledger.application.reader import (
    LedgerRecordView,
    SubjectHistory,
    SubjectRef,
)
from vigia_platform.loop.application.classification import SUBJECT_RESOURCE_KIND
from vigia_platform.loop.application.detail import (
    FindingDetailService,
    FindingPhotoView,
    finding_photo_view,
    photo_key,
)
from vigia_platform.loop.calendar import is_business_day, local_date
from vigia_platform.loop.config.classification_deadline import (
    ClassificationDeadlineRule,
    classified_in_time,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, MilestoneKind, SubjectKind
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
    "photo_documents",
    "timeline_source",
]

MAX_TIMELINE_MILESTONES: Final = 500
"""Hitos por respuesta ``[objetivo propio]``; con más, ``truncated`` (la consola lo dice)."""

CONFIGURATION_RECORD_TYPE: Final = "loop_configuration_changed"
"""Los registros de planta que fijan el plazo de clasificación (BR-LAZ-112)."""


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

    outcome: str | None = None
    reason_category: str | None = None
    reason_text: str | None = None
    action_type: str | None = None
    description: str | None = None
    expected_outcome: str | None = None
    assignee_display_name: str | None = None
    """La instantánea del responsable al asignar (un usuario de la plataforma, BR-LAZ-38)."""
    due_at: datetime | None = None
    performed_at: datetime | None = None
    performed_by_role: str | None = None
    """El cargo de quien ejecutó, nunca su nombre (§6.3)."""
    verification_note: str | None = None
    days_overdue: int | None = None
    escalation_level: str | None = None
    professional_license: str | None = None
    root_cause_category: str | None = None
    root_cause_statement: str | None = None
    conclusion: str | None = None
    from_state: str | None = None
    compensatory_measure: str | None = None
    review_due_at: datetime | None = None
    review_note: str | None = None


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
    photos: tuple[FindingPhotoView, ...] = ()
    """Las fotos del tramo en ``finding_received`` y ``late_segment_attached`` (A-128)."""


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
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
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
    """Los ``loop_configuration_changed`` de la planta en orden de cadena (solo cuentan los
    anteriores a la recepción); ``None`` si no se pudieron leer completos (el plazo de
    clasificación no se sabe)."""
    timezone: str | None
    """La zona horaria de la planta; ``None`` si no se sabe (tampoco el plazo de clasificación)."""
    photos: Mapping[FindingPhotoKey, Sequence[FindingPhoto]] = field(default_factory=dict)
    """Las fotos del hallazgo por tramo (``detail.photo_key``), con las marcas exactas."""


_PHOTO_TYPES: Final = frozenset({"finding_received", "late_segment_attached"})
"""Los hitos que llevan las fotos de su tramo (TASK-589)."""


def _segment_document(
    item: TimelineSource, receptions: Mapping[str, Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """El documento de la recepción del tramo del hito: el propio en ``finding_received`` y el
    de la recepción citada (``segment_record_id``) en ``late_segment_attached``."""
    if item.record_type == "finding_received":
        return item.document
    if item.record_type == "late_segment_attached":
        try:
            cited = uuid.UUID(str(item.document.get("segment_record_id")))
        except ValueError:
            return None
        return receptions.get(str(cited))
    return None


def photo_documents(records: Sequence[TimelineSource]) -> tuple[Mapping[str, Any], ...]:
    """Los documentos de recepción cuyas fotos lleva la ficha."""
    receptions = _receptions(records)
    found = (_segment_document(r, receptions) for r in records if r.record_type in _PHOTO_TYPES)
    return tuple(document for document in found if document is not None)


def _receptions(records: Sequence[TimelineSource]) -> dict[str, Mapping[str, Any]]:
    return {str(r.record_id): r.document for r in records if r.record_type == "finding_received"}


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
        # La recepción, un tramo tardío y un adjunto no recuperable no llevan campos: su hito
        # es su tipo y su hora. Un retraso o una hora de detección exactos reconstruirían la hora
        # exacta en una zona unipersonal (BR-LAZ-06), y la consola no los pinta.
        case "classification":
            return MilestoneDetails(
                outcome=_text(doc, "outcome"),
                reason_category=_text(doc, "reason_category"),
                reason_text=_text(doc, "reason_text"),
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
        case "root_cause_analysis":
            return MilestoneDetails(
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
    superseded: bool = False
    """Otra asignación la sustituyó antes de cerrarse (BR-LAZ-51): su plazo ya no corre."""

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
            replaced = fates.get(str(item.document.get("supersedes_action_id")))
            if replaced is not None and replaced.closed_at is None:
                replaced.superseded = True
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
    """La regla vigente en la **recepción**: la de fábrica plegada con los cambios de la planta
    anteriores en la cadena a la primera recepción del sujeto (BR-LAZ-112, como la métrica)."""
    reception = next((r for r in inputs.records if r.record_type == "finding_received"), None)
    if inputs.configuration is None or reception is None:
        return _Rule(None, None, inputs.timezone)
    rule = ClassificationDeadlineRule.defaults()
    for change in inputs.configuration:
        if (
            change.record_type == CONFIGURATION_RECORD_TYPE
            and change.chain_sequence < reception.chain_sequence
        ):
            rule = rule.after_change(change.document["new_values"])
    return _Rule(rule, reception.received_at, inputs.timezone)


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
    photos: Mapping[FindingPhotoKey, Sequence[FindingPhoto]] = field(default_factory=dict)
    receptions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    milestones: list[Milestone] = field(default_factory=list)
    first_classification: Milestone | None = None

    def segment_photos(self, item: TimelineSource) -> tuple[FindingPhotoView, ...]:
        if item.record_type not in _PHOTO_TYPES:
            return ()
        key = photo_key(_segment_document(item, self.receptions))
        found = () if key is None else self.photos.get(key, ())
        return tuple(
            finding_photo_view(photo, self.shown)
            for photo in sorted(found, key=lambda p: (p.photo_index, str(p.photo_id)))
        )

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
            if fate is None or (fate.superseded and fate.met is None):
                # Sin plazo congelado, o sustituida sin cierre ni vencimiento: no hay plazo que
                # cumplir (ni «en curso» para siempre).
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
            photos=self.segment_photos(item),
        )
        if item.record_type == "classification" and self.first_classification is None:
            self.first_classification = milestone
        self.milestones.append(milestone)


def _summary(walk: _Walk, rule: _Rule, state: FindingLifecycleState) -> TimelineSummary:
    """El resumen: el último cierre si sigue cerrado, sus días hábiles con el calendario de la
    recepción, la primera clasificación y la acción vigente (la última asignada: la que un cierre
    cita, o la que sigue abierta)."""
    milestones = walk.milestones
    received = next((m.at for m in milestones if m.kind is MilestoneKind.FINDING_RECEIVED), None)
    closures = [m for m in milestones if m.kind is MilestoneKind.CLOSURE]
    closed = closures[-1] if closures and state is FindingLifecycleState.CLOSED else None
    days = None
    if closed is not None and received is not None and rule.rule is not None and rule.timezone:
        days = _business_days(
            local_date(received, rule.timezone), local_date(closed.at, rule.timezone), rule.rule
        )
    actions = [m for m in milestones if m.kind is MilestoneKind.ACTION_ASSIGNED]
    current = actions[-1].deadline if actions else None
    first = walk.first_classification
    return TimelineSummary(
        received_at=received,
        closed_at=None if closed is None else closed.at,
        business_days_to_close=days,
        classification_met=None if first is None or first.deadline is None else first.deadline.met,
        action_met=None if current is None else current.met,
    )


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
        photos=inputs.photos,
        receptions=_receptions(inputs.records),
    )
    for item in inputs.records:
        walk.add(item)
    state = FindingLifecycleState(subject["lifecycle_state"])
    return FindingTimeline(
        finding_state_id=uuid.UUID(str(subject["finding_state_id"])),
        subject_kind=str(subject["subject_kind"]),
        plant_id=uuid.UUID(str(subject["plant_id"])),
        zone_id=uuid.UUID(str(subject["zone_id"])),
        family=str(subject["family"]),
        lifecycle_state=state,
        time_aggregated=shown.single,
        aggregation_window_minutes=shown.window if shown.single else None,
        truncated=inputs.truncated,
        summary=_summary(walk, rule, state),
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
        records = tuple(timeline_source(r) for r in history.records)
        photos = await self._subjects.record_photos(authorized, subject, photo_documents(records))
        return finding_timeline(
            TimelineInputs(
                subject=subject,
                records=records,
                photos=photos,
                truncated=history.truncated,
                configuration=(
                    tuple(timeline_source(r) for r in history.preceding)
                    if history.preceding_complete
                    else None
                ),
                timezone=timezone,
            )
        )
