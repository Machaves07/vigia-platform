"""Línea de tiempo de cobertura en dos capas (TASK-120; LC-NUC-17; BR-NUC-69 a 74).

Propiedades sobre la función pura ``ledger.domain.coverage`` con el generador ``coverage_inputs``:

- **PR-NUC-25**: ``compose_timeline`` es una partición exacta de ``[a, b)``: ordenada, contigua,
  sin solapes ni tramos vacíos, maximal (dos tramos seguidos nunca tienen el mismo compuesto) y
  sin ningún estado fuera de ``coverage_state``.
- **PR-NUC-26**: ``state_at(t)`` coincide con el tramo que contiene ``t`` en todos los bordes de
  la línea y en instantes generados; y los dos coinciden con el oráculo literal del §7.
- **PR-NUC-27**: ningún instante es ``observable`` sin nodo asignado, comunicación ``reachable``,
  modo ``productive`` y estado ``observable`` del nodo; añadir una declaración ``mute`` o un modo
  no productivo nunca aumenta ``observable_ms`` (ni vuelve observable ningún instante).
- **PR-NUC-28**: permutar el orden de llegada de las entradas da la misma composición.
- **PR-NUC-29**: ``summary`` suma exactamente ``b - a`` y cuadra con los tramos.
- **PR-NUC-51**: concatenar ``[a, c)`` y ``[c, b)`` fusionando tramos adyacentes iguales da
  ``[a, b)``; un periodo mayor que 31 días responde ``period_too_long``.

Además, ejemplos de cada borde de BR-NUC-69 a 74 y de las entradas inválidas.
Solo datos generados.
"""

from __future__ import annotations

import itertools
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.coverage_strategies import (
    CAMERA_A,
    NODES,
    ZONE,
    Scenario,
    at,
    coverage_inputs,
    ms_of,
    oracle,
    with_inputs,
)
from vigia_platform.ledger.domain.coverage import (
    LABELS_ES,
    MAX_PERIOD,
    SUMMARY_LABELS_ES,
    AssignmentInput,
    ClockBasis,
    CommunicationInput,
    CommunicationState,
    Composition,
    CoverageInputInvalid,
    CoverageInputs,
    CoverageInterval,
    CoverageLayer,
    CoveragePeriod,
    CoverageState,
    EventPhase,
    GateInput,
    ObservabilityInput,
    PeriodTooLong,
    PlatformCause,
    Subject,
    ZoneMode,
    check_period,
    compose_timeline,
    state_at,
)

MS = timedelta(milliseconds=1)


def key(interval: CoverageInterval) -> tuple[Any, ...]:
    return (interval.state, interval.causes, interval.layer)


def containing(composition: Composition, t: datetime) -> CoverageInterval:
    matches = [i for i in composition.intervals if i.starts_at <= t < i.ends_at]
    assert len(matches) == 1, (t, matches)
    return matches[0]


def merge(intervals: list[CoverageInterval]) -> list[CoverageInterval]:
    merged: list[CoverageInterval] = []
    for interval in intervals:
        if merged and merged[-1].ends_at == interval.starts_at and key(merged[-1]) == key(interval):
            previous = merged[-1]
            merged[-1] = CoverageInterval(
                starts_at=previous.starts_at,
                ends_at=interval.ends_at,
                layer=previous.layer,
                state=previous.state,
                causes=previous.causes,
                subject=None,
                source_record_ids=tuple(
                    sorted({*previous.source_record_ids, *interval.source_record_ids})
                ),
                clock_basis=previous.clock_basis,
            )
        else:
            merged.append(interval)
    return merged


# --- PR-NUC-25: partición exacta -----------------------------------------------------------------


@given(scenario=coverage_inputs())
def test_pr_nuc_25_timeline_is_an_exact_partition(scenario: Scenario) -> None:
    composition = compose_timeline(scenario.inputs, scenario.period)
    intervals = composition.intervals
    assert intervals, "un periodo no vacío siempre tiene tramos"
    assert intervals[0].starts_at == scenario.period.start
    assert intervals[-1].ends_at == scenario.period.end
    for interval in intervals:
        assert interval.starts_at < interval.ends_at
        assert interval.state in set(CoverageState)
        assert interval.subject is None
        if interval.layer is CoverageLayer.PLATFORM_COMMUNICATION:
            assert interval.state is CoverageState.NOT_OBSERVABLE
            assert interval.clock_basis is ClockBasis.PLATFORM
            assert interval.causes in {(cause.value,) for cause in PlatformCause}
        else:
            assert interval.clock_basis is ClockBasis.NODE
        assert list(interval.source_record_ids) == sorted(set(interval.source_record_ids))
    for previous, following in itertools.pairwise(intervals):
        assert previous.ends_at == following.starts_at, "contigua y sin solapes"
        assert key(previous) != key(following), "tramos maximales"
    assert composition.period == scenario.period
    # La capa del nodo sin componer queda recortada al periodo.
    for interval in composition.node_intervals:
        assert scenario.period.start <= interval.starts_at < interval.ends_at
        assert interval.ends_at <= scenario.period.end
        assert interval.layer is CoverageLayer.NODE_REPORT
        assert interval.clock_basis is ClockBasis.NODE
        assert interval.subject is not None


# --- PR-NUC-26: estado_en coincide con la línea (y con el oráculo) -------------------------------


def probe_instants(scenario: Scenario, composition: Composition, extra: list[int]) -> set[int]:
    """Cada borde de tramo, el milisegundo anterior al fin y los instantes generados."""
    instants = {scenario.a, scenario.b - 1}
    for interval in composition.intervals:
        instants.add(ms_of(interval.starts_at))
        instants.add(ms_of(interval.ends_at) - 1)
    instants.update(scenario.a + offset % (scenario.b - scenario.a) for offset in extra)
    return instants


@given(scenario=coverage_inputs(), extra=st.lists(st.integers(min_value=0), max_size=10))
def test_pr_nuc_26_state_at_matches_the_interval_containing_t(
    scenario: Scenario, extra: list[int]
) -> None:
    composition = compose_timeline(scenario.inputs, scenario.period)
    for t in probe_instants(scenario, composition, extra):
        status = state_at(scenario.inputs, at(t))
        interval = containing(composition, at(t))
        assert status.instant == at(t)
        assert (status.state, status.causes, status.layer, status.clock_basis) == (
            interval.state,
            interval.causes,
            interval.layer,
            interval.clock_basis,
        )
        assert set(status.source_record_ids) <= set(interval.source_record_ids)
        reading = oracle(scenario.inputs, t)
        assert (status.state.value, status.causes) == (reading.state, reading.causes)


def test_state_at_truncates_the_instant_to_the_millisecond() -> None:
    inputs = productive_zone(opened(0, CoverageState.DEGRADED, ("focus",)))
    status = state_at(inputs, at(5) + timedelta(microseconds=999))
    assert status.instant == at(5)
    assert status.state is CoverageState.DEGRADED


# --- PR-NUC-27: observable solo con las tres condiciones; monotonía ------------------------------


@given(scenario=coverage_inputs(), extra=st.lists(st.integers(min_value=0), max_size=10))
def test_pr_nuc_27_observable_requires_reachable_productive_and_node_observable(
    scenario: Scenario, extra: list[int]
) -> None:
    composition = compose_timeline(scenario.inputs, scenario.period)
    for t in probe_instants(scenario, composition, extra):
        if containing(composition, at(t)).state is not CoverageState.OBSERVABLE:
            continue
        reading = oracle(scenario.inputs, t)
        assert reading.node_assigned
        assert reading.communication == "reachable"
        assert reading.mode == "productive"
        assert reading.node_state == "observable"


def observable_instants(composition: Composition) -> set[tuple[datetime, datetime]]:
    return {
        (i.starts_at, i.ends_at)
        for i in composition.intervals
        if i.state is CoverageState.OBSERVABLE
    }


@given(scenario=coverage_inputs(), data=st.data())
def test_pr_nuc_27_a_mute_or_a_non_productive_mode_never_adds_observable_time(
    scenario: Scenario, data: st.DataObject
) -> None:
    before = compose_timeline(scenario.inputs, scenario.period)
    offset = st.integers(min_value=-20, max_value=140).map(lambda k: scenario.a + k * scenario.unit)
    if data.draw(st.booleans(), label="mute"):
        since = data.draw(offset, label="since")
        heartbeat = data.draw(st.one_of(st.none(), offset), label="last_heartbeat_at")
        inserted = CommunicationInput(
            uuid.uuid4(),
            data.draw(st.sampled_from(NODES), label="node"),
            CommunicationState.MUTE,
            at(since),
            None if heartbeat is None else at(heartbeat),
        )
        after_inputs = with_inputs(
            scenario, communication=(*scenario.inputs.communication, inserted)
        )
    else:
        inserted_gate = GateInput(
            uuid.uuid4(),
            at(data.draw(offset, label="at")),
            data.draw(st.sampled_from([ZoneMode.NO_CAPTURE, ZoneMode.COMMISSIONING]), label="mode"),
        )
        after_inputs = with_inputs(scenario, gates=(*scenario.inputs.gates, inserted_gate))
    after = compose_timeline(after_inputs.inputs, scenario.period)
    assert after.summary.observable_ms <= before.summary.observable_ms
    # Punto a punto: todo tramo de antes que se solapa con uno observable de después ya era
    # observable, así que ningún instante pasa a observable.
    for start, end in observable_instants(after):
        for interval in before.intervals:
            if interval.starts_at < end and start < interval.ends_at:
                assert interval.state is CoverageState.OBSERVABLE


# --- PR-NUC-28: el orden de llegada no importa ---------------------------------------------------


@given(scenario=coverage_inputs(), seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_pr_nuc_28_permuting_arrival_order_gives_the_same_timeline(
    scenario: Scenario, seed: int
) -> None:
    shuffler = random.Random(seed)  # noqa: S311 - permutación de prueba, no criptografía

    def shuffled(values: tuple[Any, ...]) -> tuple[Any, ...]:
        items = list(values)
        shuffler.shuffle(items)
        return tuple(items)

    inputs = scenario.inputs
    permuted = CoverageInputs(
        observability=shuffled(inputs.observability),
        communication=shuffled(inputs.communication),
        gates=shuffled(inputs.gates),
        assignments=shuffled(inputs.assignments),
        first_report_at=inputs.first_report_at,
    )
    assert compose_timeline(permuted, scenario.period) == compose_timeline(inputs, scenario.period)
    t = scenario.period.start + (seed % (scenario.b - scenario.a)) * MS
    assert state_at(permuted, t) == state_at(inputs, t)


# --- PR-NUC-29: el resumen suma b - a ------------------------------------------------------------


@given(scenario=coverage_inputs())
def test_pr_nuc_29_summary_adds_up_to_the_period(scenario: Scenario) -> None:
    composition = compose_timeline(scenario.inputs, scenario.period)
    summary = composition.summary
    assert summary.total_ms == scenario.b - scenario.a
    expected = dict.fromkeys(SUMMARY_LABELS_ES, 0)
    for interval in composition.intervals:
        if interval.layer is CoverageLayer.PLATFORM_COMMUNICATION:
            expected[f"{interval.causes[0]}_ms"] += interval.duration_ms
        else:
            expected[f"{interval.state.value}_ms"] += interval.duration_ms
    assert {name: getattr(summary, name) for name in SUMMARY_LABELS_ES} == expected
    assert all(value >= 0 for value in expected.values())


# --- PR-NUC-51: composición por tramos y tope de 31 días -----------------------------------------


@given(scenario=coverage_inputs(), cut=st.integers(min_value=1))
def test_pr_nuc_51_concatenating_two_halves_gives_the_whole(scenario: Scenario, cut: int) -> None:
    if scenario.b - scenario.a < 2:
        return
    c = scenario.a + 1 + cut % (scenario.b - scenario.a - 1)
    whole = compose_timeline(scenario.inputs, scenario.period)
    left = compose_timeline(scenario.inputs, CoveragePeriod(scenario.period.start, at(c)))
    right = compose_timeline(scenario.inputs, CoveragePeriod(at(c), scenario.period.end))
    assert merge([*left.intervals, *right.intervals]) == list(whole.intervals)
    assert left.summary.total_ms + right.summary.total_ms == whole.summary.total_ms


@given(
    start=st.integers(min_value=0, max_value=10**9),
    extra=st.integers(min_value=1, max_value=10**9),
    scenario=coverage_inputs(max_events=4),
)
def test_pr_nuc_51_a_period_longer_than_31_days_is_period_too_long(
    start: int, extra: int, scenario: Scenario
) -> None:
    begin = at(start)
    with pytest.raises(PeriodTooLong) as raised:
        compose_timeline(scenario.inputs, CoveragePeriod(begin, begin + MAX_PERIOD + extra * MS))
    assert raised.value.code == "period_too_long"
    exact = compose_timeline(scenario.inputs, CoveragePeriod(begin, begin + MAX_PERIOD))
    assert exact.summary.total_ms == MAX_PERIOD // MS


def test_a_32_day_period_is_period_too_long_and_31_days_is_accepted() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(PeriodTooLong):
        check_period(CoveragePeriod(start, start + timedelta(days=32)))
    with pytest.raises(PeriodTooLong):
        check_period(CoveragePeriod(start, start + timedelta(days=31, milliseconds=1)))
    a, b = check_period(CoveragePeriod(start, start + timedelta(days=31)))
    assert b - a == 31 * 24 * 3600 * 1000
    a, b = check_period(CoveragePeriod(start, start + MS))
    assert b - a == 1


# --- Ejemplos de las reglas (BR-NUC-69 a 74) -----------------------------------------------------

NODE = NODES[0]


def opened(
    start: int,
    state: CoverageState,
    causes: tuple[str, ...] = (),
    *,
    subject: Subject = ZONE,
    event_id: uuid.UUID | None = None,
) -> ObservabilityInput:
    return ObservabilityInput(
        uuid.uuid4(),
        event_id or uuid.uuid4(),
        subject,
        EventPhase.OPENED,
        state,
        causes,
        at(start),
    )


def closed(
    start: int,
    end: int,
    opener: uuid.UUID,
    state: CoverageState = CoverageState.OBSERVABLE,
    causes: tuple[str, ...] = (),
    *,
    subject: Subject = ZONE,
) -> ObservabilityInput:
    return ObservabilityInput(
        uuid.uuid4(),
        uuid.uuid4(),
        subject,
        EventPhase.CLOSED,
        state,
        causes,
        at(start),
        ended_at=at(end),
        opened_event_id=opener,
    )


def productive_zone(*events: ObservabilityInput, **overrides: Any) -> CoverageInputs:
    """Nodo asignado desde siempre, alcanzable y zona productiva desde ``t = 0``."""
    base: dict[str, Any] = {
        "observability": events,
        "communication": (
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(0)),
        ),
        "gates": (GateInput(uuid.uuid4(), at(0), ZoneMode.PRODUCTIVE),),
        "assignments": (AssignmentInput(uuid.uuid4(), NODE, at(0)),),
    }
    base.update(overrides)
    return CoverageInputs(**base)


def timeline(inputs: CoverageInputs, a: int, b: int) -> list[tuple[int, int, str, tuple[str, ...]]]:
    composition = compose_timeline(inputs, CoveragePeriod(at(a), at(b)))
    return [
        (ms_of(i.starts_at), ms_of(i.ends_at), i.state.value, i.causes)
        for i in composition.intervals
    ]


def test_mute_starts_at_the_last_heartbeat_not_at_the_declaration() -> None:
    inputs = productive_zone(
        opened(0, CoverageState.OBSERVABLE),
        communication=(
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(0)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, at(500), at(200)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(800)),
        ),
    )
    assert timeline(inputs, 0, 1000) == [
        (0, 200, "observable", ()),
        (200, 800, "not_observable", ("no_communication",)),
        (800, 1000, "observable", ()),
    ]
    # Una declaración reachable más antigua no deshace un mute declarado después.
    later = productive_zone(
        opened(0, CoverageState.OBSERVABLE),
        communication=(
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, at(500), at(100)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(300)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(0)),
        ),
    )
    assert timeline(later, 0, 1000) == [
        (0, 100, "observable", ()),
        (100, 1000, "not_observable", ("no_communication",)),
    ]


def test_node_intervals_inside_a_silence_are_kept_but_do_not_change_the_composite() -> None:
    inner = opened(300, CoverageState.DEGRADED, ("obstruction",))
    inputs = productive_zone(
        opened(0, CoverageState.OBSERVABLE),
        inner,
        closed(300, 400, inner.event_id),
        communication=(
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(0)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, at(600), at(200)),
        ),
    )
    composition = compose_timeline(inputs, CoveragePeriod(at(0), at(1000)))
    assert timeline(inputs, 0, 1000) == [
        (0, 200, "observable", ()),
        (200, 1000, "not_observable", ("no_communication",)),
    ]
    reported = [
        (ms_of(i.starts_at), ms_of(i.ends_at), i.state.value) for i in composition.node_intervals
    ]
    assert reported == [(0, 300, "observable"), (300, 400, "degraded")]


def test_priority_no_node_then_inactive_zone_then_silence_then_node() -> None:
    event = opened(0, CoverageState.DEGRADED, ("focus",))
    inputs = CoverageInputs(
        observability=(event,),
        assignments=(AssignmentInput(uuid.uuid4(), NODE, at(100), at(900)),),
        communication=(
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.UNKNOWN, at(0)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.REACHABLE, at(150)),
            CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, at(700), at(600)),
        ),
        gates=(
            GateInput(uuid.uuid4(), at(300), ZoneMode.COMMISSIONING),
            GateInput(uuid.uuid4(), at(400), ZoneMode.PRODUCTIVE),
        ),
    )
    assert timeline(inputs, 0, 1000) == [
        (0, 150, "not_observable", ("never_reported",)),  # sin nodo y después unknown
        (150, 400, "not_observable", ("zone_not_active",)),  # sin compuerta, después comisionado
        (400, 600, "degraded", ("focus",)),
        (600, 900, "not_observable", ("no_communication",)),
        (900, 1000, "not_observable", ("never_reported",)),  # desasignado
    ]
    # La zona inactiva manda sobre el silencio.
    silent_and_inactive = CoverageInputs(
        observability=(event,),
        assignments=(AssignmentInput(uuid.uuid4(), NODE, at(0)),),
        communication=(CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, at(0)),),
    )
    assert timeline(silent_and_inactive, 0, 10) == [(0, 10, "not_observable", ("zone_not_active",))]


def test_open_without_close_lasts_until_the_next_event_of_the_subject_or_b() -> None:
    first = opened(100, CoverageState.DEGRADED, ("focus",))
    second = opened(400, CoverageState.NOT_OBSERVABLE, ("obstruction",))
    camera = opened(200, CoverageState.NOT_OBSERVABLE, ("focus",), subject=CAMERA_A)
    inputs = productive_zone(first, second, camera)
    assert timeline(inputs, 0, 1000) == [
        (0, 100, "not_observable", ("never_reported",)),
        (100, 400, "degraded", ("focus",)),
        (400, 1000, "not_observable", ("obstruction",)),
    ]


def test_a_camera_event_counts_as_a_report_but_never_changes_the_zone_state() -> None:
    camera = opened(200, CoverageState.NOT_OBSERVABLE, ("focus",), subject=CAMERA_A)
    assert timeline(productive_zone(camera), 0, 1000) == [
        (0, 200, "not_observable", ("never_reported",)),
        (200, 1000, "observable", ()),
    ]
    # Un first_report_at anterior (eventos fuera de la carga) también cuenta.
    early = productive_zone(camera, first_report_at=at(50))
    assert timeline(early, 0, 1000)[0] == (0, 50, "not_observable", ("never_reported",))


def test_orphan_close_is_an_interval_with_its_own_state_or_not_observable_if_it_restores() -> None:
    lost = uuid.uuid4()
    restoring = closed(100, 300, lost)
    inputs = productive_zone(opened(0, CoverageState.OBSERVABLE), restoring)
    assert timeline(inputs, 0, 500) == [
        (0, 100, "observable", ()),
        (100, 300, "not_observable", ()),
        (300, 500, "observable", ()),
    ]
    restart = closed(
        100, 300, lost, CoverageState.NOT_OBSERVABLE, ("camera_heartbeat_lost", "node_restart")
    )
    inputs = productive_zone(opened(0, CoverageState.OBSERVABLE), restart)
    assert timeline(inputs, 0, 500)[1] == (
        100,
        300,
        "not_observable",
        ("camera_heartbeat_lost", "node_restart"),
    )


def test_pair_closes_at_its_end_and_the_earliest_close_wins() -> None:
    event = opened(100, CoverageState.DEGRADED, ("backlight",))
    late = closed(100, 700, event.event_id)
    early = closed(100, 300, event.event_id)
    inputs = productive_zone(opened(0, CoverageState.OBSERVABLE), event, late, early)
    assert timeline(inputs, 0, 1000) == [
        (0, 100, "observable", ()),
        (100, 300, "degraded", ("backlight",)),
        (300, 1000, "observable", ()),
    ]


def test_same_millisecond_starts_break_ties_by_event_id() -> None:
    low = opened(100, CoverageState.DEGRADED, ("focus",), event_id=uuid.UUID(int=1))
    high = opened(100, CoverageState.NOT_OBSERVABLE, ("obstruction",), event_id=uuid.UUID(int=2))
    for order in ((low, high), (high, low)):
        assert timeline(productive_zone(*order), 100, 200) == [
            (100, 200, "not_observable", ("obstruction",))
        ]


def test_node_events_keep_the_node_clock_and_its_declared_offset() -> None:
    event = ObservabilityInput(
        uuid.uuid4(),
        uuid.uuid4(),
        ZONE,
        EventPhase.OPENED,
        CoverageState.DEGRADED,
        ("focus",),
        at(0),
        clock_offset_ms=-12,
    )
    composition = compose_timeline(productive_zone(event), CoveragePeriod(at(0), at(10)))
    (interval,) = composition.intervals
    assert interval.clock_basis is ClockBasis.NODE
    assert interval.source_record_ids == (event.record_id,)
    (node,) = composition.node_intervals
    assert (node.clock_offset_ms, node.subject) == (-12, ZONE)


def test_timestamps_below_the_millisecond_are_truncated() -> None:
    inputs = productive_zone(
        opened(0, CoverageState.OBSERVABLE),
        gates=(GateInput(uuid.uuid4(), at(0) + timedelta(microseconds=1999), ZoneMode.PRODUCTIVE),),
    )
    assert timeline(inputs, 0, 5) == [
        (0, 1, "not_observable", ("zone_not_active",)),
        (1, 5, "observable", ()),
    ]


# --- Etiquetas (BR-NUC-72) ----------------------------------------------------------------------

_FORBIDDEN = ("no ocurri", "despejad", "segur", "intenci", "sabotaje", "manipul", "a propósito")


def test_labels_say_not_observed_never_did_not_happen_or_clear() -> None:
    for label in (*LABELS_ES.values(), *SUMMARY_LABELS_ES.values()):
        lowered = label.lower()
        assert not any(word in lowered for word in _FORBIDDEN), label
    for cause in PlatformCause:
        assert "no se observó" in LABELS_ES[cause]
    for name in (
        "not_observable_ms",
        "no_communication_ms",
        "never_reported_ms",
        "zone_not_active_ms",
    ):
        assert "no se observó" in SUMMARY_LABELS_ES[name]
    assert set(SUMMARY_LABELS_ES) == {
        "observable_ms",
        "degraded_ms",
        "not_observable_ms",
        "no_communication_ms",
        "never_reported_ms",
        "zone_not_active_ms",
    }
    assert {state.value for state in CoverageState} == {"observable", "degraded", "not_observable"}


# --- Entradas inválidas ---------------------------------------------------------------------------

_NAIVE = datetime(2026, 9, 1)  # noqa: DTZ001 - entrada hostil sin zona a propósito
_START = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "period",
    [
        CoveragePeriod(_START, _START),
        CoveragePeriod(_START + MS, _START),
        CoveragePeriod(_NAIVE, _START),
        CoveragePeriod(_START, _START + timedelta(microseconds=1500)),
        CoveragePeriod(_START + timedelta(microseconds=1), _START + MS),
        CoveragePeriod("2026-09-01T00:00:00.000Z", _START),  # type: ignore[arg-type]
        (_START, _START + MS),
        None,
    ],
)
def test_invalid_periods_are_rejected(period: Any) -> None:
    with pytest.raises(CoverageInputInvalid) as raised:
        compose_timeline(CoverageInputs(), period)
    assert raised.value.code == "query_invalid"


def _event(**changes: Any) -> ObservabilityInput:
    base: dict[str, Any] = {
        "record_id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "subject": ZONE,
        "phase": EventPhase.OPENED,
        "state": CoverageState.DEGRADED,
        "causes": ("focus",),
        "started_at": _START,
    }
    base.update(changes)
    return ObservabilityInput(**base)


@pytest.mark.parametrize(
    "inputs",
    [
        CoverageInputs(observability=(_event(started_at=_NAIVE),)),
        CoverageInputs(observability=(_event(state="clear"),)),
        CoverageInputs(observability=(_event(state="safe"),)),
        CoverageInputs(observability=(_event(causes=("sabotage",)),)),
        CoverageInputs(observability=(_event(causes=("focus", "focus")),)),
        CoverageInputs(observability=(_event(causes=()),)),
        CoverageInputs(observability=(_event(causes=["focus"]),)),
        CoverageInputs(observability=(_event(causes=tuple(["focus"] * 11)),)),
        CoverageInputs(observability=(_event(state=CoverageState.OBSERVABLE),)),
        CoverageInputs(observability=(_event(phase="closing"),)),
        CoverageInputs(observability=(_event(phase=EventPhase.CLOSED),)),
        CoverageInputs(observability=(_event(ended_at=_START),)),
        CoverageInputs(
            observability=(
                _event(phase=EventPhase.CLOSED, ended_at=_START - MS, opened_event_id=uuid.uuid4()),
            )
        ),
        CoverageInputs(observability=(_event(subject=Subject("person")),)),
        CoverageInputs(observability=(_event(subject=Subject("camera")),)),
        CoverageInputs(observability=(_event(subject=Subject("zone", camera_id=uuid.uuid4())),)),
        CoverageInputs(observability=(_event(subject=Subject("zone", signal_id=uuid.uuid4())),)),
        CoverageInputs(observability=(_event(event_id="x"),)),
        CoverageInputs(observability=(_event(clock_offset_ms=1.5),)),
        CoverageInputs(
            observability=(_event(event_id=uuid.UUID(int=7)), _event(event_id=uuid.UUID(int=7)))
        ),
        CoverageInputs(observability=[_event()]),  # type: ignore[arg-type]
        CoverageInputs(communication=(CommunicationInput(uuid.uuid4(), NODE, "silent", _START),)),  # type: ignore[arg-type]
        CoverageInputs(
            communication=(CommunicationInput(uuid.uuid4(), NODE, CommunicationState.MUTE, _NAIVE),)
        ),
        CoverageInputs(gates=(GateInput(uuid.uuid4(), _START, "active"),)),  # type: ignore[arg-type]
        CoverageInputs(assignments=(AssignmentInput(uuid.uuid4(), NODE, _START, _START),)),
        CoverageInputs(first_report_at=_NAIVE),
        {"observability": ()},
    ],
)
def test_invalid_inputs_are_rejected(inputs: Any) -> None:
    with pytest.raises(CoverageInputInvalid):
        compose_timeline(inputs, CoveragePeriod(_START, _START + MS))
    with pytest.raises(CoverageInputInvalid):
        state_at(inputs, _START)


def test_state_at_rejects_a_naive_instant() -> None:
    with pytest.raises(CoverageInputInvalid):
        state_at(CoverageInputs(), _NAIVE)
