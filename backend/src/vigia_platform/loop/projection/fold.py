"""Pliegue puro de la proyección del lazo (LC-LAZ-01; domain-entities.md de U-04, §2.1, §2.2,
§2.14 y §9; business-logic-model.md §7.3; BR-LAZ-13 a 17, 146, 147, 151 y 155).

Módulo **puro** (PAT-LAZ-SEG-09): sin FastAPI, sin SQLAlchemy, sin reloj y sin puertos. Deriva
``Episode`` y ``FindingState`` de los registros del expediente de una planta aplicando
``lifecycle.transition``. Lo comparten las dos vías de actualización:

- ``step(episode, finding, item)``: el **núcleo**, que aplica un registro a las filas de **su**
  sujeto. ``store.apply_in_transaction`` lo llama con las filas leídas de la base, en la misma
  transacción que la escritura por ``EscritorExpediente`` (BR-LAZ-146);
- ``fold(items)``: la reconstrucción (``rebuild_loop_projection``, TASK-306): agrupa por
  partición ``(organization_id, plant_id)`` y aplica ``step`` en orden de ``chain_sequence``
  dentro de cada una (el orden de llegada no cuenta: PR-LAZ-43).

**Idempotencia** (BR-LAZ-151, PR-LAZ-43): un registro ya aplicado no cambia nada. Se reconoce por
``last_record_id`` del sujeto (la reaplicación del último registro, que es la que produce un
reintento o dos aplicaciones concurrentes), por ``last_event_id`` (el evento repetido de U-03) y,
para un tramo, por su presencia en ``Episode.segment_record_ids``. Una escritura de U-04 que el
escritor resuelve como ``accepted_duplicate`` no se vuelve a aplicar (``store``).

**Lo que el pliegue no sabe, y deja con el valor del esquema**: los campos de enriquecimiento
(TASK-341) toman el valor conservador hasta que el consumidor los rellena:
``observability_state_at_event`` es el que declaró el nodo en el tramo;
``zone_gate_status_at_event`` es ``approved`` (la ingesta no persiste un hallazgo sin compuerta
aprobada, P9);
``single_occupancy_zone`` es **verdadero** (sin el puerto se agrega, §2.2) con la ventana de 60
minutos; ``evidence_flagged``, ``assignee_inactive``, ``zone_agreement_revoked`` y
``walk_test_regression_pending`` son falsos. ``classified_within_deadline`` (plazo en días
hábiles de la planta) y ``age_days`` (depende de «hoy») no se derivan aquí: nulo y 0.

**Tramo tardío** (BR-LAZ-15 y 155, T21): un tramo de un episodio ya visible que llega cuando el
hallazgo ya no está ``received``, cuando el episodio ya se había cerrado o que rellena un hueco se
adjunta al episodio (duración, clips, tier máximo, tramos faltantes) **sin crear elemento ni
reabrir**; ``Step.late_segment`` es el contenido del registro ``late_segment_attached`` que la
vía de eventos debe escribir (TASK-341). Aplicar luego ese registro es T21: no cambia el estado.

Un registro de U-04 que la máquina rechaza, o cuyo sujeto no existe, es una **incoherencia**
entre el expediente y la proyección (``ProjectionInconsistent``): el pliegue no inventa un
estado (BR-LAZ-156).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from vigia_platform.loop.domain.enums import (
    ActionType,
    ClassificationOutcome,
    FindingLifecycleState,
    ReviewResolutionOutcome,
    SubjectKind,
    UnclosableReviewOutcome,
)
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    AssignAction,
    AttachLateSegment,
    Classify,
    Close,
    Command,
    DeclareOverdue,
    DeclareUnclosable,
    LifecycleSnapshot,
    PolicyStatus,
    ReceiveDetection,
    ReceiveFinding,
    Reopen,
    ResolveReview,
    ReviewUnclosable,
    transition,
)
from vigia_platform.loop.projection.digest import (
    EMPTY_PARTITION_DIGEST,
    EPISODE_DIGEST_FIELDS,
    FINDING_STATE_DIGEST_FIELDS,
    partition_term,
    replace_term,
    row_digest,
)

__all__ = [
    "DEFAULT_AGGREGATION_WINDOW_MINUTES",
    "PROJECTED_RECORD_TYPES",
    "U03_RECORD_TYPES",
    "U04_RECORD_TYPES",
    "EpisodeRow",
    "FindingStateRow",
    "LateSegmentNotice",
    "PlantProjection",
    "ProjectionInconsistent",
    "ProjectionInput",
    "Step",
    "aggregation_bucket",
    "episode_term",
    "finding_term",
    "fold",
    "step",
    "subject_id_of",
]

S = FindingLifecycleState

DEFAULT_AGGREGATION_WINDOW_MINUTES: Final = 60
"""Ventana de agregación por defecto, fijada por U-03 (§2.2, pregunta 3 = A)."""

U03_RECORD_TYPES: Final = frozenset({"finding_received", "detection_for_review_received"})
"""Los tipos de U-03 que el pliegue lee (§9)."""

U04_RECORD_TYPES: Final = frozenset(
    {
        "classification",
        "review_resolution",
        "action_assigned",
        "action_evidence_added",
        "closure",
        "unclosable_state_declared",
        "root_cause_analysis",
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "vocabulary_list_changed",
        "action_overdue",
        "unclosable_review",
        "reopening",
        "commitments_recorded",
        "loop_configuration_changed",
        "late_segment_attached",
    }
)
"""Los diecisiete tipos de U-04 (§5.1). Los que no cambian ``Episode`` ni ``FindingState``
(acreditación, vocabulario, configuración y entradas de evidencia) solo avanzan el control."""

PROJECTED_RECORD_TYPES: Final = U03_RECORD_TYPES | U04_RECORD_TYPES

_NO_SUBJECT_EFFECT: Final = frozenset(
    {
        "action_evidence_added",
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "vocabulary_list_changed",
        "commitments_recorded",
        "loop_configuration_changed",
    }
)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class ProjectionInconsistent(ValueError):
    """El registro no es aplicable a la proyección: el expediente y la proyección divergen."""

    def __init__(self, record_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"registro {record_id}: {reason}")
        self.record_id = record_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProjectionInput:
    """Un registro del expediente tal como lo lee el pliegue (o el evento que lo anuncia).

    ``document`` es el contenido JSON del registro; ``event_id`` solo llega por la vía de
    eventos (BR-NUC-76)."""

    record_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    chain_sequence: int
    record_type: str
    received_at: datetime
    document: Mapping[str, Any]
    event_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    """Una fila de ``loop.episode`` (§2.1); los nombres son los de las columnas."""

    episode_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    standard_id: uuid.UUID
    standard_version: int
    segment_record_ids: tuple[uuid.UUID, ...]
    """En orden de ``segment_index``."""
    segment_count: int
    expected_segment_count: int
    missing_segment_indexes: tuple[int, ...]
    started_at: datetime
    ended_at: datetime
    total_episode_duration_ms: int
    total_condition_duration_ms: int
    max_confidence: float
    clip_references: tuple[Mapping[str, Any], ...]
    """Todos los clips de todos los tramos, agrupados por cámara."""
    observability_state_at_start: str
    continues_open: bool
    last_segment_received_at: datetime
    last_event_id: uuid.UUID
    projection_updated_at: datetime

    @property
    def present_indexes(self) -> tuple[int, ...]:
        """Los ``segment_index`` presentes, en orden (los que no faltan hasta el máximo)."""
        missing = set(self.missing_segment_indexes)
        return tuple(i for i in range(self.expected_segment_count) if i not in missing)

    def digest(self) -> str:
        return row_digest(dataclasses.asdict(self), EPISODE_DIGEST_FIELDS)


@dataclass(frozen=True, slots=True)
class FindingStateRow:
    """Una fila de ``loop.finding_state`` (§2.2); los nombres son los de las columnas."""

    finding_state_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    anchor_record_id: uuid.UUID
    family: str
    tier: str
    standard_id: uuid.UUID
    standard_version: int
    lifecycle_state: str
    previous_lifecycle_state: str | None
    current_classification_outcome: str | None
    current_reason_category: str | None
    classification_count: int
    first_classified_at: datetime | None
    last_classified_at: datetime | None
    classified_within_deadline: bool | None
    current_action_id: uuid.UUID | None
    current_action_type: str | None
    assignee_user_id: uuid.UUID | None
    assignee_inactive: bool
    due_at: datetime | None
    overdue_since: datetime | None
    overdue_days: int
    closed_at: datetime | None
    closure_record_id: uuid.UUID | None
    unclosable_since: datetime | None
    unclosable_review_due_at: datetime | None
    unclosable_pending_review: bool
    closed_with_later_different_classification: bool
    reopening_count: int
    has_signed_root_cause: bool
    observability_state_at_event: str
    zone_gate_status_at_event: str
    zone_agreement_revoked: bool
    zone_agreement_revoked_at: datetime | None
    single_occupancy_zone: bool
    aggregation_window_minutes: int
    walk_test_regression_pending: bool
    walk_test_regression_cause: str | None
    occurred_at_exact: datetime
    occurred_at_bucket: datetime
    age_days: int
    evidence_flagged: bool
    missing_segments_declared: bool
    last_event_id: uuid.UUID
    last_record_id: uuid.UUID
    projection_updated_at: datetime
    projection_digest: str

    @property
    def state(self) -> FindingLifecycleState:
        return FindingLifecycleState(self.lifecycle_state)

    def computed_digest(self) -> str:
        return row_digest(dataclasses.asdict(self), FINDING_STATE_DIGEST_FIELDS)


@dataclass(frozen=True, slots=True)
class LateSegmentNotice:
    """Contenido del registro ``late_segment_attached`` que un tramo tardío exige (§5.2)."""

    segment_record_id: uuid.UUID
    episode_id: uuid.UUID
    segment_index: int
    previous_segment_record_id: uuid.UUID | None
    received_at: datetime
    episode_state_at_arrival: FindingLifecycleState
    delay_ms: int


@dataclass(frozen=True, slots=True)
class Step:
    """El resultado de aplicar un registro a las filas de su sujeto."""

    episode: EpisodeRow | None
    finding: FindingStateRow | None
    applied: bool
    """Falso si el registro ya estaba aplicado o no afecta a ningún sujeto."""
    transition_id: str | None = None
    late_segment: LateSegmentNotice | None = None


# --- lectura del contenido ------------------------------------------------------------------


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _instant(value: Any) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria")
    return moment.astimezone(UTC)


def _optional_instant(value: Any) -> datetime | None:
    return None if value is None else _instant(value)


def subject_id_of(item: ProjectionInput) -> uuid.UUID | None:
    """El sujeto del lazo al que el registro afecta: el episodio, la detección o ninguno."""
    document = item.document
    match item.record_type:
        case "finding_received":
            return _uuid(document["episode"]["episode_id"])
        case "detection_for_review_received":
            return _uuid(document["detection_id"])
        case "review_resolution":
            return _uuid(document["detection_id"])
        case "late_segment_attached":
            return _uuid(document["episode_id"])
        case record_type if record_type in _NO_SUBJECT_EFFECT:
            return None
        case record_type if record_type in U04_RECORD_TYPES:
            return _uuid(document["subject_id"])
    return None


def aggregation_bucket(moment: datetime, minutes: int) -> datetime:
    """``moment`` truncado al inicio de su ventana de ``minutes`` (``occurred_at_bucket``, §2.2)."""
    window = timedelta(minutes=minutes)
    return _EPOCH + ((moment - _EPOCH) // window) * window


def _max_tier(left: str, right: str) -> str:
    return "tier_1" if "tier_1" in (left, right) else "tier_2"


def _clips(document: Mapping[str, Any], segment_index: int) -> list[dict[str, Any]]:
    return [
        {
            "camera_id": str(camera["camera_id"]),
            "clip_id": str(clip["clip_id"]),
            "segment": str(clip["segment"]),
            "segment_index": segment_index,
        }
        for camera in document["cameras"]
        for clip in camera["clips"]
    ]


def _clip_order(clip: Mapping[str, Any]) -> tuple[str, int, str]:
    return (str(clip["camera_id"]), int(clip["segment_index"]), str(clip["clip_id"]))


def _finalize_finding(row: FindingStateRow, item: ProjectionInput) -> FindingStateRow:
    """Contabilidad de la proyección y resumen de los campos de estado."""
    stamped = dataclasses.replace(
        row,
        last_event_id=item.event_id or item.record_id,
        last_record_id=item.record_id,
        projection_updated_at=item.received_at,
    )
    return dataclasses.replace(stamped, projection_digest=stamped.computed_digest())


def _new_finding(
    item: ProjectionInput,
    *,
    subject_kind: SubjectKind,
    subject_id: uuid.UUID,
    state: FindingLifecycleState,
    tier: str,
    missing_segments: bool,
) -> FindingStateRow:
    document = item.document
    occurred = _instant(document["node_time"]["started_at"])
    window = DEFAULT_AGGREGATION_WINDOW_MINUTES
    row = FindingStateRow(
        finding_state_id=item.record_id,
        organization_id=item.organization_id,
        plant_id=_uuid(document["plant_id"]),
        zone_id=_uuid(document["zone_id"]),
        subject_kind=subject_kind.value,
        subject_id=subject_id,
        anchor_record_id=item.record_id,
        family=str(document["family"]),
        tier=tier,
        standard_id=_uuid(document["standard"]["standard_id"]),
        standard_version=int(document["standard"]["version"]),
        lifecycle_state=state.value,
        previous_lifecycle_state=None,
        current_classification_outcome=None,
        current_reason_category=None,
        classification_count=0,
        first_classified_at=None,
        last_classified_at=None,
        classified_within_deadline=None,
        current_action_id=None,
        current_action_type=None,
        assignee_user_id=None,
        assignee_inactive=False,
        due_at=None,
        overdue_since=None,
        overdue_days=0,
        closed_at=None,
        closure_record_id=None,
        unclosable_since=None,
        unclosable_review_due_at=None,
        unclosable_pending_review=False,
        closed_with_later_different_classification=False,
        reopening_count=0,
        has_signed_root_cause=False,
        observability_state_at_event=str(document["observability_state"]),
        zone_gate_status_at_event="approved",
        zone_agreement_revoked=False,
        zone_agreement_revoked_at=None,
        single_occupancy_zone=True,
        aggregation_window_minutes=window,
        walk_test_regression_pending=False,
        walk_test_regression_cause=None,
        occurred_at_exact=occurred,
        occurred_at_bucket=aggregation_bucket(occurred, window),
        age_days=0,
        evidence_flagged=False,
        missing_segments_declared=missing_segments,
        last_event_id=item.record_id,
        last_record_id=item.record_id,
        projection_updated_at=item.received_at,
        projection_digest=EMPTY_PARTITION_DIGEST,
    )
    return _finalize_finding(row, item)


# --- tramos ---------------------------------------------------------------------------------


def _new_episode(item: ProjectionInput) -> EpisodeRow:
    document = item.document
    segment = document["episode"]
    index = int(segment["segment_index"])
    return EpisodeRow(
        episode_id=_uuid(segment["episode_id"]),
        organization_id=item.organization_id,
        plant_id=_uuid(document["plant_id"]),
        zone_id=_uuid(document["zone_id"]),
        family=str(document["family"]),
        tier=str(document["tier"]),
        standard_id=_uuid(document["standard"]["standard_id"]),
        standard_version=int(document["standard"]["version"]),
        segment_record_ids=(item.record_id,),
        segment_count=1,
        expected_segment_count=index + 1,
        missing_segment_indexes=tuple(range(index)),
        started_at=_instant(document["node_time"]["started_at"]),
        ended_at=_instant(document["node_time"]["ended_at"]),
        total_episode_duration_ms=int(document["episode_duration_ms"]),
        total_condition_duration_ms=int(document["condition_duration_ms"]),
        max_confidence=float(document["max_confidence"]),
        clip_references=tuple(sorted(_clips(document, index), key=_clip_order)),
        observability_state_at_start=str(document["observability_state"]),
        continues_open=bool(segment["continues"]),
        last_segment_received_at=item.received_at,
        last_event_id=item.event_id or item.record_id,
        projection_updated_at=item.received_at,
    )


def _extend_episode(episode: EpisodeRow, item: ProjectionInput) -> EpisodeRow:
    """Añade un tramo: duración = suma de los presentes, tier máximo, estándar del primero."""
    document = item.document
    segment = document["episode"]
    index = int(segment["segment_index"])
    by_index = dict(zip(episode.present_indexes, episode.segment_record_ids, strict=True))
    by_index[index] = item.record_id
    expected = max(episode.expected_segment_count, index + 1)
    present = sorted(by_index)
    continues_open = bool(segment["continues"]) if index >= max(present) else episode.continues_open
    clips = [*episode.clip_references, *_clips(document, index)]
    return dataclasses.replace(
        episode,
        tier=_max_tier(episode.tier, str(document["tier"])),
        segment_record_ids=tuple(by_index[i] for i in present),
        segment_count=len(present),
        expected_segment_count=expected,
        missing_segment_indexes=tuple(i for i in range(expected) if i not in by_index),
        started_at=min(episode.started_at, _instant(document["node_time"]["started_at"])),
        ended_at=max(episode.ended_at, _instant(document["node_time"]["ended_at"])),
        total_episode_duration_ms=episode.total_episode_duration_ms
        + int(document["episode_duration_ms"]),
        total_condition_duration_ms=episode.total_condition_duration_ms
        + int(document["condition_duration_ms"]),
        max_confidence=max(episode.max_confidence, float(document["max_confidence"])),
        clip_references=tuple(sorted(clips, key=_clip_order)),
        continues_open=continues_open,
        last_segment_received_at=max(episode.last_segment_received_at, item.received_at),
        last_event_id=item.event_id or item.record_id,
        projection_updated_at=item.received_at,
    )


def _is_late(episode: EpisodeRow, finding: FindingStateRow, index: int) -> bool:
    """Tardío: el hallazgo ya no está ``received``, el episodio ya se cerró o rellena un hueco."""
    return (
        finding.state is not S.RECEIVED
        or not episode.continues_open
        or index < max(episode.present_indexes)
    )


def _finding_received(
    episode: EpisodeRow | None, finding: FindingStateRow | None, item: ProjectionInput
) -> Step:
    document = item.document
    subject_id = _uuid(document["episode"]["episode_id"])
    index = int(document["episode"]["segment_index"])
    if (episode is None) != (finding is None):
        # El estado y el episodio nacen juntos; uno sin el otro es un tramo de otra planta (el
        # estado se busca por planta) o una proyección rota: no se inventa ninguno de los dos.
        raise ProjectionInconsistent(item.record_id, "episodio y estado del hallazgo no concuerdan")
    if episode is None or finding is None:
        accepted = _expect(item, LifecycleSnapshot(None), ReceiveFinding())
        new_episode = _new_episode(item)
        new_finding = _new_finding(
            item,
            subject_kind=SubjectKind.FINDING,
            subject_id=subject_id,
            state=accepted.state,
            tier=new_episode.tier,
            missing_segments=bool(new_episode.missing_segment_indexes),
        )
        return Step(new_episode, new_finding, True, accepted.transition.transition_id)
    if index in episode.present_indexes:
        # Otro registro para un tramo ya presente: el contrato no lo admite (BR-CTR-26).
        raise ProjectionInconsistent(item.record_id, f"tramo {index} repetido en el episodio")
    late = _is_late(episode, finding, index)
    extended = _extend_episode(episode, item)
    updated = _finalize_finding(
        dataclasses.replace(
            finding,
            tier=_max_tier(finding.tier, extended.tier),
            missing_segments_declared=bool(extended.missing_segment_indexes),
        ),
        item,
    )
    notice = None
    if late:
        previous = document["episode"].get("previous_segment_record_id")
        ended = _instant(document["node_time"]["ended_at"])
        delay = max(0, (item.received_at - ended) // timedelta(milliseconds=1))
        notice = LateSegmentNotice(
            segment_record_id=item.record_id,
            episode_id=subject_id,
            segment_index=index,
            previous_segment_record_id=_optional_uuid(previous),
            received_at=item.received_at,
            episode_state_at_arrival=finding.state,
            delay_ms=delay,
        )
    return Step(extended, updated, True, None, notice)


# --- registros de U-04 ----------------------------------------------------------------------


def _expect(item: ProjectionInput, snapshot: LifecycleSnapshot, command: Command) -> Accepted:
    result = transition(snapshot, command)
    if not isinstance(result, Accepted):
        state = "—" if snapshot.state is None else snapshot.state.value
        raise ProjectionInconsistent(
            item.record_id, f"{item.record_type} no aplicable desde {state}: {result.code.value}"
        )
    return result


def _snapshot(finding: FindingStateRow) -> LifecycleSnapshot:
    previous = finding.previous_lifecycle_state
    return LifecycleSnapshot(
        state=finding.state,
        previous_state=None if previous is None else FindingLifecycleState(previous),
        due_at=finding.due_at,
    )


def _days_between(start: datetime, end: datetime) -> int:
    return max(0, (end - start) // timedelta(days=1))


def _moved(finding: FindingStateRow, accepted: Accepted, **changes: Any) -> FindingStateRow:
    state = accepted.state
    previous = accepted.previous_state
    leaves_overdue = state is not S.OVERDUE
    fields: dict[str, Any] = {
        "lifecycle_state": state.value,
        "previous_lifecycle_state": None if previous is None else previous.value,
    }
    if leaves_overdue:
        fields["overdue_days"] = 0
    fields.update(changes)
    return dataclasses.replace(finding, **fields)


def _classification(finding: FindingStateRow, item: ProjectionInput) -> tuple[FindingStateRow, str]:
    document = item.document
    outcome = ClassificationOutcome(document["outcome"])
    at = _instant(document["classified_at"])
    command = Classify(outcome, bool(document["reinforced_reason"]), at)
    accepted = _expect(item, _snapshot(finding), command)
    changes: dict[str, Any] = {
        "current_classification_outcome": outcome.value,
        "current_reason_category": str(document["reason_category"]),
        "classification_count": finding.classification_count + 1,
        "first_classified_at": finding.first_classified_at or at,
        "last_classified_at": at,
    }
    transition_id = accepted.transition.transition_id
    if transition_id == "T19":
        changes["closed_with_later_different_classification"] = (
            finding.closed_with_later_different_classification
            or outcome is not ClassificationOutcome.CONFIRMED
        )
    if transition_id == "T16" and accepted.state is S.OVERDUE and finding.due_at is not None:
        changes["overdue_since"] = finding.overdue_since or finding.due_at
        changes["overdue_days"] = _days_between(finding.due_at, at)
    return _moved(finding, accepted, **changes), transition_id


def _action_assigned(
    finding: FindingStateRow, item: ProjectionInput
) -> tuple[FindingStateRow, str | None]:
    document = item.document
    action_id = _uuid(document["action_id"])
    fields: dict[str, Any] = {
        "current_action_id": action_id,
        "current_action_type": ActionType(document["action_type"]).value,
        "assignee_user_id": _uuid(document["assignee_user_id"]),
        "due_at": _instant(document["due_at"]),
        "overdue_since": None,
        "overdue_days": 0,
        "assignee_inactive": False,
    }
    pending = (
        finding.state is S.ACTION_ASSIGNED
        and finding.current_action_id == action_id
        and finding.due_at is None
    )
    if pending:
        # Segunda mitad de T12, T13 o T14: la revisión o la reapertura ya movió el estado.
        return dataclasses.replace(finding, **fields), None
    accepted = _expect(item, _snapshot(finding), AssignAction())
    return _moved(finding, accepted, **fields), accepted.transition.transition_id


def _action_overdue(
    finding: FindingStateRow, item: ProjectionInput
) -> tuple[FindingStateRow, str | None]:
    document = item.document
    days = int(document["days_overdue"])
    if _uuid(document["action_id"]) != finding.current_action_id:
        raise ProjectionInconsistent(
            item.record_id, "vencimiento de una acción que no es la vigente"
        )
    if finding.state is S.OVERDUE:
        # Escalamiento de un vencimiento ya declarado (nivel manager_weekly): no es transición.
        return dataclasses.replace(finding, overdue_days=max(finding.overdue_days, days)), None
    accepted = _expect(item, _snapshot(finding), DeclareOverdue())
    changes = {"overdue_since": _instant(document["declared_at"]), "overdue_days": days}
    return _moved(finding, accepted, **changes), accepted.transition.transition_id


def _closure(finding: FindingStateRow, item: ProjectionInput) -> tuple[FindingStateRow, str]:
    document = item.document
    command = Close(
        evidence_entry_count=len(document["evidence_entry_ids"]),
        signer_has_license=bool(document["signer"].get("professional_license")),
    )
    accepted = _expect(item, _snapshot(finding), command)
    changes = {"closed_at": _instant(document["signed_at"]), "closure_record_id": item.record_id}
    return _moved(finding, accepted, **changes), accepted.transition.transition_id


def _unclosable(finding: FindingStateRow, item: ProjectionInput) -> tuple[FindingStateRow, str]:
    document = item.document
    command = DeclareUnclosable(
        fields_complete=True,
        policy=PolicyStatus.LOADED,
        review_date_in_range=True,
        signer_has_license=bool(document["signer"].get("professional_license")),
    )
    accepted = _expect(item, _snapshot(finding), command)
    changes = {
        "unclosable_since": _instant(document["declared_at"]),
        "unclosable_review_due_at": _instant(document["review_due_at"]),
        "unclosable_pending_review": False,
    }
    return _moved(finding, accepted, **changes), accepted.transition.transition_id


_NO_ACTION: Final[Mapping[str, Any]] = {
    "current_action_type": None,
    "assignee_user_id": None,
    "due_at": None,
    "overdue_since": None,
    "overdue_days": 0,
    "assignee_inactive": False,
}
"""La acción nueva de T12, T13 o T14 aún sin su registro ``action_assigned``."""


def _unclosable_review(
    finding: FindingStateRow, item: ProjectionInput
) -> tuple[FindingStateRow, str]:
    document = item.document
    outcome = UnclosableReviewOutcome(document["outcome"])
    accepted = _expect(item, _snapshot(finding), ReviewUnclosable(outcome))
    if outcome is UnclosableReviewOutcome.RENEWED:
        changes: dict[str, Any] = {
            "unclosable_review_due_at": _instant(document["new_review_due_at"]),
            "unclosable_pending_review": False,
        }
    else:
        changes = {
            **_NO_ACTION,
            "current_action_id": _uuid(document["new_action_id"]),
            "unclosable_since": None,
            "unclosable_review_due_at": None,
            "unclosable_pending_review": False,
        }
    return _moved(finding, accepted, **changes), accepted.transition.transition_id


def _reopening(finding: FindingStateRow, item: ProjectionInput) -> tuple[FindingStateRow, str]:
    document = item.document
    accepted = _expect(item, _snapshot(finding), Reopen(reason_given=bool(document["reason_text"])))
    changes = {
        **_NO_ACTION,
        "current_action_id": _uuid(document["new_action_id"]),
        "reopening_count": finding.reopening_count + 1,
        "closed_at": None,
        "closure_record_id": None,
        "unclosable_since": None,
        "unclosable_review_due_at": None,
        "unclosable_pending_review": False,
    }
    return _moved(finding, accepted, **changes), accepted.transition.transition_id


def _subject_step(
    finding: FindingStateRow, item: ProjectionInput
) -> tuple[FindingStateRow, str | None]:
    document = item.document
    match item.record_type:
        case "classification":
            return _classification(finding, item)
        case "review_resolution":
            outcome = ReviewResolutionOutcome(document["outcome"])
            accepted = _expect(item, _snapshot(finding), ResolveReview(outcome))
            return _moved(finding, accepted), accepted.transition.transition_id
        case "action_assigned":
            return _action_assigned(finding, item)
        case "action_overdue":
            return _action_overdue(finding, item)
        case "closure":
            return _closure(finding, item)
        case "unclosable_state_declared":
            return _unclosable(finding, item)
        case "unclosable_review":
            return _unclosable_review(finding, item)
        case "reopening":
            return _reopening(finding, item)
        case "root_cause_analysis":
            signed = document["state"] == "signed"
            return dataclasses.replace(
                finding, has_signed_root_cause=finding.has_signed_root_cause or signed
            ), None
        case "late_segment_attached":
            accepted = _expect(item, _snapshot(finding), AttachLateSegment())
            return finding, accepted.transition.transition_id
    raise ProjectionInconsistent(
        item.record_id, f"tipo sin efecto sobre el sujeto: {item.record_type}"
    )


def _already_applied(
    episode: EpisodeRow | None, finding: FindingStateRow | None, item: ProjectionInput
) -> bool:
    if finding is not None:
        if finding.last_record_id == item.record_id:
            return True
        if item.event_id is not None and finding.last_event_id == item.event_id:
            return True
    return episode is not None and item.record_id in episode.segment_record_ids


def step(
    episode: EpisodeRow | None, finding: FindingStateRow | None, item: ProjectionInput
) -> Step:
    """Aplica ``item`` a las filas de su sujeto (``subject_id_of``): el núcleo del pliegue.

    ``episode`` y ``finding`` son las filas actuales del sujeto (o ``None``). Puro y
    determinista: el resultado depende solo de las filas y del registro."""
    if item.record_type not in PROJECTED_RECORD_TYPES or item.record_type in _NO_SUBJECT_EFFECT:
        return Step(episode, finding, False)
    if _already_applied(episode, finding, item):
        return Step(episode, finding, False)
    if item.record_type == "finding_received":
        return _finding_received(episode, finding, item)
    if item.record_type == "detection_for_review_received":
        if finding is not None:
            return Step(episode, finding, False)
        accepted = _expect(item, LifecycleSnapshot(None), ReceiveDetection())
        created = _new_finding(
            item,
            subject_kind=SubjectKind.DETECTION,
            subject_id=_uuid(item.document["detection_id"]),
            state=accepted.state,
            # La detección no declara tier: no se afirma más evidencia de la que hay (P6).
            tier="tier_2",
            missing_segments=False,
        )
        return Step(None, created, True, accepted.transition.transition_id)
    if finding is None:
        raise ProjectionInconsistent(item.record_id, "el sujeto no existe en la proyección")
    updated, transition_id = _subject_step(finding, item)
    return Step(episode, _finalize_finding(updated, item), True, transition_id)


# --- la partición ---------------------------------------------------------------------------


def episode_term(row: EpisodeRow) -> int:
    return partition_term("episode", row.episode_id, row.digest())


def finding_term(row: FindingStateRow) -> int:
    return partition_term("finding_state", row.finding_state_id, row.projection_digest)


@dataclass
class PlantProjection:
    """La proyección de una partición ``(organization_id, plant_id)`` en memoria."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    episodes: dict[uuid.UUID, EpisodeRow] = field(default_factory=dict)
    findings: dict[uuid.UUID, FindingStateRow] = field(default_factory=dict)
    """Por ``subject_id`` (el episodio o la detección)."""
    last_applied_ledger_sequence: int = 0
    last_applied_event_id: uuid.UUID | None = None
    projection_digest: str = EMPTY_PARTITION_DIGEST

    def apply(self, item: ProjectionInput) -> Step:
        """Aplica ``item`` y actualiza el control de la partición."""
        if item.organization_id != self.organization_id or item.plant_id != self.plant_id:
            raise ValueError("el registro no es de esta partición")
        subject = subject_id_of(item) if item.record_type in PROJECTED_RECORD_TYPES else None
        episode = self.episodes.get(subject) if subject is not None else None
        finding = self.findings.get(subject) if subject is not None else None
        result = step(episode, finding, item) if subject is not None else Step(None, None, False)
        if subject is not None and result.applied:
            digest = self.projection_digest
            if result.episode is not None:
                old = None if episode is None else episode_term(episode)
                digest = replace_term(digest, old, episode_term(result.episode))
                self.episodes[subject] = result.episode
            if result.finding is not None:
                old = None if finding is None else finding_term(finding)
                digest = replace_term(digest, old, finding_term(result.finding))
                self.findings[subject] = result.finding
            self.projection_digest = digest
        self.last_applied_ledger_sequence = max(
            self.last_applied_ledger_sequence, item.chain_sequence
        )
        if item.event_id is not None:
            self.last_applied_event_id = item.event_id
        return result

    def state(self) -> tuple[dict[uuid.UUID, EpisodeRow], dict[uuid.UUID, FindingStateRow], str]:
        """Lo comparable de la partición: filas y resumen (sin la contabilidad del control)."""
        return dict(self.episodes), dict(self.findings), self.projection_digest


def fold(items: Iterable[ProjectionInput]) -> dict[tuple[uuid.UUID, uuid.UUID], PlantProjection]:
    """Reconstrucción pura: cada partición, en orden de ``chain_sequence`` (§9).

    Los registros de organización (sin planta) no son de ninguna partición del lazo."""
    partitions: dict[tuple[uuid.UUID, uuid.UUID], list[ProjectionInput]] = {}
    for item in items:
        if item.plant_id is not None:
            partitions.setdefault((item.organization_id, item.plant_id), []).append(item)
    result: dict[tuple[uuid.UUID, uuid.UUID], PlantProjection] = {}
    for key in sorted(partitions, key=lambda pair: (str(pair[0]), str(pair[1]))):
        projection = PlantProjection(*key)
        for item in sorted(partitions[key], key=lambda entry: entry.chain_sequence):
            projection.apply(item)
        result[key] = projection
    return result
