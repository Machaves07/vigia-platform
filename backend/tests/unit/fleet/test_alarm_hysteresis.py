"""Histéresis, mudos y cargas de las alarmas de flota (TASK-225; NFR-GOB-45; BR-GOB-74, 81, 94).

Pruebas puras de ``fleet.domain.alarm_hysteresis``, ``fleet.domain.mute_detection`` y
``fleet.domain.fleet_alarm``, con los bordes de cada regla: dos evaluaciones consecutivas para
entrar y para salir; una oscilación de un solo ciclo no alarma; las clases sin histéresis deciden
en la primera; ``orphan_clips_growing`` entra con más de 50 al momento o con 24 h sostenidas y sale
tras 24 h; el mudo es estrictamente **más de** cinco intervalos y empieza en ``last_heartbeat_at``.
Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.fleet.domain.alarm_hysteresis import (
    CONFIRMED_KINDS,
    ORPHAN_SUSTAIN,
    REQUIRED_CONSECUTIVE,
    STATEFUL_KINDS,
    Evaluation,
    Transition,
    observe,
    required_evaluations,
    transition,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import (
    EVALUATED_KINDS,
    STATELESS_EVALUATED_KINDS,
    FleetAlarm,
    cleared_payload,
    is_retired,
    raised_payload,
)
from vigia_platform.fleet.domain.mute_detection import is_silent, mute_transition
from vigia_platform.fleet.events import FleetAlarmCleared, FleetAlarmRaised

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CYCLE = timedelta(seconds=60)
ORPHAN = FleetAlarmKind.ORPHAN_CLIPS_GROWING


def _run(
    kind: FleetAlarmKind, conditions: list[bool], *, immediate: bool = False
) -> list[Transition | None]:
    """Evalúa ``conditions`` una por ciclo y aplica cada transición a la alarma (abierta o no)."""
    evaluation: Evaluation | None = None
    open_alarm = False
    steps: list[Transition | None] = []
    for index, condition in enumerate(conditions):
        now = T0 + index * CYCLE
        evaluation = observe(evaluation, condition, now)
        step = transition(kind, evaluation, open_alarm=open_alarm, now=now, immediate=immediate)
        if step is Transition.RAISE:
            open_alarm = True
        elif step is Transition.CLEAR:
            open_alarm = False
        steps.append(step)
    return steps


# --- Clases ---------------------------------------------------------------------------------------


def test_the_kinds_partition_as_the_design_says() -> None:
    assert {
        FleetAlarmKind.QUEUE_OVER_THRESHOLD,
        FleetAlarmKind.CLOCK_DRIFT,
        FleetAlarmKind.CAMERA_BELOW_MIN_FPS,
    } == CONFIRMED_KINDS
    assert CONFIRMED_KINDS | {ORPHAN} == STATEFUL_KINDS
    assert EVALUATED_KINDS == STATEFUL_KINDS | STATELESS_EVALUATED_KINDS
    assert set(FleetAlarmKind) - EVALUATED_KINDS == {
        FleetAlarmKind.NODE_MUTE,
        FleetAlarmKind.CERTIFICATE_EXPIRING,
    }
    assert REQUIRED_CONSECUTIVE == 2
    for kind in FleetAlarmKind:
        assert required_evaluations(kind) == (2 if kind in CONFIRMED_KINDS else 1)


# --- Dos evaluaciones consecutivas ------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(CONFIRMED_KINDS))
def test_two_consecutive_evaluations_raise_and_two_clear(kind: FleetAlarmKind) -> None:
    assert _run(kind, [True]) == [None]
    assert _run(kind, [True, True]) == [None, Transition.RAISE]
    # Abierta: un solo ciclo sin la condición no la baja; dos, sí.
    assert _run(kind, [True, True, False]) == [None, Transition.RAISE, None]
    assert _run(kind, [True, True, False, False]) == [
        None,
        Transition.RAISE,
        None,
        Transition.CLEAR,
    ]
    # Una condición sostenida días no produce un segundo raise.
    assert _run(kind, [True] * 1_440).count(Transition.RAISE) == 1


@pytest.mark.parametrize("kind", sorted(CONFIRMED_KINDS))
def test_a_one_cycle_oscillation_never_raises_nor_clears(kind: FleetAlarmKind) -> None:
    assert set(_run(kind, [True, False] * 50)) == {None}
    assert set(_run(kind, [False, True] * 50)) == {None}
    # Con la alarma abierta, la oscilación tampoco la baja.
    steps = _run(kind, [True, True] + [False, True] * 50)
    assert steps.count(Transition.RAISE) == 1 and Transition.CLEAR not in steps


@given(
    kind=st.sampled_from(sorted(FleetAlarmKind)),
    conditions=st.lists(st.booleans(), min_size=1, max_size=60),
)
def test_raises_and_clears_alternate(kind: FleetAlarmKind, conditions: list[bool]) -> None:
    """BR-GOB-81: entre un raise y su clear nunca hay otro raise (y nunca un clear sin raise)."""
    taken = [step for step in _run(kind, conditions) if step is not None]
    assert taken == [Transition.RAISE, Transition.CLEAR] * (len(taken) // 2) + (
        [Transition.RAISE] if len(taken) % 2 else []
    )


@given(conditions=st.lists(st.booleans(), min_size=1, max_size=60))
def test_a_confirmed_raise_follows_two_true_evaluations(conditions: list[bool]) -> None:
    steps = _run(FleetAlarmKind.CLOCK_DRIFT, conditions)
    for index, step in enumerate(steps):
        if step is Transition.RAISE:
            assert index >= 1 and conditions[index] and conditions[index - 1]
        if step is Transition.CLEAR:
            assert index >= 1 and not conditions[index] and not conditions[index - 1]


@pytest.mark.parametrize("kind", sorted(STATELESS_EVALUATED_KINDS | {FleetAlarmKind.NODE_MUTE}))
def test_kinds_without_hysteresis_decide_on_the_first_evaluation(kind: FleetAlarmKind) -> None:
    assert _run(kind, [True]) == [Transition.RAISE]
    assert _run(kind, [True, False]) == [Transition.RAISE, Transition.CLEAR]
    assert _run(kind, [False]) == [None]


def test_observe_counts_the_streak_with_a_cap_and_keeps_its_start() -> None:
    first = observe(None, True, T0)
    assert first == Evaluation(True, 1, T0)
    second = observe(first, True, T0 + CYCLE)
    assert second == Evaluation(True, 2, T0)
    assert observe(second, True, T0 + 2 * CYCLE) == Evaluation(True, 2, T0)
    assert observe(second, False, T0 + 3 * CYCLE) == Evaluation(False, 1, T0 + 3 * CYCLE)


def test_evaluation_bounds() -> None:
    with pytest.raises(ValueError, match="consecutive"):
        Evaluation(True, 0, T0)
    with pytest.raises(ValueError, match="consecutive"):
        Evaluation(True, 3, T0)
    with pytest.raises(TypeError):
        Evaluation(1, 1, T0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="zona"):
        Evaluation(True, 1, datetime(2026, 10, 5))  # noqa: DTZ001 - sin zona a propósito
    with pytest.raises(TypeError):
        observe(None, 1, T0)  # type: ignore[arg-type]


# --- orphan_clips_growing ----------------------------------------------------------------------


def test_orphans_above_fifty_raise_at_once() -> None:
    assert _run(ORPHAN, [True], immediate=True) == [Transition.RAISE]


def test_the_five_percent_must_hold_24_hours_to_raise_and_to_clear() -> None:
    raised = observe(None, True, T0)
    assert transition(ORPHAN, raised, open_alarm=False, now=T0) is None
    just_before = T0 + ORPHAN_SUSTAIN - timedelta(milliseconds=1)
    assert transition(ORPHAN, raised, open_alarm=False, now=just_before) is None
    at = T0 + ORPHAN_SUSTAIN
    assert transition(ORPHAN, raised, open_alarm=False, now=at) is Transition.RAISE
    below = observe(raised, False, at + CYCLE)
    assert transition(ORPHAN, below, open_alarm=True, now=at + CYCLE) is None
    assert (
        transition(ORPHAN, below, open_alarm=True, now=at + CYCLE + ORPHAN_SUSTAIN - CYCLE / 60)
        is None
    )
    assert (
        transition(ORPHAN, below, open_alarm=True, now=at + CYCLE + ORPHAN_SUSTAIN)
        is Transition.CLEAR
    )
    # Bajar del umbral un ciclo reinicia la racha: hacen falta otras 24 h.
    again = observe(below, True, at + 2 * CYCLE)
    assert again.since == at + 2 * CYCLE


def test_immediate_does_not_clear_early() -> None:
    below = observe(None, False, T0)
    assert transition(ORPHAN, below, open_alarm=True, now=T0, immediate=True) is None


# --- Mudos -------------------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [15, 60, 600])
def test_mute_is_strictly_more_than_five_intervals(interval: int) -> None:
    last = T0
    threshold = timedelta(seconds=5 * interval)
    assert not is_silent(last, last + threshold, interval)
    assert is_silent(last, last + threshold + timedelta(milliseconds=1), interval)
    assert not is_silent(None, last + 10 * threshold, interval)


@pytest.mark.parametrize("interval", [14, 601, 0, -60])
def test_an_interval_out_of_range_is_rejected(interval: int) -> None:
    with pytest.raises(ValueError, match="15 a 600"):
        is_silent(T0, T0 + timedelta(days=1), interval)


def test_the_mute_interval_starts_at_the_last_heartbeat() -> None:
    node = uuid.uuid4()
    last = datetime(2026, 10, 5, 11, 58, 59, 123_000, tzinfo=UTC)
    assert mute_transition(node, last) == {
        "node_id": str(node),
        "state": "mute",
        "since": "2026-10-05T11:58:59.123Z",
        "last_heartbeat_at": "2026-10-05T11:58:59.123Z",
    }


# --- FleetAlarm y cargas --------------------------------------------------------------------------


def _alarm(**changes: object) -> FleetAlarm:
    fields: dict[str, object] = {
        "alarm_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "plant_id": uuid.uuid4(),
        "alarm_kind": FleetAlarmKind.CLOCK_DRIFT,
        "node_id": uuid.uuid4(),
        "zone_id": None,
        "raised_at": T0,
        "cleared_at": None,
        "raised_event_id": uuid.uuid4(),
        "cleared_event_id": None,
    }
    fields.update(changes)
    return FleetAlarm(**fields)  # type: ignore[arg-type]


def test_fleet_alarm_closing_rules() -> None:
    assert _alarm().open
    with pytest.raises(ValueError, match="juntos"):
        _alarm(cleared_at=T0)
    with pytest.raises(ValueError, match="antes"):
        _alarm(cleared_at=T0 - CYCLE, cleared_event_id=uuid.uuid4())
    with pytest.raises(ValueError, match="zona"):
        _alarm(zone_id=uuid.uuid4())
    zoned = _alarm(alarm_kind=FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE, zone_id=uuid.uuid4())
    assert zoned.zone_id is not None


def test_the_payloads_are_identifiers_kinds_and_stamps_and_validate_strictly() -> None:
    alarm = _alarm(alarm_kind=FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE, zone_id=uuid.uuid4())
    raised = raised_payload(
        alarm_id=alarm.alarm_id,
        kind=alarm.alarm_kind,
        node_id=alarm.node_id,
        zone_id=alarm.zone_id,
        since=T0 - CYCLE,
    )
    assert set(raised) == {"alarm_id", "alarm_kind", "node_id", "zone_id", "since"}
    FleetAlarmRaised.model_validate_json(json.dumps(raised), strict=True)
    cleared = cleared_payload(alarm, since=T0 + CYCLE, cleared_at=T0 + 2 * CYCLE)
    assert set(cleared) == {"alarm_id", "alarm_kind", "node_id", "zone_id", "since", "cleared_at"}
    FleetAlarmCleared.model_validate_json(json.dumps(cleared), strict=True)
    assert cleared["cleared_at"] == "2026-10-05T12:02:00.000Z"
    plain = raised_payload(
        alarm_id=uuid.uuid4(),
        kind=FleetAlarmKind.NODE_MUTE,
        node_id=uuid.uuid4(),
        zone_id=None,
        since=T0,
    )
    assert "zone_id" not in plain
    FleetAlarmRaised.model_validate_json(json.dumps(plain), strict=True)


def test_a_retired_node_is_revoked_or_decommissioned() -> None:
    assert is_retired("revoked", revoked_at=None, decommissioned_at=None)
    assert is_retired("enrolled", revoked_at=T0, decommissioned_at=None)
    assert is_retired("enrolled", revoked_at=None, decommissioned_at=T0)
    assert not is_retired("enrolled", revoked_at=None, decommissioned_at=None)
    assert not is_retired("re_enrollment_pending", revoked_at=None, decommissioned_at=None)
