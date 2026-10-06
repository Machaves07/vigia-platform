"""PR-GOB-08, parte de mudos (TASK-225; BR-GOB-73, 74; NFR-GOB-47), contra PostgreSQL 16 real.

``RuleBasedStateMachine`` (perfil ``ci``, semilla fija y semilla de la sesión registradas por
``tests/conftest.py``) con la ruta real ``POST heartbeats`` de VIG-157 (``tests/heartbeat_support``)
y la tarea real ``detect_mute_nodes``: cada ejemplo es un nodo nuevo dado de alta (``unknown``)
con su intervalo de latido (15, 60 o 600 s). Las reglas generan ``heartbeat_sequences`` con
huecos (latidos tras un hueco cualquiera, repeticiones del último ``heartbeat_id``, silencios
largos) y ejecutan la tarea en cualquier momento, también dos veces seguidas.

Invariantes, sobre los ``node_communication_state_changed`` del expediente, el inventario y las
alarmas:

- la tarea escribe ``mute`` **solo** cuando pasaron **más de** cinco intervalos desde el último
  latido aceptado y el nodo no estaba ``mute``; todo intervalo ``mute`` empieza **exactamente** en
  ``last_heartbeat_at`` (``since`` y ``last_heartbeat_at`` del registro iguales al del inventario);
- un nodo que nunca latió sigue ``unknown`` (la tarea no escribe nada);
- la unión de intervalos ``unknown`` (desde el alta), ``reachable`` y ``mute`` es una partición del
  periodo: cada uno empieza donde termina el anterior, nunca retroceden y nunca hay dos estados
  iguales seguidos;
- ``node_mute`` está abierta exactamente desde la marca ``mute`` hasta la evaluación siguiente a
  la vuelta (aquí, solo la tarea de mudos: abierta mientras el nodo está ``mute``), y nunca hay dos
  abiertas.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import ClassVar, Final

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import _seeds_for_profile
from tests.fleet_alarm_support import AlarmRows, AlarmTasks, alarm_tasks, run_in
from tests.heartbeat_support import HeartbeatStack, NodeSite, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.mute_detection import is_silent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

STEPS: Final = 12
INTERVALS: Final = (15, 60, 600)


@dataclass
class Interval:
    state: str
    start: dt.datetime


@dataclass
class Model:
    interval: int = 60
    declared_at: dt.datetime | None = None
    intervals: list[Interval] = field(default_factory=list)
    last_heartbeat_at: dt.datetime | None = None
    last_body: dict[str, object] | None = None
    mute_open: bool = False
    mute_raised: int = 0


class MuteIntervals(RuleBasedStateMachine):
    stack: ClassVar[HeartbeatStack]
    tasks: ClassVar[AlarmTasks]

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.site: NodeSite | None = None
        self.rows = AlarmRows(self.stack.fetch)

    @initialize(interval=st.sampled_from(INTERVALS))
    def declare(self, interval: int) -> None:
        stack = self.stack
        stack.tick()
        self.site = stack.site(interval=interval)
        self.model.interval = interval
        self.model.declared_at = stack.now()
        self.model.intervals.append(Interval("unknown", stack.now()))

    # --- Latidos -------------------------------------------------------------------------------

    def _post(self, body: dict[str, object]) -> dt.datetime:
        assert self.site is not None
        received = to_millisecond(self.stack.now())
        response = self.stack.post(self.site, body)
        assert response.status_code == 200, response.text
        return received

    @rule(gap=st.integers(min_value=1, max_value=3_600))
    def heartbeat_after_a_gap(self, gap: int) -> None:
        assert self.site is not None
        self.stack.tick(gap)
        body = self.stack.body(self.site)
        received = self._post(body)
        model = self.model
        if model.intervals[-1].state != "reachable":
            model.intervals.append(Interval("reachable", received))
        model.last_heartbeat_at = received
        model.last_body = body

    @precondition(lambda self: self.model.last_body is not None)
    @rule(gap=st.integers(min_value=0, max_value=600))
    def the_same_heartbeat_again(self, gap: int) -> None:
        assert self.model.last_body is not None
        self.stack.tick(gap)
        self._post(dict(self.model.last_body))  # duplicado: no cambia nada

    @rule(seconds=st.sampled_from([1, 59, 74, 75, 76, 299, 300, 301, 2_999, 3_000, 3_001, 4_000]))
    def silence(self, seconds: int) -> None:
        # Los bordes de cinco intervalos (15, 60 y 600 s) a un segundo de cada lado.
        self.stack.tick(seconds)

    # --- La tarea real -------------------------------------------------------------------------

    @rule(twice=st.booleans())
    def detect_mute_nodes(self, twice: bool) -> None:
        assert self.site is not None
        stack, model = self.stack, self.model
        database = stack.primary.database
        expected = model.intervals[-1].state == "reachable" and is_silent(
            model.last_heartbeat_at, to_millisecond(stack.now()), model.interval
        )
        report = stack.run(run_in(database, self.site.organization_id, self.tasks.detector.detect))
        assert report.transitions == ([self.site.node_id] if expected else [])
        if twice:  # una segunda pasada en el mismo instante no escribe nada
            again = stack.run(
                run_in(database, self.site.organization_id, self.tasks.detector.detect)
            )
            assert again.transitions == [] and again.raised == []
        # La alarma abre con la transición, salvo que la anterior siga abierta (sin evaluación).
        opens = expected and not model.mute_open
        assert [alarm.alarm_kind.value for alarm in report.raised] == (
            ["node_mute"] if opens else []
        )
        if expected:
            assert model.last_heartbeat_at is not None
            model.intervals.append(Interval("mute", model.last_heartbeat_at))
        if opens:
            model.mute_open = True
            model.mute_raised += 1

    @rule()
    def evaluate_fleet_alarms(self) -> None:
        assert self.site is not None
        stack, model = self.stack, self.model
        stack.tick(60)  # un ciclo: la evaluación anterior nunca es de este
        report = stack.run(
            run_in(stack.primary.database, self.site.organization_id, self.tasks.evaluator.evaluate)
        )
        back = model.mute_open and model.intervals[-1].state == "reachable"
        assert [alarm.alarm_kind.value for alarm in report.cleared] == (
            ["node_mute"] if back else []
        )
        assert report.raised == []
        if back:
            model.mute_open = False

    # --- Invariantes --------------------------------------------------------------------------

    @invariant()
    def the_written_transitions_are_the_model(self) -> None:
        if self.site is None:
            return
        written = self.rows.communication(self.site.node_id)
        expected = self.model.intervals[1:]
        assert [record["state"] for record in written] == [i.state for i in expected]
        for record, interval in zip(written, expected, strict=True):
            assert record["since"] == format_timestamp(interval.start)
            if record["state"] == "mute":
                # El silencio empieza en el último latido aceptado, nunca en el barrido.
                assert record["last_heartbeat_at"] == record["since"]

    @invariant()
    def never_two_equal_states_in_a_row(self) -> None:
        states = [interval.state for interval in self.model.intervals]
        assert all(first != second for first, second in itertools.pairwise(states))

    @invariant()
    def the_intervals_partition_the_period(self) -> None:
        intervals = self.model.intervals
        if not intervals:
            return
        assert intervals[0].start == self.model.declared_at
        starts = [interval.start for interval in intervals]
        assert starts == sorted(starts)
        assert all(start <= self.stack.now() for start in starts)

    @invariant()
    def the_inventory_and_the_alarm_follow_the_state(self) -> None:
        if self.site is None:
            return
        node = self.site.node_id
        state = self.model.intervals[-1].state
        if self.model.last_heartbeat_at is None:
            assert self.stack.inventory(node) is None  # nunca latió: sigue unknown
            assert self.rows.alarms(node) == []
            return
        inventory = self.stack.inventory(node)
        assert inventory is not None
        assert inventory["communication_state"] == state
        assert inventory["last_heartbeat_at"] == self.model.last_heartbeat_at
        mute_alarms = self.rows.alarms(node, "node_mute")
        assert len(mute_alarms) == self.model.mute_raised
        assert sum(alarm["cleared_at"] is None for alarm in mute_alarms) == int(
            self.model.mute_open
        )
        if state == "mute":
            assert self.rows.open_kinds(node) == {"node_mute"}
        # Por transición: cada alarma cierra antes de que abra la siguiente.
        for first, second in itertools.pairwise(mute_alarms):
            assert first["cleared_at"] is not None and first["cleared_at"] <= second["raised_at"]


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "pr_gob_08_mute", alarm_events=True) as built:
        built.authz.sessions.clock.set(to_millisecond(built.now()))
        yield built


def test_pr_gob_08_mute_intervals_start_at_the_last_heartbeat_and_partition_the_period(
    stack: HeartbeatStack,
) -> None:
    outbox = stack.outbox
    sessions = stack.authz.sessions
    MuteIntervals.stack = stack
    MuteIntervals.tasks = alarm_tasks(
        clock=sessions.clock, outbox=outbox, writer=stack.writer, audit=sessions.audit
    )
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(MuteIntervals)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))
