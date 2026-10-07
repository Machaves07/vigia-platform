"""Propiedades de la prueba de oclusión (LC-GOB-07; TASK-215): PR-GOB-24 y PR-GOB-09 aplicada.

- **PR-GOB-24** (``occlusion_histories``): secuencias generadas de llegadas de eventos, consultas,
  declaraciones y avances del reloj simulado. Reevaluar dos veces deja lo mismo que reevaluar una;
  el estado es monótono (``pending`` pasa una vez a ``verified``, ``declared`` o ``failed`` y no
  vuelve); ``failed`` solo aparece con ``now > deadline``; ``blocking_occlusions`` bloquea la
  cámara mientras su prueba sigue ``pending`` con la fecha límite sin vencer; y, sin declaración,
  el resultado final no depende de cuándo se miró: es el de una sola evaluación vencida la fecha
  límite con todo lo recibido.
- **PR-GOB-09 aplicada** (``camera_outage_subsets``): para toda cobertura satisfacible y toda
  cámara ocluida, el criterio de ``verified`` coincide con el oráculo de ``coverage_state`` sobre
  las cámaras sin la ocluida, y con la lectura literal de BR-GOB-97 escrita aquí aparte.
- **NFR-GOB-44** con el reloj simulado: un evento de la cámara recibido a los 10 s o a los 4 min de
  ``ended_at`` deja ``verified``; a los 6 min, ``failed`` con
  ``no_observability_events_in_window``; los bordes de ±30 s y de ``deadline``, en los dos lados.
- **Alcance de nodo** (pendiente nº 40, con ``node_scope_events`` del kit): las copias en varias
  zonas no cuentan, no impiden ``verified`` ni valen como evento de la cámara.

Semillas: las del perfil activo (``tests/conftest.py``). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.conformance.generators.observability import node_scope_events
from vigia_contracts.models.enumerations import (
    DegradationCause,
    ObservabilityState,
    ObservabilitySubjectKind,
)
from vigia_contracts.models.observability_event import ObservabilityEvent

from tests.properties.gob.strategies.catalog import OutageCase, camera_outage_subsets
from vigia_platform.catalog.domain.coverage import MinimumCoverage, coverage_state
from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.catalog.domain.occlusion import (
    MAX_CORRELATED_EVENTS,
    MAX_WINDOW,
    VERIFICATION_WAIT,
    WINDOW_TOLERANCE,
    FailureReason,
    ObservedEvent,
    OcclusionRuleViolated,
    OcclusionTest,
    Resolution,
    blocking_occlusions,
    check_window,
    counted_events,
    new_test,
    resolve,
    verified_criterion,
    zone_drop_expected,
)
from vigia_platform.catalog.record_types import (
    MAX_CORRELATED_EVENTS as RECORD_MAX_CORRELATED_EVENTS,
)
from vigia_platform.catalog.record_types import OcclusionTestResult
from vigia_platform.shared.signing.keys import format_timestamp

T0: Final = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
"""Inicio de la ventana: un instante fijo del reloj simulado (nada se compara con la hora real)."""
MS: Final = timedelta(milliseconds=1)
REASON: Final = "El nodo no envió eventos durante la oclusión de la cámara"
USER: Final = uuid.UUID(int=7, version=4)
ORGANIZATION: Final = uuid.UUID(int=1, version=4)
PLANT: Final = uuid.UUID(int=2, version=4)
ZONE: Final = uuid.UUID(int=3, version=4)
SESSION: Final = uuid.UUID("01900000-0000-7000-8000-000000000001")
NODE: Final = uuid.UUID(int=9, version=4)
_ids = iter(range(1, 10**9))


def _uuid7() -> uuid.UUID:
    return uuid.UUID(f"01900000-0000-7000-8000-{next(_ids):012x}")


def _test(camera: uuid.UUID, length: timedelta = timedelta(seconds=20)) -> OcclusionTest:
    return new_test(
        test_id=_uuid7(),
        organization_id=ORGANIZATION,
        plant_id=PLANT,
        session_id=SESSION,
        camera_id=camera,
        started_at=T0,
        ended_at=T0 + length,
        recorded_by=USER,
    )


def _coverage(
    cameras: Sequence[uuid.UUID], required: Sequence[uuid.UUID] = (), count: int = 1
) -> MinimumCoverage:
    return MinimumCoverage(
        required_count=count, required_camera_ids=tuple(required), camera_ids=tuple(cameras)
    )


def _event(
    *,
    started_at: datetime,
    received_at: datetime,
    kind: ObservabilitySubjectKind = ObservabilitySubjectKind.CAMERA,
    camera: uuid.UUID | None = None,
    state: ObservabilityState = ObservabilityState.DEGRADED,
    causes: frozenset[DegradationCause] = frozenset({DegradationCause.OBSTRUCTION}),
    node: uuid.UUID = NODE,
    opened: uuid.UUID | None = None,
) -> ObservedEvent:
    return ObservedEvent(
        event_id=_uuid7(),
        node_id=node,
        subject_kind=kind,
        camera_id=camera if kind is ObservabilitySubjectKind.CAMERA else None,
        state=state,
        causes=causes,
        started_at=started_at,
        received_at=received_at,
        opened_event_id=opened,
    )


def _settle(
    test: OcclusionTest,
    events: Sequence[ObservedEvent],
    coverage: MinimumCoverage | None,
    now: datetime,
    reason: str | None = None,
) -> OcclusionTest:
    """Una reevaluación: lo visible en ``now`` es lo ya recibido; resuelve una sola vez."""
    visible = [event for event in events if event.received_at <= now]
    resolution = resolve(test, visible, coverage, now, reason)
    if resolution is None or test.resolved:
        return test
    return test.resolved_with(resolution, _uuid7())


# --- Estrategias ---------------------------------------------------------------------------------

_KIND = st.sampled_from(
    ["own_camera", "other_camera", "zone_drop", "zone_degraded", "zone_restored", "node_scope"]
)
_START_OFFSET_MS = st.one_of(
    st.sampled_from([-30_001, -30_000, -1, 0, 1, 20_000, 49_999, 50_000, 50_001]),
    st.integers(-120_000, 120_000),
)
"""Inicio del evento respecto de ``T0`` (la ventana es ``[T0, T0 + 20 s]``: borde en ±30 s)."""
_RECEIVED_AFTER_END_MS = st.one_of(
    st.sampled_from([0, 10_000, 240_000, 299_999, 300_000, 300_001, 360_000]),
    st.integers(0, 420_000),
)
"""Recepción respecto de ``ended_at``: ``deadline`` es 300 000 ms."""


@dataclasses.dataclass(frozen=True)
class Scenario:
    coverage: MinimumCoverage
    camera: uuid.UUID
    events: tuple[ObservedEvent, ...]


@st.composite
def occlusion_scenarios(draw: st.DrawFn) -> Scenario:
    """Cobertura, cámara ocluida y eventos generados alrededor de la ventana de 20 s."""
    case = draw(camera_outage_subsets())
    coverage = case.coverage
    camera = draw(st.sampled_from(coverage.camera_ids))
    others = [c for c in coverage.camera_ids if c != camera] or [uuid.UUID(int=99, version=4)]
    end = T0 + timedelta(seconds=20)
    events: list[ObservedEvent] = []
    for _ in range(draw(st.integers(0, 6))):
        kind = draw(_KIND)
        started = T0 + draw(_START_OFFSET_MS) * MS
        received = max(started, end + draw(_RECEIVED_AFTER_END_MS) * MS)
        if kind == "own_camera":
            events.append(_event(started_at=started, received_at=received, camera=camera))
        elif kind == "other_camera":
            other = draw(st.sampled_from(others))
            events.append(_event(started_at=started, received_at=received, camera=other))
        elif kind.startswith("zone"):
            state = {
                "zone_drop": ObservabilityState.NOT_OBSERVABLE,
                "zone_degraded": ObservabilityState.DEGRADED,
                "zone_restored": ObservabilityState.OBSERVABLE,
            }[kind]
            causes = frozenset() if kind == "zone_restored" else frozenset({DegradationCause.FOCUS})
            events.append(
                _event(
                    started_at=started,
                    received_at=received,
                    kind=ObservabilitySubjectKind.ZONE,
                    state=state,
                    causes=causes,
                )
            )
        else:
            node_kind = draw(
                st.sampled_from(
                    [
                        (ObservabilitySubjectKind.CLOCK, DegradationCause.CLOCK_UNSYNCHRONIZED),
                        (
                            ObservabilitySubjectKind.SIGNAL_READER,
                            DegradationCause.SIGNAL_READER_UNAVAILABLE,
                        ),
                        (
                            ObservabilitySubjectKind.LOCAL_QUEUE,
                            DegradationCause.LOCAL_QUEUE_OVER_THRESHOLD,
                        ),
                        (ObservabilitySubjectKind.ZONE, DegradationCause.NODE_RESTART),
                    ]
                )
            )
            events.append(
                _event(
                    started_at=started,
                    received_at=received,
                    kind=node_kind[0],
                    state=ObservabilityState.NOT_OBSERVABLE,
                    causes=frozenset({node_kind[1]}),
                )
            )
    return Scenario(coverage, camera, tuple(events))


_STEP = st.one_of(
    st.tuples(st.just("look"), st.integers(0, 120_000)),
    st.tuples(st.just("twice"), st.integers(0, 120_000)),
    st.tuples(st.just("declare"), st.integers(0, 120_000)),
)
"""Un paso de la historia: avanzar el reloj ``n`` ms y mirar, mirar dos veces o declarar."""


# --- PR-GOB-24 -----------------------------------------------------------------------------------


@given(scenario=occlusion_scenarios(), steps=st.lists(_STEP, max_size=12))
def test_pr_gob_24_lazy_reevaluation_is_idempotent_monotonic_and_honest(
    scenario: Scenario, steps: list[tuple[str, int]]
) -> None:
    test = _test(scenario.camera)
    now = test.ended_at
    first_resolution: Resolution | None = None
    declared_once = False
    for action, advance in steps:
        now += advance * MS
        before = test
        if action == "declare":
            test = _settle(test, scenario.events, scenario.coverage, now, REASON)
            if test.verification is OcclusionVerification.DECLARED and not before.resolved:
                declared_once = True
                visible = [e for e in scenario.events if e.received_at <= now]
                # La declaración solo se admite antes de deadline y sin eventos contados.
                assert now <= test.deadline
                assert counted_events(test, visible) == ()
        else:
            test = _settle(test, scenario.events, scenario.coverage, now)
            if action == "twice":
                again = _settle(test, scenario.events, scenario.coverage, now)
                assert again == test  # reevaluar dos veces = reevaluar una
        # Monotonía: lo resuelto no cambia ni vuelve a pending.
        if before.resolved:
            assert test == before
        if test.resolved:
            if first_resolution is None:
                first_resolution = test.resolution
            assert test.resolution == first_resolution
        # failed solo con now > deadline; verified siempre con eventos de la cámara.
        if test.verification is OcclusionVerification.FAILED:
            assert now > test.deadline
        if test.verification is OcclusionVerification.VERIFIED:
            assert test.correlated_event_ids
        # El acta no se cierra con una prueba pending cuya fecha límite no venció.
        blocking = blocking_occlusions([test], scenario.coverage.camera_ids, now)
        if test.verification is OcclusionVerification.PENDING and now <= test.deadline:
            assert scenario.camera in blocking
        if test.verification in (OcclusionVerification.VERIFIED, OcclusionVerification.DECLARED):
            assert scenario.camera not in blocking
        if test.verification is OcclusionVerification.FAILED:
            assert scenario.camera in blocking
    # Vencida la fecha límite, sin declaración, el resultado no depende de cuándo se miró.
    late = test.deadline + timedelta(seconds=1)
    final = _settle(test, scenario.events, scenario.coverage, max(now, late))
    assert final.resolved
    if not declared_once:
        once = _settle(_test(scenario.camera), scenario.events, scenario.coverage, late)
        assert final.verification is once.verification
        # Un verified temprano (la zona debía caer y cayó) guarda los eventos que había al
        # resolverse: un subconjunto de los de la evaluación vencida.
        assert set(final.correlated_event_ids or ()) <= set(once.correlated_event_ids or ())
        if final.verification is not OcclusionVerification.VERIFIED:
            assert final.resolution == once.resolution


@given(scenario=occlusion_scenarios())
def test_pr_gob_24_a_written_resolution_is_a_valid_occlusion_test_result(
    scenario: Scenario,
) -> None:
    test = _settle(
        _test(scenario.camera),
        scenario.events,
        scenario.coverage,
        T0 + timedelta(minutes=10),
    )
    content = test.record_content()
    OcclusionTestResult.model_validate_json(json.dumps(content))
    assert content["deadline"] == format_timestamp(test.ended_at + VERIFICATION_WAIT)
    assert content["verification"] != "pending"


# --- PR-GOB-09 aplicada --------------------------------------------------------------------------


def _literal_drop(coverage: MinimumCoverage, camera: uuid.UUID) -> bool:
    """BR-GOB-97 leída aparte: perder una requerida o bajar de ``required_count``."""
    return camera in coverage.required_camera_ids or (
        len(coverage.camera_ids) - 1 < coverage.required_count
    )


@given(case=camera_outage_subsets(), camera_seen=st.booleans(), zone_dropped=st.booleans())
def test_pr_gob_09_the_verified_criterion_matches_the_coverage_oracle(
    case: OutageCase, camera_seen: bool, zone_dropped: bool
) -> None:
    coverage = case.coverage
    occluded = sorted(case.down) or list(coverage.camera_ids)
    end = T0 + timedelta(seconds=20)
    for camera in occluded:
        oracle = (
            coverage_state(coverage, [c for c in coverage.camera_ids if c != camera])
            is ObservabilityState.NOT_OBSERVABLE
        )
        assert zone_drop_expected(coverage, camera) is oracle
        assert oracle is _literal_drop(coverage, camera)
        events = []
        if camera_seen:
            events.append(_event(started_at=T0, received_at=end, camera=camera))
        if zone_dropped:
            events.append(
                _event(
                    started_at=T0,
                    received_at=end,
                    kind=ObservabilitySubjectKind.ZONE,
                    state=ObservabilityState.NOT_OBSERVABLE,
                )
            )
        test = _test(camera)
        expected = camera_seen and zone_dropped == oracle
        assert verified_criterion(camera, counted_events(test, events), coverage) is expected
        settled = _settle(test, events, coverage, test.deadline + MS)
        verified = settled.verification is OcclusionVerification.VERIFIED
        assert verified is expected


# --- NFR-GOB-44 ----------------------------------------------------------------------------------

CAMERAS: Final = (uuid.UUID(int=11, version=4), uuid.UUID(int=12, version=4))
REDUNDANT: Final = _coverage(CAMERAS, required=(), count=1)
"""Dos cámaras, una basta: ocluir cualquiera conserva la cobertura (la zona no cae)."""
REQUIRED: Final = _coverage(CAMERAS, required=(CAMERAS[0],), count=1)
"""La primera es requerida: ocluirla debe dejar la zona ``not_observable``."""


@pytest.mark.parametrize("after_end", [timedelta(seconds=10), timedelta(minutes=4)])
def test_nfr_gob_44_a_camera_event_received_in_time_verifies(after_end: timedelta) -> None:
    test = _test(CAMERAS[1])
    event = _event(started_at=T0 + MS, received_at=test.ended_at + after_end, camera=CAMERAS[1])
    # Antes de la fecha límite la zona redundante aún podría caer: sigue pending (P2).
    assert _settle(test, [event], REDUNDANT, test.ended_at + after_end).verification == "pending"
    settled = _settle(test, [event], REDUNDANT, test.deadline + MS)
    assert settled.verification is OcclusionVerification.VERIFIED
    assert settled.correlated_event_ids == (event.event_id,)


def test_nfr_gob_44_a_camera_event_received_at_six_minutes_fails() -> None:
    test = _test(CAMERAS[1])
    event = _event(
        started_at=T0 + MS, received_at=test.ended_at + timedelta(minutes=6), camera=CAMERAS[1]
    )
    assert _settle(test, [event], REDUNDANT, test.deadline).verification == "pending"
    settled = _settle(test, [event], REDUNDANT, test.ended_at + timedelta(minutes=6))
    assert settled.verification is OcclusionVerification.FAILED
    assert settled.failure_reason is FailureReason.NO_OBSERVABILITY_EVENTS_IN_WINDOW
    assert settled.correlated_event_ids == ()


def test_nfr_gob_44_a_required_camera_verifies_as_soon_as_the_zone_drops() -> None:
    test = _test(CAMERAS[0])
    received = test.ended_at + timedelta(seconds=10)
    events = [
        _event(started_at=T0 + MS, received_at=received, camera=CAMERAS[0]),
        _event(
            started_at=T0 + 2 * MS,
            received_at=received,
            kind=ObservabilitySubjectKind.ZONE,
            state=ObservabilityState.NOT_OBSERVABLE,
        ),
    ]
    settled = _settle(test, events, REQUIRED, received)
    assert settled.verification is OcclusionVerification.VERIFIED
    assert settled.correlated_event_ids == (events[0].event_id, events[1].event_id)
    # Sin la caída de la zona, la cámara requerida no verifica la redundancia (BR-GOB-99).
    alone = _settle(_test(CAMERAS[0]), events[:1], REQUIRED, test.deadline + MS)
    assert alone.verification is OcclusionVerification.FAILED
    assert alone.failure_reason is FailureReason.REDUNDANCY_NOT_VERIFIED


def test_nfr_gob_44_a_redundant_camera_with_the_zone_down_fails() -> None:
    test = _test(CAMERAS[1])
    events = [
        _event(started_at=T0, received_at=test.ended_at, camera=CAMERAS[1]),
        _event(
            started_at=T0,
            received_at=test.deadline,
            kind=ObservabilitySubjectKind.ZONE,
            state=ObservabilityState.NOT_OBSERVABLE,
        ),
    ]
    settled = _settle(test, events, REDUNDANT, test.deadline + MS)
    assert settled.verification is OcclusionVerification.FAILED
    assert settled.failure_reason is FailureReason.REDUNDANCY_NOT_VERIFIED


@pytest.mark.parametrize(
    ("offset", "counted"),
    [
        (-WINDOW_TOLERANCE, True),
        (-WINDOW_TOLERANCE - MS, False),
        (timedelta(seconds=20) + WINDOW_TOLERANCE, True),
        (timedelta(seconds=20) + WINDOW_TOLERANCE + MS, False),
    ],
    ids=["start-30s", "start-30s-1ms", "end+30s", "end+30s+1ms"],
)
def test_nfr_gob_44_the_30_second_tolerance_on_both_edges(offset: timedelta, counted: bool) -> None:
    test = _test(CAMERAS[1])
    event = _event(started_at=T0 + offset, received_at=test.ended_at, camera=CAMERAS[1])
    assert (counted_events(test, [event]) != ()) is counted
    settled = _settle(test, [event], REDUNDANT, test.deadline + MS)
    expected = OcclusionVerification.VERIFIED if counted else OcclusionVerification.FAILED
    assert settled.verification is expected


@pytest.mark.parametrize(("late", "counted"), [(timedelta(0), True), (MS, False)])
def test_nfr_gob_44_received_at_the_deadline_counts_and_one_ms_later_does_not(
    late: timedelta, counted: bool
) -> None:
    test = _test(CAMERAS[1])
    event = _event(started_at=T0, received_at=test.deadline + late, camera=CAMERAS[1])
    assert (counted_events(test, [event]) != ()) is counted


def test_failed_only_after_the_deadline_and_never_from_silence_to_verified() -> None:
    test = _test(CAMERAS[1])
    assert _settle(test, [], REDUNDANT, test.deadline).verification == "pending"
    silent = _settle(test, [], REDUNDANT, test.deadline + MS)
    assert silent.verification is OcclusionVerification.FAILED
    assert silent.failure_reason is FailureReason.NO_OBSERVABILITY_EVENTS_IN_WINDOW
    # Un catálogo cuya cobertura no se entiende nunca deja verified (falla cerrado).
    event = _event(started_at=T0, received_at=test.ended_at, camera=CAMERAS[1])
    assert _settle(test, [event], None, test.deadline + MS).verification == "failed"


# --- Declaración ---------------------------------------------------------------------------------


def test_a_declaration_before_the_deadline_without_events_is_declared() -> None:
    test = _test(CAMERAS[1])
    declared = _settle(test, [], REDUNDANT, test.deadline, REASON)
    assert declared.verification is OcclusionVerification.DECLARED
    assert declared.declared_reason_es == REASON
    assert declared.correlated_event_ids == ()
    # El acta la distingue de verified: el registro dice declared con su motivo.
    content = declared.record_content()
    assert (content["verification"], content["declared_reason_es"]) == ("declared", REASON)
    assert blocking_occlusions([declared], CAMERAS[1:], test.deadline + timedelta(hours=1)) == ()


def test_a_declaration_after_the_deadline_or_with_counted_events_is_not_admitted() -> None:
    test = _test(CAMERAS[1])
    assert resolve(test, [], REDUNDANT, test.deadline + MS, REASON) == Resolution(
        OcclusionVerification.FAILED, ()
    )
    other = _event(started_at=T0, received_at=test.ended_at, camera=CAMERAS[0])
    assert resolve(test, [other], REDUNDANT, test.deadline, REASON) is None


# --- Alcance de nodo (pendiente nº 40) -----------------------------------------------------------


_NODE_CAUSE: Final = {
    "clock": "clock_unsynchronized",
    "signal_reader": "signal_reader_unavailable",
    "local_queue": "local_queue_over_threshold",
    "zone": "node_restart",
}


def _catalog(zone: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": str(ORGANIZATION), "plant_id": str(PLANT), "zone_id": str(zone)}


@given(
    data=st.data(),
    zones=st.integers(1, 4),
    camera_after=st.sampled_from([timedelta(seconds=1), timedelta(minutes=4)]),
)
def test_node_scope_copies_neither_count_nor_block_verified(
    data: st.DataObject, zones: int, camera_after: timedelta
) -> None:
    others = [uuid.UUID(int=500 + index, version=4) for index in range(zones - 1)]
    copies = data.draw(
        node_scope_events([_catalog(z) for z in (ZONE, *others)], accepted=True), label="copies"
    )
    test = _test(CAMERAS[1])
    stamp = format_timestamp(T0 + timedelta(seconds=5))
    received = test.ended_at + camera_after
    observed = []
    for copy in copies:
        documents = [{**copy, "started_at": stamp}]
        if copy["phase"] == "closed":
            # El kit da el cierre suelto: se le pone delante su apertura, como la enviaría el nodo.
            documents[0]["ended_at"] = stamp
            opening = {
                key: value
                for key, value in documents[0].items()
                if key not in ("opened_event_id", "ended_at")
            }
            kind = copy["subject"]["kind"]
            opening.update(
                event_id=str(_uuid7()),
                phase="opened",
                state="not_observable",
                causes=[_NODE_CAUSE[kind]],
            )
            documents[0]["opened_event_id"] = opening["event_id"]
            documents.insert(0, opening)
        for document in documents:
            # Siguen siendo eventos del contrato.
            ObservabilityEvent.model_validate_json(json.dumps(document))
            observed.append(ObservedEvent.of_content(document, received))
    # Ninguna copia cuenta: tampoco como evento de la cámara ni como caída de la zona.
    assert counted_events(test, observed) == ()
    alone = _settle(test, observed, REDUNDANT, test.deadline + MS)
    assert alone.verification is OcclusionVerification.FAILED
    assert alone.failure_reason is FailureReason.NO_OBSERVABILITY_EVENTS_IN_WINDOW
    camera = _event(started_at=T0, received_at=received, camera=CAMERAS[1])
    settled = _settle(test, [*observed, camera], REDUNDANT, test.deadline + MS)
    assert settled.verification is OcclusionVerification.VERIFIED
    assert settled.correlated_event_ids == (camera.event_id,)


def test_a_zone_copy_sharing_node_causes_and_start_with_a_node_event_does_not_count() -> None:
    """Las copias se agrupan por ``node_id``, ``causes`` y ``started_at`` (pendiente nº 40): el
    cierre de zona de un reinicio llega suelto (sin su apertura en la lectura) junto al cierre del
    reloj del mismo nodo, con el mismo inicio y sin causas; no cuenta."""
    test = _test(CAMERAS[1])
    clock_closed = _event(
        started_at=T0,
        received_at=test.ended_at,
        kind=ObservabilitySubjectKind.CLOCK,
        state=ObservabilityState.OBSERVABLE,
        causes=frozenset(),
        opened=_uuid7(),
    )
    zone_closed = _event(
        started_at=T0,
        received_at=test.ended_at,
        kind=ObservabilitySubjectKind.ZONE,
        state=ObservabilityState.OBSERVABLE,
        causes=frozenset(),
        opened=_uuid7(),
    )
    assert not zone_closed.node_scope
    assert counted_events(test, [clock_closed, zone_closed]) == ()
    # Otro nodo o otro inicio no es una copia: ese sí cuenta.
    other_node = dataclasses.replace(zone_closed, node_id=uuid.UUID(int=10, version=4))
    assert counted_events(test, [clock_closed, other_node]) == (other_node,)


def test_a_node_restart_zone_drop_does_not_count_as_the_zone_falling() -> None:
    test = _test(CAMERAS[1])
    restart = _event(
        started_at=T0,
        received_at=test.ended_at,
        kind=ObservabilitySubjectKind.ZONE,
        state=ObservabilityState.NOT_OBSERVABLE,
        causes=frozenset({DegradationCause.NODE_RESTART}),
    )
    restored = _event(
        started_at=T0,
        received_at=test.ended_at,
        kind=ObservabilitySubjectKind.ZONE,
        state=ObservabilityState.OBSERVABLE,
        causes=frozenset(),
        opened=restart.event_id,
    )
    camera = _event(started_at=T0, received_at=test.ended_at, camera=CAMERAS[1])
    assert counted_events(test, [restart, restored, camera]) == (camera,)
    settled = _settle(test, [restart, restored, camera], REDUNDANT, test.deadline + MS)
    assert settled.verification is OcclusionVerification.VERIFIED


# --- Bordes de la entrada ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0, T0),  # ventana vacía
        (T0 + MS, T0),  # invertida
        (T0, T0 + timedelta(days=1)),  # termina después de ahora
        (T0 - MAX_WINDOW - MS, T0),  # más larga que el máximo
        (datetime.min.replace(tzinfo=UTC), T0),  # sin desbordes
        (T0.replace(tzinfo=None), T0),  # sin zona horaria
        ("2026-10-06T12:00:00Z", T0),  # texto
    ],
)
def test_check_window_rejects_out_of_bounds_windows(start: Any, end: Any) -> None:
    with pytest.raises(OcclusionRuleViolated):
        check_window(start, end, T0 + timedelta(seconds=1))


def test_check_window_accepts_the_maximum_window_ending_now() -> None:
    assert check_window(T0 - MAX_WINDOW, T0, T0) == (T0 - MAX_WINDOW, T0)


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"subject": None},
        {"subject": {"kind": "camera"}, "event_id": "x"},
        {
            "event_id": str(uuid.uuid4()),
            "node_id": str(NODE),
            "subject": {"kind": "planet"},
            "state": "degraded",
            "causes": [],
            "started_at": "2026-10-06T12:00:00.000Z",
        },
    ],
)
def test_a_malformed_event_is_value_error_never_a_guess(content: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ObservedEvent.of_content(content, T0)


def test_correlated_events_are_capped_like_the_record_and_keep_the_camera_first() -> None:
    assert MAX_CORRELATED_EVENTS == RECORD_MAX_CORRELATED_EVENTS
    test = _test(CAMERAS[1])
    noise = [
        _event(started_at=T0, received_at=test.ended_at, camera=CAMERAS[0])
        for _ in range(MAX_CORRELATED_EVENTS + 5)
    ]
    own = _event(started_at=T0 + 3 * MS, received_at=test.ended_at, camera=CAMERAS[1])
    settled = _settle(test, [*noise, own], REDUNDANT, test.deadline + MS)
    assert settled.verification is OcclusionVerification.VERIFIED
    ids = settled.correlated_event_ids or ()
    assert (len(ids), ids[0]) == (MAX_CORRELATED_EVENTS, own.event_id)


def test_blocking_uses_the_last_test_of_each_camera() -> None:
    failed = _settle(_test(CAMERAS[0]), [], REDUNDANT, T0 + timedelta(hours=1))
    retried = _settle(_test(CAMERAS[0]), [], REDUNDANT, T0 + timedelta(seconds=30), REASON)
    assert failed.verification is OcclusionVerification.FAILED
    assert retried.verification is OcclusionVerification.DECLARED
    later = T0 + timedelta(hours=2)
    assert blocking_occlusions([failed], CAMERAS, later) == CAMERAS
    assert blocking_occlusions([failed, retried], CAMERAS, later) == (CAMERAS[1],)
    pending = _test(CAMERAS[1])
    assert blocking_occlusions([pending], CAMERAS[1:], pending.deadline) == (CAMERAS[1],)
    assert blocking_occlusions([pending], CAMERAS[1:], pending.deadline + MS) == ()
