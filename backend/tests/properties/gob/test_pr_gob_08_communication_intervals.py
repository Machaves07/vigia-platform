"""PR-GOB-08, parte del latido (TASK-223; BR-GOB-73, 74; NFR-GOB-47), contra PostgreSQL 16 real.

``RuleBasedStateMachine`` (perfil ``ci``, semilla fija y semilla de la sesión registradas por
``tests/conftest.py``) sobre la ruta real ``POST heartbeats`` (``tests/heartbeat_support.py``):
cada ejemplo es un nodo nuevo, recién dado de alta (``unknown``), con su intervalo de latido
(15, 60 o 600 s). Las reglas generan ``heartbeat_sequences`` con huecos: latidos nuevos tras un
hueco cualquiera, repeticiones del último ``heartbeat_id`` y esperas largas. Las **marcas de
mudo** las pone el modelo de la tarea ``detect_mute_nodes`` que completa TASK-224: cuando pasan
cinco veces el intervalo sin latido aceptado, el inventario pasa a ``mute`` con el intervalo
empezando en ``last_heartbeat_at`` (leído de la base).

Invariantes, sobre los ``node_communication_state_changed`` que la ruta escribió en el expediente:

- ``reachable`` se escribe **exactamente** en el primer latido aceptado tras ``unknown`` o ``mute``
  (con ``since`` y ``last_heartbeat_at`` iguales a su recepción) y nunca dos seguidos;
- cada intervalo ``mute`` del modelo empieza en ``last_heartbeat_at`` del inventario;
- la unión de intervalos (``unknown`` desde el alta, ``reachable`` y ``mute``) es una partición del
  periodo: empiezan en el alta, cada uno termina donde empieza el siguiente y nunca retroceden.

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
from tests.heartbeat_support import HeartbeatStack, NodeSite, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.communication_state import is_mute_at, mute_after_seconds
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

STEPS: Final = 10
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
    reachable_writes: list[dt.datetime] = field(default_factory=list)


class CommunicationIntervals(RuleBasedStateMachine):
    stack: ClassVar[HeartbeatStack]

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.site: NodeSite | None = None

    @initialize(interval=st.sampled_from(INTERVALS))
    def declare(self, interval: int) -> None:
        stack = self.stack
        stack.tick()
        self.site = stack.site(interval=interval)
        self.model.interval = interval
        self.model.declared_at = stack.now()
        self.model.intervals.append(Interval("unknown", stack.now()))

    # --- Latidos -----------------------------------------------------------------------------

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
            model.reachable_writes.append(received)
        model.last_heartbeat_at = received
        model.last_body = body

    @precondition(lambda self: self.model.last_body is not None)
    @rule(gap=st.integers(min_value=0, max_value=3_600))
    def the_same_heartbeat_again(self, gap: int) -> None:
        assert self.model.last_body is not None
        self.stack.tick(gap)
        self._post(dict(self.model.last_body))  # duplicado: no cambia nada

    @rule(seconds=st.integers(min_value=1, max_value=4_000))
    def silence(self, seconds: int) -> None:
        self.stack.tick(seconds)

    # --- Modelo de la tarea de mudo (TASK-224) ------------------------------------------------

    @rule()
    def detect_mute(self) -> None:
        model, stack = self.model, self.stack
        assert self.site is not None
        if model.last_heartbeat_at is None or model.intervals[-1].state != "reachable":
            return
        if not is_mute_at(model.last_heartbeat_at, stack.now(), model.interval):
            return
        inventory = stack.inventory(self.site.node_id)
        assert inventory is not None
        start = inventory["last_heartbeat_at"]
        assert start == model.last_heartbeat_at  # el intervalo mute empieza en el último latido
        stack.set_communication_state(self.site.node_id, "mute")
        model.intervals.append(Interval("mute", start))

    # --- Invariantes --------------------------------------------------------------------------

    @invariant()
    def reachable_is_written_exactly_on_the_first_heartbeat_after_unknown_or_mute(self) -> None:
        if self.site is None:
            return
        written = self.stack.communication(self.site)
        assert [record["state"] for record in written] == ["reachable"] * len(written)
        assert [record["since"] for record in written] == [
            format_timestamp(moment) for moment in self.model.reachable_writes
        ]
        assert all(record["since"] == record["last_heartbeat_at"] for record in written)

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
        # Cada intervalo termina donde empieza el siguiente (el último, ahora): sin huecos ni
        # solapes; un nodo con un solo latido seguido de silencio tiene un reachable vacío.
        assert all(start <= self.stack.now() for start in starts)

    @invariant()
    def the_inventory_is_reachable_after_an_accepted_heartbeat_until_a_mute_mark(self) -> None:
        if self.site is None or self.model.last_heartbeat_at is None:
            return
        inventory = self.stack.inventory(self.site.node_id)
        assert inventory is not None
        assert inventory["communication_state"] == self.model.intervals[-1].state
        assert inventory["last_heartbeat_at"] == self.model.last_heartbeat_at
        assert mute_after_seconds(self.model.interval) == 5 * self.model.interval


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "pr_gob_08") as built:
        yield built


def test_pr_gob_08_communication_intervals_partition_the_period(stack: HeartbeatStack) -> None:
    CommunicationIntervals.stack = stack
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(CommunicationIntervals)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))
