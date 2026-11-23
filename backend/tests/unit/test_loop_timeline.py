"""La ficha de vida del hallazgo, composición pura (TASK-585; BR-LAZ-06, 39, 46 y 112).

- **El plazo de clasificación es el de la métrica**: para todo historial generado de recepciones,
  cambios de configuración (antes y después de la recepción, también después de la
  clasificación) y clasificaciones, ``met`` de la primera clasificación coincide con el numerador
  de ``classified_in_time`` de ``compute_metrics`` para ese hallazgo (la misma función pura,
  ``loop.config.classification_deadline``). El caso del 22-11 (la configuración nace después de
  la clasificación) va además como ejemplo fijo, en los dos sentidos.
- **Un hito por registro**, en orden de expediente, con su ``record_id``; un tipo fuera de la
  lista sale como ``other`` con su tipo.
- **Una sola hora**: en zona unipersonal todas las horas y los tiempos entre hitos salen de las
  horas agregadas.
- **El plazo de la acción**: cerrada antes de ``due_at``, a tiempo; con un vencimiento declarado
  antes del cierre o cerrada tarde, fuera de plazo; abierta y sin vencimiento, sin saberse.
- **Quién**: solo en los hitos cuyo registro lo trae, con el rol y el nombre, nunca un
  identificador de usuario.

Solo datos generados; ningún reloj (la ficha no lee la hora).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.writer_support import unit_context
from vigia_platform.accreditation.metrics.compute import compute_metrics
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.fleet.application.finding_photos import FindingPhoto, FindingPhotoKey
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import ResourceRef
from vigia_platform.ledger.application.reader import (
    LectorExpediente,
    LedgerQueryInvalid,
    LedgerRecordView,
    RecordActor,
    SubjectHistory,
    SubjectRef,
)
from vigia_platform.loop.adapters.http.timeline_routes import timeline_view
from vigia_platform.loop.application.timeline import (
    CONFIGURATION_RECORD_TYPE,
    MAX_TIMELINE_MILESTONES,
    FindingTimeline,
    FindingTimelineService,
    MilestoneActor,
    MilestoneDetails,
    MilestoneKind,
    TimelineInputs,
    TimelineSource,
    finding_timeline,
    photo_documents,
)
from vigia_platform.loop.calendar import business_deadline
from vigia_platform.loop.config.classification_deadline import (
    ClassificationDeadlineRule,
    classified_in_time,
)
from vigia_platform.loop.domain.enums import MetricKey, MetricStatus, MetricUnit
from vigia_platform.loop.projection.fold import ProjectionInput
from vigia_platform.shared.context import ActorUnit, ScopeContext

ORG: Final = uuid.UUID(int=1)
PLANT: Final = uuid.UUID(int=2)
ZONE: Final = uuid.UUID(int=3)
SUBJECT: Final = uuid.UUID(int=100)
SIGNER: Final = uuid.UUID(int=900)
START: Final = datetime(2026, 3, 1, tzinfo=UTC)
END: Final = datetime(2026, 4, 1, tzinfo=UTC)
PREVIOUS: Final = datetime(2026, 2, 1, tzinfo=UTC)
WINDOW: Final = 60
TIMEZONES: Final = ("UTC", "America/Bogota", "Asia/Tokyo")


def stamp(at: datetime) -> str:
    return at.isoformat()


# --- documentos sintéticos ---------------------------------------------------------------------


def received_document(at: datetime, subject: uuid.UUID = SUBJECT) -> dict[str, Any]:
    return {
        "plant_id": str(PLANT),
        "zone_id": str(ZONE),
        "family": "coexistence",
        "tier": "tier_1",
        "standard": {"standard_id": str(uuid.UUID(int=7)), "version": 1},
        "node_time": {"started_at": stamp(at), "ended_at": stamp(at + timedelta(seconds=1))},
        "observability_state": "observable",
        "max_confidence": 0.9,
        "episode_duration_ms": 1000,
        "condition_duration_ms": 1000,
        "cameras": [],
        "episode": {"episode_id": str(subject), "segment_index": 0, "continues": False},
    }


def classification_document(at: datetime, sequence: int = 1) -> dict[str, Any]:
    """La primera confirma; una reclasificación descarta (la máquina exige cambiar de salida)."""
    first = sequence == 1
    return {
        "subject_id": str(SUBJECT),
        "subject_kind": "finding",
        "sequence_in_subject": sequence,
        "outcome": "confirmed" if first else "false_positive",
        "reason_category": "real_coexistence" if first else "no_person_present",
        "reason_text": "Coexistencia sintética observada en el pasillo de carga de la zona",
        "is_reclassification": sequence > 1,
        "reinforced_reason": sequence > 1,
        "signer": {
            "user_id": str(SIGNER),
            "display_name": "Coordinación SST sintética",
            "role_in_use": "coordinator_sst",
        },
        "classified_at": stamp(at),
    }


def configuration_document(
    at: datetime, days: int | None, weekdays: Sequence[int] | None = None
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    if days is not None:
        values.append({"parameter": "classification_deadline_business_days", "integer_value": days})
    if weekdays is not None:
        values.append(
            {
                "parameter": "business_calendar",
                "business_calendar": {
                    "working_weekdays": sorted(weekdays),
                    "non_working_dates": [],
                },
            }
        )
    if not values:
        values.append({"parameter": "sample_percent", "integer_value": 10})
    return {"plant_id": str(PLANT), "new_values": values, "changed_at": stamp(at)}


@st.composite
def histories(draw: st.DrawFn) -> tuple[list[ProjectionInput], str]:
    """Una recepción, su clasificación (y a veces una reclasificación) y de cero a cuatro cambios
    de configuración en cualquier punto: antes de la recepción, entre las dos o después de la
    clasificación. Todo dentro de marzo de 2026 (el periodo de la métrica)."""
    timezone = draw(st.sampled_from(TIMEZONES))
    received = START + timedelta(days=draw(st.integers(0, 9)), minutes=draw(st.integers(0, 1439)))
    classified = received + timedelta(minutes=draw(st.integers(0, 18 * 1440)))
    events: list[tuple[str, dict[str, Any], datetime]] = [
        ("finding_received", received_document(received), received),
        ("classification", classification_document(classified), classified),
    ]
    if draw(st.booleans()):
        again = classified + timedelta(minutes=draw(st.integers(1, 1440)))
        events.append(("classification", classification_document(again, 2), again))
    latest = END - timedelta(minutes=1)
    for _ in range(draw(st.integers(0, 4))):
        at = min(
            latest,
            max(START, received + timedelta(minutes=draw(st.integers(-10 * 1440, 20 * 1440)))),
        )
        days = draw(st.one_of(st.none(), st.integers(1, 10)))
        weekdays = draw(
            st.one_of(st.none(), st.sets(st.integers(1, 7), min_size=1, max_size=7).map(sorted))
        )
        events.append(
            ("loop_configuration_changed", configuration_document(at, days, weekdays), at)
        )
    ordered = sorted(enumerate(events), key=lambda pair: (pair[1][2], pair[0]))
    records = [
        ProjectionInput(
            record_id=uuid.UUID(int=1000 + sequence),
            organization_id=ORG,
            plant_id=PLANT,
            chain_sequence=sequence,
            record_type=kind,
            received_at=at,
            document=document,
        )
        for sequence, (_, (kind, document, at)) in enumerate(ordered, start=1)
    ]
    return records, timezone


def metric_numerator(records: Sequence[ProjectionInput], timezone: str) -> int:
    values = compute_metrics(
        records,
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        start=START,
        end=END,
        previous_start=PREVIOUS,
        cutoff=END,
        timezone=timezone,
        approved=True,
        adoption_count=0,
        exposure=MetricValue(
            key=MetricKey.EXPOSURE,
            status=MetricStatus.COMPUTED,
            unit=MetricUnit.MINUTES,
            value=0.0,
            excluded_unclosable_count=0,
        ),
    )
    (metric,) = [v for v in values if v.key is MetricKey.CLASSIFIED_IN_TIME]
    assert metric.denominator == 1, metric
    assert metric.numerator is not None
    return metric.numerator


def subject_row(
    *, single: bool = False, window: int = WINDOW, state: str = "open", kind: str = "finding"
) -> dict[str, Any]:
    return {
        "finding_state_id": str(uuid.UUID(int=1000)),
        "subject_kind": kind,
        "plant_id": str(PLANT),
        "zone_id": str(ZONE),
        "family": "coexistence",
        "lifecycle_state": state,
        "single_occupancy_zone": single,
        "aggregation_window_minutes": window,
    }


def source(item: ProjectionInput, **changes: Any) -> TimelineSource:
    return TimelineSource(
        record_id=item.record_id,
        chain_sequence=item.chain_sequence,
        record_type=item.record_type,
        received_at=item.received_at,
        document=item.document,
        **changes,
    )


def timeline_of(records: Sequence[ProjectionInput], timezone: str) -> FindingTimeline:
    """La ficha con lo que lee el servicio: los registros del sujeto y la configuración de la
    planta (aquí, **toda**, también la posterior: la ficha solo debe contar la anterior)."""
    subject = [r for r in records if r.record_type != "loop_configuration_changed"]
    configuration = [r for r in records if r.record_type == "loop_configuration_changed"]
    return finding_timeline(
        TimelineInputs(
            subject=subject_row(),
            records=tuple(source(r) for r in subject),
            truncated=False,
            configuration=tuple(source(r) for r in configuration),
            timezone=timezone,
        )
    )


def first_classification_met(timeline: FindingTimeline) -> bool | None:
    first = next(m for m in timeline.milestones if m.kind is MilestoneKind.CLASSIFICATION)
    assert first.deadline is not None
    return first.deadline.met


# --- el plazo de clasificación es el de la métrica ----------------------------------------------


@given(histories())
def test_classification_met_equals_the_numerator_of_the_metric(
    history: tuple[list[ProjectionInput], str],
) -> None:
    records, timezone = history
    timeline = timeline_of(records, timezone)
    met = first_classification_met(timeline)
    assert met is (metric_numerator(records, timezone) == 1)
    assert timeline.summary.classification_met is met


def _late_case(after_days: int, config_days: int) -> tuple[list[ProjectionInput], str]:
    """Recibido el lunes 2 de marzo a las 16:40Z, clasificado ``after_days`` días naturales
    después y la configuración del lazo creada **dos minutos después** de la clasificación (el
    caso del 22-11), con ``config_days`` días hábiles."""
    received = datetime(2026, 3, 2, 16, 40, tzinfo=UTC)
    classified = received + timedelta(days=after_days, minutes=8)
    created = classified + timedelta(minutes=2, seconds=47)
    events = [
        ("finding_received", received_document(received), received),
        ("classification", classification_document(classified), classified),
        ("loop_configuration_changed", configuration_document(created, config_days), created),
    ]
    return [
        ProjectionInput(
            record_id=uuid.UUID(int=2000 + index),
            organization_id=ORG,
            plant_id=PLANT,
            chain_sequence=index,
            record_type=kind,
            received_at=at,
            document=document,
        )
        for index, (kind, document, at) in enumerate(events, start=1)
    ], "UTC"


def test_a_configuration_created_after_the_classification_never_changes_the_deadline() -> None:
    # Fuera de plazo con la de fábrica (3 días hábiles); con la posterior (10), «a tiempo».
    late, timezone = _late_case(after_days=8, config_days=10)
    assert first_classification_met(timeline_of(late, timezone)) is False
    assert metric_numerator(late, timezone) == 0
    # A tiempo con la de fábrica; con la posterior (1 día), «fuera de plazo».
    early, timezone = _late_case(after_days=2, config_days=1)
    assert first_classification_met(timeline_of(early, timezone)) is True
    assert metric_numerator(early, timezone) == 1


def test_the_rule_is_one_function_for_the_metric_and_the_timeline() -> None:
    """La regla compartida: el fin exclusivo del plazo y el booleano, con su borde exacto."""
    rule = ClassificationDeadlineRule.defaults()
    received = datetime(2026, 3, 2, 16, 40, tzinfo=UTC)  # lunes
    due = rule.deadline(received, "UTC")
    assert due == datetime(2026, 3, 6, tzinfo=UTC)  # fin del jueves 5: tres días hábiles
    assert due == business_deadline(received, 3, rule.calendar, "UTC")
    assert classified_in_time(due - timedelta(milliseconds=1), due) is True
    assert classified_in_time(due, due) is False
    changed = rule.after_change(
        [
            {"parameter": "classification_deadline_business_days", "integer_value": 1},
            {
                "parameter": "business_calendar",
                "business_calendar": {"working_weekdays": [1, 2, 3], "non_working_dates": []},
            },
        ]
    )
    assert (changed.business_days, sorted(changed.calendar.working_weekdays)) == (1, [1, 2, 3])
    assert changed.deadline(received, "UTC") == datetime(2026, 3, 4, tzinfo=UTC)
    assert rule.after_change([{"parameter": "sample_percent", "integer_value": 5}]) == rule


def test_without_the_plant_timezone_or_the_configuration_the_deadline_is_unknown() -> None:
    records, _ = _late_case(after_days=8, config_days=10)
    subject = [r for r in records if r.record_type != "loop_configuration_changed"]
    for configuration, timezone in (((), None), (None, "UTC")):
        timeline = finding_timeline(
            TimelineInputs(
                subject=subject_row(),
                records=tuple(source(r) for r in subject),
                truncated=False,
                configuration=configuration,
                timezone=timezone,
            )
        )
        (classification,) = [
            m for m in timeline.milestones if m.kind is MilestoneKind.CLASSIFICATION
        ]
        assert classification.deadline is None
        assert timeline.summary.classification_met is None


def test_a_detection_has_no_classification_deadline() -> None:
    records, timezone = _late_case(after_days=8, config_days=10)
    subject = [r for r in records if r.record_type != "loop_configuration_changed"]
    timeline = finding_timeline(
        TimelineInputs(
            subject=subject_row(kind="detection"),
            records=tuple(source(r) for r in subject),
            truncated=False,
            configuration=(),
            timezone=timezone,
        )
    )
    assert [m.deadline for m in timeline.milestones] == [None, None]


# --- el ciclo completo ---------------------------------------------------------------------------

ACTION: Final = uuid.UUID(int=500)
NEW_ACTION: Final = uuid.UUID(int=501)
T0: Final = datetime(2026, 3, 2, 10, 7, 13, 250_000, tzinfo=UTC)  # lunes, fuera de la cuadrícula


def _user(name: str = "Coordinación SST sintética") -> dict[str, Any]:
    return {"user_id": str(SIGNER), "display_name": name, "role_in_use": "coordinator_sst"}


def _signer() -> dict[str, Any]:
    return {
        "user_id": str(SIGNER),
        "display_name": "Coordinación SST sintética",
        "professional_license": "LIC-SINTETICA-01",
    }


def cycle(
    *, overdue: bool = True, close_offset: timedelta = timedelta(days=3, minutes=13)
) -> list[TimelineSource]:
    """Recibido, clasificación, acción, dos evidencias, vencimiento, cierre firmado, causa raíz,
    reapertura con su acción nueva y un registro del sujeto de un tipo que no está en la lista."""
    due = T0 + timedelta(days=2, hours=5)
    steps: list[tuple[str, dict[str, Any], timedelta]] = [
        ("finding_received", received_document(T0), timedelta(0)),
        (
            "classification",
            classification_document(T0 + timedelta(minutes=37)),
            timedelta(minutes=37),
        ),
        (
            "action_assigned",
            {
                "action_id": str(ACTION),
                "subject_id": str(SUBJECT),
                "action_type": "engineering",
                "action_description": "Instalar un resguardo fijo en el acceso a la zona",
                "expected_outcome": "Sin paso peatonal por el frente de la prensa",
                "assignee_user_id": str(uuid.UUID(int=901)),
                "assignee_display_name": "Mantenimiento sintético",
                "due_at": stamp(due),
                "deadline_days": 2,
                "assigned_by": _user(),
                "assigned_at": stamp(T0 + timedelta(hours=1, minutes=11)),
            },
            timedelta(hours=1, minutes=11),
        ),
        (
            "action_evidence_added",
            {
                "evidence_entry_id": str(uuid.UUID(int=600)),
                "action_id": str(ACTION),
                "subject_id": str(SUBJECT),
                "description": "Resguardo fijo instalado y anclado al piso en el acceso",
                "verification_note": "Anclaje comprobado en la ronda",
                "performed_at": stamp(T0 + timedelta(days=1)),
                "performed_by_role": "Técnico de mantenimiento",
                "attachments": [],
                "added_by": _user(),
                "added_at": stamp(T0 + timedelta(days=1, minutes=3)),
            },
            timedelta(days=1, minutes=3),
        ),
        (
            "action_evidence_added",
            {
                "evidence_entry_id": str(uuid.UUID(int=601)),
                "action_id": str(ACTION),
                "subject_id": str(SUBJECT),
                "description": "Señalización del pasillo peatonal repintada en la zona",
                "performed_at": stamp(T0 + timedelta(days=1, hours=2)),
                "performed_by_role": "Técnico de mantenimiento",
                "attachments": [],
                "added_by": _user(),
                "added_at": stamp(T0 + timedelta(days=1, hours=2, minutes=29)),
            },
            timedelta(days=1, hours=2, minutes=29),
        ),
    ]
    if overdue:
        steps.append(
            (
                "action_overdue",
                {
                    "overdue_id": str(uuid.UUID(int=700)),
                    "action_id": str(ACTION),
                    "subject_id": str(SUBJECT),
                    "due_at": stamp(due),
                    "declared_at": stamp(due + timedelta(hours=1)),
                    "days_overdue": 1,
                    "escalation_level": "coordinator",
                    "assignee_user_id": str(uuid.UUID(int=901)),
                    "assignee_inactive": False,
                },
                timedelta(days=2, hours=6, minutes=1),
            )
        )
    steps += [
        (
            "closure",
            {
                "closure_id": str(uuid.UUID(int=800)),
                "subject_id": str(SUBJECT),
                "action_id": str(ACTION),
                "action_type": "engineering",
                "closure_description": "Resguardo verificado y anclado en el acceso de la zona",
                "verification_note": "Ronda de cierre sin hallazgos",
                "evidence_entry_ids": [str(uuid.UUID(int=600))],
                "signer": _signer(),
                "signed_at": stamp(T0 + close_offset),
            },
            close_offset,
        ),
        (
            "root_cause_analysis",
            {
                "analysis_id": str(uuid.UUID(int=810)),
                "subject_id": str(SUBJECT),
                "state": "signed",
                "root_cause_category": "engineering",
                "root_cause_statement": "El trazado mezcla los flujos de carga y de paso",
                "conclusion": "Separar físicamente los flujos con un resguardo fijo",
                "signer": _signer(),
                "signed_at": stamp(T0 + close_offset + timedelta(hours=2)),
            },
            close_offset + timedelta(hours=2, minutes=17),
        ),
        (
            "reopening",
            {
                "reopening_id": str(uuid.UUID(int=820)),
                "subject_id": str(SUBJECT),
                "from_state": "closed",
                "reason_text": "El resguardo se retiró durante el mantenimiento mayor",
                "new_action_id": str(NEW_ACTION),
                "reopened_by": _user(),
                "reopened_at": stamp(T0 + close_offset + timedelta(days=1)),
            },
            close_offset + timedelta(days=1, minutes=41),
        ),
        (
            "action_assigned",
            {
                "action_id": str(NEW_ACTION),
                "subject_id": str(SUBJECT),
                "action_type": "process",
                "action_description": "Bloquear el retiro del resguardo en el mantenimiento",
                "assignee_user_id": str(uuid.UUID(int=901)),
                "assignee_display_name": "Mantenimiento sintético",
                "due_at": stamp(T0 + close_offset + timedelta(days=9)),
                "deadline_days": 7,
                "supersedes_action_id": str(ACTION),
                "assigned_by": _user(),
                "assigned_at": stamp(T0 + close_offset + timedelta(days=1)),
            },
            close_offset + timedelta(days=1, minutes=41, seconds=1),
        ),
        (
            "export_produced",
            {"subject_id": str(SUBJECT), "export_id": str(uuid.UUID(int=830))},
            close_offset + timedelta(days=2, minutes=5),
        ),
    ]
    return [
        TimelineSource(
            record_id=uuid.UUID(int=3000 + index),
            chain_sequence=40 + index,
            record_type=kind,
            received_at=T0 + offset,
            document=document,
            actor_kind="user" if kind in ("closure", "root_cause_analysis") else None,
            actor_id=SIGNER if kind in ("closure", "root_cause_analysis") else None,
            actor_role_in_use="coordinator_sst"
            if kind in ("closure", "root_cause_analysis")
            else None,
        )
        for index, (kind, document, offset) in enumerate(steps)
    ]


def closed_cycle(close_offset: timedelta) -> list[TimelineSource]:
    """El ciclo sin vencimiento hasta la causa raíz: sin la reapertura ni su acción nueva."""
    return [
        r
        for r in cycle(overdue=False, close_offset=close_offset)
        if r.record_type != "reopening" and r.document.get("action_id") != str(NEW_ACTION)
    ]


def compose(
    records: Sequence[TimelineSource],
    *,
    single: bool = False,
    state: str = "action_assigned",
    truncated: bool = False,
) -> FindingTimeline:
    return finding_timeline(
        TimelineInputs(
            subject=subject_row(single=single, state=state),
            records=tuple(records),
            truncated=truncated,
            configuration=(),
            timezone="UTC",
        )
    )


EXPECTED_KINDS: Final = [
    MilestoneKind.FINDING_RECEIVED,
    MilestoneKind.CLASSIFICATION,
    MilestoneKind.ACTION_ASSIGNED,
    MilestoneKind.ACTION_EVIDENCE_ADDED,
    MilestoneKind.ACTION_EVIDENCE_ADDED,
    MilestoneKind.ACTION_OVERDUE,
    MilestoneKind.CLOSURE,
    MilestoneKind.ROOT_CAUSE_ANALYSIS,
    MilestoneKind.REOPENING,
    MilestoneKind.ACTION_ASSIGNED,
    MilestoneKind.OTHER,
]


def test_one_milestone_per_record_in_ledger_order_with_its_record_id() -> None:
    records = cycle()
    timeline = compose(records, truncated=True)
    assert [m.kind for m in timeline.milestones] == EXPECTED_KINDS
    assert [(m.record_id, m.chain_sequence, m.record_type) for m in timeline.milestones] == [
        (r.record_id, r.chain_sequence, r.record_type) for r in records
    ]
    other = timeline.milestones[-1]
    assert (other.record_type, other.actor, other.deadline) == ("export_produced", None, None)
    assert timeline.truncated is True
    assert compose(records).truncated is False


def test_the_elapsed_time_is_the_difference_of_the_shown_times() -> None:
    records = cycle()
    timeline = compose(records)
    shown = [m.at for m in timeline.milestones]
    assert shown == [r.received_at for r in records]
    assert [m.elapsed_ms_since_previous for m in timeline.milestones] == [None] + [
        int((later - earlier) / timedelta(milliseconds=1))
        for earlier, later in itertools.pairwise(shown)
    ]


def _on_grid(moment: datetime, window: int = WINDOW) -> bool:
    return (moment - datetime(1970, 1, 1, tzinfo=UTC)) % timedelta(minutes=window) == timedelta(0)


def _instants(timeline: FindingTimeline) -> Iterator[datetime]:
    yield from (m for m in (timeline.summary.received_at, timeline.summary.closed_at) if m)
    for milestone in timeline.milestones:
        yield milestone.at
        if milestone.deadline is not None:
            yield milestone.deadline.due_at
        details = milestone.details
        for moment in (details.due_at, details.performed_at, details.review_due_at):
            if moment is not None:
                yield moment


def test_in_a_single_occupancy_zone_every_time_and_every_gap_comes_from_the_aggregated_times() -> (
    None
):
    records = [*cycle(), *extras()]
    timeline = compose(records, single=True, state="closed")
    assert timeline.time_aggregated is True
    assert timeline.aggregation_window_minutes == WINDOW
    instants = list(_instants(timeline))
    assert len(instants) > len(records)
    assert all(_on_grid(moment) for moment in instants), instants
    window_ms = WINDOW * 60_000
    gaps = [m.elapsed_ms_since_previous for m in timeline.milestones[1:]]
    assert all(gap is not None and gap % window_ms == 0 for gap in gaps), gaps
    # La sonda: con las horas exactas, al menos un tiempo no sería múltiplo de la ventana.
    exact = [r.received_at for r in records]
    assert any((b - a) % timedelta(minutes=WINDOW) for a, b in itertools.pairwise(exact))
    # Fuera de zona unipersonal, las horas son las exactas.
    assert compose(records).aggregation_window_minutes is None


def test_the_action_deadline_is_met_only_when_closed_before_its_frozen_due_date() -> None:
    def action_deadlines(records: Sequence[TimelineSource]) -> list[bool | None]:
        return [
            m.deadline.met
            for m in compose(records).milestones
            if m.kind is MilestoneKind.ACTION_ASSIGNED and m.deadline is not None
        ]

    # Vencida antes del cierre (BR-LAZ-46): fuera de plazo; la acción nueva sigue abierta.
    assert action_deadlines(cycle()) == [False, None]
    # Cerrada antes del plazo congelado y sin vencimiento: a tiempo.
    assert action_deadlines(cycle(overdue=False, close_offset=timedelta(days=1, hours=5))) == [
        True,
        None,
    ]
    # Cerrada tarde sin que la tarea declarara el vencimiento: fuera de plazo.
    assert action_deadlines(cycle(overdue=False)) == [False, None]
    # Justo en el plazo (fin exclusivo): fuera de plazo.
    on_due = cycle(overdue=False, close_offset=timedelta(days=2, hours=5))
    assert action_deadlines(on_due) == [False, None]
    # Abierta y en plazo: aún no se sabe.
    assert action_deadlines(cycle()[:5]) == [None]
    # Abierta con el vencimiento declarado: fuera de plazo aunque aún no se cierre (BR-LAZ-46).
    assert action_deadlines(cycle()[:6]) == [False]
    # Un vencimiento que llega después del cierre no cambia lo cerrado a tiempo.
    in_time = closed_cycle(close_offset=timedelta(days=1, hours=5))
    late_notice = TimelineSource(
        record_id=uuid.UUID(int=4100),
        chain_sequence=in_time[-1].chain_sequence + 1,
        record_type="action_overdue",
        received_at=in_time[-1].received_at + timedelta(days=3),
        document={"action_id": str(ACTION), "subject_id": str(SUBJECT), "days_overdue": 1},
    )
    assert action_deadlines([*in_time, late_notice]) == [True]


def test_the_summary_says_received_closed_business_days_and_both_deadlines() -> None:
    timeline = compose(closed_cycle(close_offset=timedelta(days=1, hours=5)), state="closed")
    summary = timeline.summary
    assert summary.received_at == T0
    (closure,) = [m for m in timeline.milestones if m.kind is MilestoneKind.CLOSURE]
    assert summary.closed_at == closure.at
    assert summary.business_days_to_close == 1  # lunes 2 → martes 3 de marzo
    assert summary.classification_met is True  # 37 minutos después, con la de fábrica
    assert summary.action_met is True
    # Reabierto: ya no está cerrado y la acción vigente sigue abierta.
    reopened = compose(cycle(), state="action_assigned").summary
    assert (reopened.closed_at, reopened.business_days_to_close, reopened.action_met) == (
        None,
        None,
        None,
    )


def test_business_days_to_close_follow_the_plant_calendar() -> None:
    # Cierre el jueves 5: tres días hábiles con lunes a viernes; uno solo si la planta trabaja
    # solo los martes (un cambio anterior a la recepción).
    closed = closed_cycle(close_offset=timedelta(days=3))
    change = TimelineSource(
        record_id=uuid.UUID(int=4000),
        chain_sequence=1,
        record_type="loop_configuration_changed",
        received_at=T0 - timedelta(days=1),
        document=configuration_document(T0 - timedelta(days=1), None, [2]),
    )
    for configuration, days in (((), 3), ((change,), 1)):
        timeline = finding_timeline(
            TimelineInputs(
                subject=subject_row(state="closed"),
                records=tuple(closed),
                truncated=False,
                configuration=configuration,
                timezone="UTC",
            )
        )
        assert timeline.summary.business_days_to_close == days


def test_the_actor_appears_only_where_the_record_carries_it_and_never_with_an_identifier() -> None:
    timeline = compose(cycle())
    actors = [(m.kind, m.actor) for m in timeline.milestones]
    with_actor = [kind for kind, actor in actors if actor is not None]
    assert with_actor == [
        MilestoneKind.CLASSIFICATION,
        MilestoneKind.ACTION_ASSIGNED,
        MilestoneKind.ACTION_EVIDENCE_ADDED,
        MilestoneKind.ACTION_EVIDENCE_ADDED,
        MilestoneKind.CLOSURE,
        MilestoneKind.ROOT_CAUSE_ANALYSIS,
        MilestoneKind.REOPENING,
        MilestoneKind.ACTION_ASSIGNED,
    ]
    for _, actor in actors:
        if actor is not None:
            assert (actor.role_in_use, actor.display_name) == (
                "coordinator_sst",
                "Coordinación SST sintética",
            )
    body = timeline_view(timeline).model_dump(mode="json")
    keys = set(_keys(body))
    assert not keys & {"user_id", "assignee_user_id", "responsible_user_id", "actor_id"}
    assert {frozenset(m["actor"]) for m in body["milestones"] if m["actor"] is not None} == {
        frozenset({"role_in_use", "display_name"})
    }


def test_a_licensed_signer_takes_its_role_only_from_its_own_record_envelope() -> None:
    records = cycle()
    closure = next(r for r in records if r.record_type == "closure")
    (shown,) = [
        m.actor
        for m in compose([records[0], closure]).milestones
        if m.kind is MilestoneKind.CLOSURE
    ]
    assert shown is not None and shown.role_in_use == "coordinator_sst"
    for changes in ({"actor_id": uuid.UUID(int=999)}, {"actor_kind": "system"}):
        (other,) = [
            m.actor
            for m in compose([records[0], replace(closure, **changes)]).milestones
            if m.kind is MilestoneKind.CLOSURE
        ]
        assert other is not None
        assert (other.role_in_use, other.display_name) == (None, "Coordinación SST sintética")


RESPONSIBLE: Final = "Responsable sintético del incerrable"
LATE: Final = T0 + timedelta(days=9, minutes=7, seconds=11)


def extras() -> list[TimelineSource]:
    """Los cinco tipos que el ciclo no recorre: tramo tardío, resolución de la cola, adjunto no
    recuperable, declaración SIN ALTERNATIVA SEGURA DISPONIBLE y su revisión (horas fuera de la
    cuadrícula, para la prueba unipersonal)."""
    review_due = LATE + timedelta(days=30, minutes=13)
    steps: list[tuple[str, dict[str, Any], timedelta, bool]] = [
        (
            "late_segment_attached",
            {
                "segment_record_id": str(uuid.UUID(int=5001)),
                "episode_id": str(SUBJECT),
                "segment_index": 1,
                "received_at": stamp(LATE),
                "episode_state_at_arrival": "closed",
                "reopened_loop": False,
                "delay_ms": 3_456_789,
            },
            timedelta(0),
            False,
        ),
        (
            "review_resolution",
            {
                "resolution_id": str(uuid.UUID(int=5002)),
                "detection_id": str(SUBJECT),
                "outcome": "review_confirmed",
                "reason_category": "real_coexistence",
                "reason_text": "Coexistencia sintética confirmada al revisar la detección",
                "resolved_by": _user(),
                "resolved_at": stamp(LATE + timedelta(minutes=3)),
            },
            timedelta(minutes=3, seconds=17),
            False,
        ),
        (
            "closure_attachment_unrecoverable",
            {
                "declaration_id": str(uuid.UUID(int=5003)),
                "subject_id": str(SUBJECT),
                "action_id": str(ACTION),
                "attachment_id": str(uuid.UUID(int=5004)),
                "sha256": "d" * 64,
                "detected_at": stamp(LATE + timedelta(minutes=41, seconds=5)),
                "previous_version_available": False,
            },
            timedelta(minutes=47, seconds=29),
            False,
        ),
        (
            "unclosable_state_declared",
            {
                "declaration_id": str(uuid.UUID(int=5005)),
                "subject_id": str(SUBJECT),
                "evaluated_alternative": "Reubicar la estación de carga al costado norte",
                "rejection_reason": "La reubicación deja el pasillo sin salida de emergencia",
                "responsible_user_id": str(uuid.UUID(int=901)),
                "responsible_display_name": RESPONSIBLE,
                "compensatory_measure": "Vigilancia peatonal permanente durante la carga",
                "review_due_at": stamp(review_due),
                "signer": _signer(),
                "declared_at": stamp(LATE + timedelta(hours=2)),
            },
            timedelta(hours=2, minutes=3, seconds=1),
            True,
        ),
        (
            "unclosable_review",
            {
                "review_id": str(uuid.UUID(int=5006)),
                "declaration_id": str(uuid.UUID(int=5005)),
                "subject_id": str(SUBJECT),
                "outcome": "renewed",
                "review_note": "La alternativa sigue descartada por el trazado de la zona",
                "new_review_due_at": stamp(review_due + timedelta(days=60, minutes=9)),
                "reviewer": _signer(),
                "reviewed_at": stamp(LATE + timedelta(days=30)),
            },
            timedelta(days=30, minutes=19, seconds=3),
            True,
        ),
    ]
    return [
        TimelineSource(
            record_id=uuid.UUID(int=6000 + index),
            chain_sequence=80 + index,
            record_type=kind,
            received_at=LATE + offset,
            document=document,
            actor_kind="user" if licensed else None,
            actor_id=SIGNER if licensed else None,
            actor_role_in_use="coordinator_sst" if licensed else None,
        )
        for index, (kind, document, offset, licensed) in enumerate(steps)
    ]


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_details_and_actor_are_exactly_the_chosen_fields_of_each_kind() -> None:
    """Cada uno de los trece tipos, por igualdad: los campos elegidos y quién (o nadie)."""
    records = [*cycle(), *extras()]
    shown = compose(records).milestones
    coordinator = MilestoneActor(
        role_in_use="coordinator_sst", display_name="Coordinación SST sintética"
    )
    due = T0 + timedelta(days=2, hours=5)
    documents = {r.record_id: r.document for r in records}
    expected: list[tuple[MilestoneKind, MilestoneDetails, MilestoneActor | None]] = [
        (MilestoneKind.FINDING_RECEIVED, MilestoneDetails(), None),
        (
            MilestoneKind.CLASSIFICATION,
            MilestoneDetails(
                outcome="confirmed",
                reason_category="real_coexistence",
                reason_text="Coexistencia sintética observada en el pasillo de carga de la zona",
            ),
            coordinator,
        ),
        (
            MilestoneKind.ACTION_ASSIGNED,
            MilestoneDetails(
                action_type="engineering",
                description="Instalar un resguardo fijo en el acceso a la zona",
                expected_outcome="Sin paso peatonal por el frente de la prensa",
                assignee_display_name="Mantenimiento sintético",
                due_at=due,
            ),
            coordinator,
        ),
        (
            MilestoneKind.ACTION_EVIDENCE_ADDED,
            MilestoneDetails(
                description="Resguardo fijo instalado y anclado al piso en el acceso",
                performed_at=T0 + timedelta(days=1),
                performed_by_role="Técnico de mantenimiento",
                verification_note="Anclaje comprobado en la ronda",
            ),
            coordinator,
        ),
        (
            MilestoneKind.ACTION_EVIDENCE_ADDED,
            MilestoneDetails(
                description="Señalización del pasillo peatonal repintada en la zona",
                performed_at=T0 + timedelta(days=1, hours=2),
                performed_by_role="Técnico de mantenimiento",
            ),
            coordinator,
        ),
        (
            MilestoneKind.ACTION_OVERDUE,
            MilestoneDetails(due_at=due, days_overdue=1, escalation_level="coordinator"),
            None,
        ),
        (
            MilestoneKind.CLOSURE,
            MilestoneDetails(
                action_type="engineering",
                description="Resguardo verificado y anclado en el acceso de la zona",
                verification_note="Ronda de cierre sin hallazgos",
                professional_license="LIC-SINTETICA-01",
            ),
            coordinator,
        ),
        (
            MilestoneKind.ROOT_CAUSE_ANALYSIS,
            MilestoneDetails(
                root_cause_category="engineering",
                root_cause_statement="El trazado mezcla los flujos de carga y de paso",
                conclusion="Separar físicamente los flujos con un resguardo fijo",
            ),
            coordinator,
        ),
        (
            MilestoneKind.REOPENING,
            MilestoneDetails(
                from_state="closed",
                reason_text="El resguardo se retiró durante el mantenimiento mayor",
            ),
            coordinator,
        ),
        (
            MilestoneKind.ACTION_ASSIGNED,
            MilestoneDetails(
                action_type="process",
                description="Bloquear el retiro del resguardo en el mantenimiento",
                assignee_display_name="Mantenimiento sintético",
                due_at=_at(documents[shown[9].record_id]["due_at"]),
            ),
            coordinator,
        ),
        (MilestoneKind.OTHER, MilestoneDetails(), None),
        # Un tramo tardío: ni su índice ni su retraso (reconstruirían la hora exacta).
        (MilestoneKind.LATE_SEGMENT_ATTACHED, MilestoneDetails(), None),
        (
            MilestoneKind.REVIEW_RESOLUTION,
            MilestoneDetails(
                outcome="review_confirmed",
                reason_category="real_coexistence",
                reason_text="Coexistencia sintética confirmada al revisar la detección",
            ),
            coordinator,
        ),
        # Un adjunto no recuperable: sin su hora de detección.
        (MilestoneKind.CLOSURE_ATTACHMENT_UNRECOVERABLE, MilestoneDetails(), None),
        # La declaración: la medida y la revisión, firmada; nunca el nombre del responsable.
        (
            MilestoneKind.UNCLOSABLE_STATE_DECLARED,
            MilestoneDetails(
                compensatory_measure="Vigilancia peatonal permanente durante la carga",
                review_due_at=_at(documents[shown[14].record_id]["review_due_at"]),
            ),
            coordinator,
        ),
        (
            MilestoneKind.UNCLOSABLE_REVIEW,
            MilestoneDetails(
                outcome="renewed",
                review_note="La alternativa sigue descartada por el trazado de la zona",
                review_due_at=_at(documents[shown[15].record_id]["new_review_due_at"]),
            ),
            coordinator,
        ),
    ]
    assert [(m.kind, m.details, m.actor) for m in shown] == expected
    body = timeline_view(compose(records)).model_dump(mode="json")
    assert RESPONSIBLE not in set(_values(body))


def _values(value: Any) -> Iterator[Any]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _values(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _values(item)
    else:
        yield value


def test_a_superseded_action_without_closure_has_no_running_deadline() -> None:
    records = cycle()[:5]
    later = records[-1].received_at + timedelta(minutes=5)
    replacement = TimelineSource(
        record_id=uuid.UUID(int=4300),
        chain_sequence=records[-1].chain_sequence + 1,
        record_type="action_assigned",
        received_at=later,
        document={
            "action_id": str(NEW_ACTION),
            "subject_id": str(SUBJECT),
            "action_type": "engineering",
            "action_description": "Reasignar el resguardo a mantenimiento mayor",
            "assignee_display_name": "Mantenimiento sintético",
            "due_at": stamp(later + timedelta(days=5)),
            "supersedes_action_id": str(ACTION),
            "assigned_by": _user(),
            "assigned_at": stamp(later),
        },
    )
    first, second = [
        m.deadline
        for m in compose([*records, replacement]).milestones
        if m.kind is MilestoneKind.ACTION_ASSIGNED
    ]
    assert first is None  # sustituida sin cierre: no queda «en curso» para siempre
    assert second is not None and second.met is None
    # Si venció antes de sustituirse, sigue diciendo «fuera de plazo».
    overdue = cycle()[:6]
    shifted = replace(replacement, chain_sequence=overdue[-1].chain_sequence + 1)
    first_overdue = next(
        m.deadline
        for m in compose([*overdue, shifted]).milestones
        if m.kind is MilestoneKind.ACTION_ASSIGNED
    )
    assert first_overdue is not None and first_overdue.met is False


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _keys(item)


# --- el servicio: alcance, una sola lectura y lo que no se sabe -------------------------------


def _view(item: TimelineSource) -> LedgerRecordView:
    return LedgerRecordView(
        record_id=item.record_id,
        organization_id=ORG,
        plant_id=PLANT,
        chain_sequence=item.chain_sequence,
        record_type=item.record_type,
        schema_version=1,
        actor=RecordActor(
            kind="user",
            id=SIGNER,
            display_name_snapshot="Coordinación SST sintética",
            role_in_use="coordinator_sst",
            concession_id=None,
            unit="U-04",
        ),
        scope_plant_id=PLANT,
        scope_zone_id=ZONE,
        scope_node_id=None,
        correlation_id=uuid.UUID(int=77),
        received_at=item.received_at,
        occurred_at=None,
        source_key=None,
        content=json_bytes(item.document),
        content_hash="a" * 64,
        previous_hash="b" * 64,
        record_hash="c" * 64,
        evidences=(),
    )


def json_bytes(document: Mapping[str, Any]) -> bytes:
    import json

    return json.dumps(document).encode("utf-8")


class _Subjects:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self.row = row
        self.calls = 0

    async def visible_subject(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, Mapping[str, Any]]:
        self.calls += 1
        return context, self.row

    async def record_photos(
        self, context: ScopeContext, subject: Mapping[str, Any], documents: Any
    ) -> Mapping[Any, Any]:
        """Sin fotos (TASK-589): la ficha con fotos la prueban las de integración."""
        return {}


class _Reader:
    def __init__(self, history: SubjectHistory | None) -> None:
        self.history = history
        self.calls: list[tuple[SubjectRef, int, tuple[str, ...]]] = []

    async def subject_history(
        self,
        context: ScopeContext,
        subject: SubjectRef,
        *,
        limit: int,
        preceding_types: tuple[str, ...] = (),
    ) -> SubjectHistory | None:
        self.calls.append((subject, limit, preceding_types))
        return self.history


class _Timezones:
    async def plant_timezone(self, context: ScopeContext, plant_id: uuid.UUID) -> str | None:
        return "UTC"


def _service_row() -> dict[str, Any]:
    return {
        **subject_row(),
        "subject_id": str(SUBJECT),
        "anchor_record_id": str(uuid.UUID(int=3000)),
    }


def _run_service(history: SubjectHistory | None) -> tuple[FindingTimeline, _Reader]:
    import asyncio

    reader = _Reader(history)
    service = FindingTimelineService(
        subjects=_Subjects(_service_row()),  # type: ignore[arg-type]
        reader=reader,
        timezones=_Timezones(),
    )
    context = unit_context(ORG, ActorUnit.U04)
    result = asyncio.run(service.timeline(context, uuid.UUID(int=1000)))
    return result, reader


def test_the_service_reads_the_ledger_once_with_the_finding_as_resource() -> None:
    records = closed_cycle(close_offset=timedelta(days=1))[:2]
    history = SubjectHistory(
        records=tuple(_view(r) for r in records),
        truncated=False,
        preceding=(),
        preceding_complete=True,
    )
    timeline, reader = _run_service(history)
    ((subject, limit, preceding),) = reader.calls
    assert (subject.plant_id, subject.zone_id, subject.subject_id) == (PLANT, ZONE, SUBJECT)
    assert subject.anchor_record_id == uuid.UUID(int=3000)
    assert subject.resource == ResourceRef("finding", uuid.UUID(int=1000))
    assert (limit, preceding) == (MAX_TIMELINE_MILESTONES, (CONFIGURATION_RECORD_TYPE,))
    assert MAX_TIMELINE_MILESTONES == 500
    assert [m.record_id for m in timeline.milestones] == [r.record_id for r in records]
    (classification,) = [m for m in timeline.milestones if m.kind is MilestoneKind.CLASSIFICATION]
    assert classification.deadline is not None and classification.deadline.met is True


def test_the_service_never_invents_a_deadline_from_an_incomplete_configuration() -> None:
    records = closed_cycle(close_offset=timedelta(days=1))[:2]
    history = SubjectHistory(
        records=tuple(_view(r) for r in records),
        truncated=True,
        preceding=(),
        preceding_complete=False,
    )
    timeline, _ = _run_service(history)
    (classification,) = [m for m in timeline.milestones if m.kind is MilestoneKind.CLASSIFICATION]
    assert classification.deadline is None
    assert timeline.truncated is True


def test_a_subject_whose_anchor_the_ledger_does_not_show_is_not_found() -> None:
    with pytest.raises(ResourceNotFound):
        _run_service(None)


class _Untouchable:
    """Ni base ni auditoría: una entrada inválida se rechaza antes de tocarlas."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"no debía usarse: {name}")


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5])
def test_the_reader_rejects_a_limit_out_of_range_before_reading(limit: Any) -> None:
    import asyncio

    reader = LectorExpediente(database=_Untouchable(), audit=_Untouchable())  # type: ignore[arg-type]
    subject = SubjectRef(PLANT, ZONE, SUBJECT, uuid.UUID(int=3000), ResourceRef("finding", SUBJECT))
    with pytest.raises(LedgerQueryInvalid):
        asyncio.run(reader.subject_history(unit_context(ORG, ActorUnit.U04), subject, limit=limit))


def test_the_reader_rejects_a_malformed_subject_before_reading() -> None:
    import asyncio

    reader = LectorExpediente(database=_Untouchable(), audit=_Untouchable())  # type: ignore[arg-type]
    context = unit_context(ORG, ActorUnit.U04)
    good = SubjectRef(PLANT, ZONE, SUBJECT, uuid.UUID(int=3000), ResourceRef("finding", SUBJECT))
    for subject in ("no", replace(good, resource="finding"), replace(good, zone_id=str(ZONE))):
        with pytest.raises(LedgerQueryInvalid):
            asyncio.run(reader.subject_history(context, subject, limit=10))  # type: ignore[arg-type]
    for types in (("Mal",), ("classification",), ("loop_configuration_changed", "closure")):
        with pytest.raises(LedgerQueryInvalid):
            asyncio.run(reader.subject_history(context, good, limit=10, preceding_types=types))


def test_only_the_first_classification_has_a_deadline_and_it_drives_the_summary() -> None:
    late = T0 + timedelta(days=9)
    records = [
        *cycle()[:2],
        TimelineSource(
            record_id=uuid.UUID(int=4200),
            chain_sequence=99,
            record_type="classification",
            received_at=late,
            document=classification_document(late, 2),
        ),
    ]
    timeline = compose(records)
    first, again = [m for m in timeline.milestones if m.kind is MilestoneKind.CLASSIFICATION]
    assert first.deadline is not None and first.deadline.met is True
    assert again.deadline is None
    assert timeline.summary.classification_met is True


def test_a_snapshot_without_a_name_has_no_actor() -> None:
    records = cycle()
    classification = records[1]
    document = dict(classification.document)
    document["signer"] = {"user_id": str(SIGNER), "role_in_use": "coordinator_sst"}
    (shown,) = [
        m.actor
        for m in compose([records[0], replace(classification, document=document)]).milestones
        if m.kind is MilestoneKind.CLASSIFICATION
    ]
    assert shown is None


# --- fotos del hallazgo (TASK-589, A-128) --------------------------------------------------------


def _finding_photo(index: int, at: datetime) -> FindingPhoto:
    return FindingPhoto(
        photo_id=uuid.UUID(int=7000 + index),
        photo_index=index,
        photo_role="peak" if index == 0 else "burst",
        classified_as="finding",
        family="coexistence",
        standard_id=uuid.UUID(int=7),
        standard_version=1,
        max_confidence=0.9,
        started_at=at,
        ended_at=at + timedelta(seconds=2),
        received_at=at + timedelta(seconds=40),
        expires_at=None,
        camera_id=None,
        frame_at=at + timedelta(seconds=1),
        width=320,
        height=240,
        has_image=True,
        image_absent_reason=None,
    )


def test_the_reception_and_the_late_segment_carry_the_photos_of_their_segment() -> None:
    """``finding_received`` lleva las de su tramo y ``late_segment_attached`` las de la recepción
    que cita (``segment_record_id``); ningún otro hito lleva fotos (TASK-589)."""
    at = START + timedelta(hours=3, minutes=17, seconds=5)
    first_id, late_id = uuid.UUID(int=6001), uuid.UUID(int=6002)
    node_first, node_late = uuid.UUID(int=6101), uuid.UUID(int=6102)
    first = TimelineSource(
        record_id=first_id,
        chain_sequence=1,
        record_type="finding_received",
        received_at=at,
        document={**received_document(at), "finding_id": str(node_first)},
    )
    late_document = received_document(at + timedelta(minutes=2))
    late_document["episode"] = {**late_document["episode"], "segment_index": 1}
    late_reception = TimelineSource(
        record_id=late_id,
        chain_sequence=2,
        record_type="finding_received",
        received_at=at + timedelta(minutes=2),
        document={**late_document, "finding_id": str(node_late)},
    )
    attached = TimelineSource(
        record_id=uuid.UUID(int=6003),
        chain_sequence=3,
        record_type="late_segment_attached",
        received_at=at + timedelta(minutes=2, seconds=1),
        document={
            "segment_record_id": str(late_id),
            "episode_id": str(SUBJECT),
            "segment_index": 1,
        },
    )
    photos = {
        FindingPhotoKey(node_first, SUBJECT, 0): (_finding_photo(1, at), _finding_photo(0, at)),
        FindingPhotoKey(node_late, SUBJECT, 1): (_finding_photo(2, at),),
    }
    records = (first, late_reception, attached)
    assert photo_documents(records) == (
        first.document,
        late_reception.document,
        late_reception.document,
    )
    for single in (False, True):
        timeline = finding_timeline(
            TimelineInputs(
                subject=subject_row(single=single),
                records=records,
                truncated=False,
                configuration=(),
                timezone="UTC",
                photos=photos,
            )
        )
        indexes = [[p.photo_index for p in m.photos] for m in timeline.milestones]
        assert indexes == [[0, 1], [2], [2]]
        photo = timeline.milestones[0].photos[0]
        moments = (photo.frame_at, photo.started_at, photo.ended_at, photo.received_at)
        if single:
            assert all(_on_grid(m) for m in moments if m is not None)
        else:
            assert moments == (
                at + timedelta(seconds=1),
                at,
                at + timedelta(seconds=2),
                at + timedelta(seconds=40),
            )
        assert photo.episode_duration_ms == 2000
