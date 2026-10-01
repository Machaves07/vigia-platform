"""PR-NUC-30, PR-NUC-31 y PR-NUC-33: la bandeja frente a un modelo simplificado (TASK-129; PBT-06).

**PR-NUC-30** (``outbox_commands``): tras cualquier secuencia de ``publish``, ``deliver_ok``,
``deliver_fail``, ``dependency_down``, ``dependency_up``, ``crash_between_effect_and_ack`` y
``replay``, cada evento se entrega a cada consumidor al menos una vez, ninguno se procesa con
efecto dos veces, ninguno se entrega fuera del orden de su partición, ninguno cae en cola muerta
por circuito abierto, y lo entregado más la cola muerta iguala a lo publicado al drenar.

Contra PostgreSQL 16 real, como ``vigia_app``, con el ``Outbox`` y el ``Dispatcher`` de verdad y
dos consumidores guionizados (``tests/dispatch_support.py``): ``dispatch_plain`` sin dependencia
externa y ``dispatch_external`` con ella. Los comandos:

- ``publish``: de uno a tres eventos en una transacción, en dos organizaciones y tres particiones
  (dos plantas y la de organización); casi siempre en el mismo milisegundo, así que el orden
  dentro de la partición lo decide ``publish_seq`` (seguimiento 1 de VIG-47);
- ``script``: el guion de un evento para un consumidor: defectos (``deliver_fail``; nueve o más
  llevan a la cola muerta), ``ExternalDependencyDown`` o una caída entre el efecto y la
  confirmación (``crash_between_effect_and_ack``);
- ``dependency_down`` / ``dependency_up``: el manejador externo lanza ``ExternalDependencyDown``
  mientras dure;
- ``dispatch``: una ronda (``deliver_ok`` y lo que el guion diga);
- ``advance``: el reloj, por los bordes del retroceso y de la sonda de 60 s;
- ``replay``: el operador reprocesa una entrega de la cola muerta.

El modelo es independiente del código: por partición, la cabeza es el primer evento sin resolver
en orden de publicación; vence si su ``next_attempt_at`` llegó; el circuito sigue BR-NUC-79 (abre
con la primera caída, pausa todo, una sonda cada 60 s, cierra con su éxito o con un defecto); un
defecto suma un intento y reintenta tras ``[1, 4, 16, 64, 256, 600, 600][k-1]`` s (variación fija
en el centro) hasta la cola muerta en el octavo.

**Después de cada comando** la base coincide con el modelo: estado e intentos de cada entrega,
filas de la cola muerta, circuitos, y el efecto en la base (un eco publicado en la transacción de
la entrega) existe una vez si y solo si la entrega está ``delivered``. Y en cada ronda: ningún
evento se invoca si otro anterior de su partición no estaba resuelto, ninguna entrega completa
dos veces, el efecto externo idempotente queda una vez, y el manejador corre con la organización
y el ``correlation_id`` del evento (**PR-NUC-33**). Al final se drena y lo entregado más la cola
muerta iguala a lo publicado; ninguna entrega de ``dispatch_external`` llega a la cola muerta sin
ocho defectos propios.

**PR-NUC-31** (``failure_sequences``): el retraso del intento ``k`` es ``min(600, 4^(k-1))`` s
con variación acotada, y el octavo fallo produce cola muerta.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import seed as hypothesis_seed
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
from tests.dispatch_support import (
    EXTERNAL,
    SCRIPTED_CONSUMERS,
    Behavior,
    DispatchEnvironment,
    SimulatedCrash,
    dispatch_environment,
    operator_context,
    replay_service,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.outbox.breaker import PROBE_INTERVAL
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.retry import (
    BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_ATTEMPTS,
    DeadLetter,
    Retry,
    RetryPolicy,
)

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 15
"""Cada paso son varias transacciones reales más la comparación completa con el modelo."""

_OPEN = ("pending", "retrying")


# --- PR-NUC-31 ---------------------------------------------------------------------------------


@given(
    attempt=st.integers(1, MAX_ATTEMPTS - 1),
    jitter=st.floats(0.0, 1.0, exclude_max=True, allow_nan=False),
)
def test_pr_nuc_31_retry_delay_is_bounded_around_four_to_the_k(attempt: int, jitter: float) -> None:
    base = min(600, 4 ** (attempt - 1))
    assert RetryPolicy.base_delay_seconds(attempt) == base == BACKOFF_SECONDS[attempt - 1]
    decision = RetryPolicy.after_failure(attempt, jitter)
    assert isinstance(decision, Retry) and decision.attempts == attempt
    seconds = decision.delay.total_seconds()
    assert base * (1 - JITTER_RATIO) - 0.001 <= seconds <= base * (1 + JITTER_RATIO) + 0.001
    assert decision.delay == timedelta(milliseconds=decision.delay // timedelta(milliseconds=1))


@given(jitter=st.floats(0.0, 1.0, exclude_max=True, allow_nan=False))
def test_pr_nuc_31_the_eighth_failure_is_a_dead_letter(jitter: float) -> None:
    assert RetryPolicy.after_failure(MAX_ATTEMPTS, jitter) == DeadLetter(MAX_ATTEMPTS)
    waits = sum(RetryPolicy.base_delay_seconds(k) for k in range(1, MAX_ATTEMPTS))
    assert waits == 1541  # siete esperas, unos 26 minutos (BR-NUC-78)


@given(
    attempt=st.one_of(st.integers(max_value=0), st.integers(min_value=MAX_ATTEMPTS + 1)),
    jitter=st.one_of(
        st.floats(max_value=0.0, exclude_max=True),
        st.floats(min_value=1.0),
        st.just(math.nan),
        st.just(math.inf),
    ),
)
def test_pr_nuc_31_out_of_range_inputs_are_rejected(attempt: int, jitter: float) -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy.after_failure(attempt, 0.5)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy.after_failure(1, jitter)
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy.after_failure(True, 0.5)  # type: ignore[arg-type]


# --- PR-NUC-30 y PR-NUC-33: máquina con estado -------------------------------------------------


@dataclass
class ModelEvent:
    event_id: uuid.UUID
    organization_id: uuid.UUID
    partition_key: str
    order: int


@dataclass
class ModelDelivery:
    status: str = "pending"
    attempts: int = 0
    next_at: datetime | None = None
    dead_letters: int = 0
    defects_since_replay: int = 0


@dataclass
class ModelCircuit:
    state: str = "closed"
    changed_at: datetime | None = None


@dataclass
class Model:
    events: list[ModelEvent] = field(default_factory=list)
    deliveries: dict[tuple[str, uuid.UUID], ModelDelivery] = field(default_factory=dict)
    circuits: dict[str, ModelCircuit] = field(
        default_factory=lambda: {name: ModelCircuit() for name in SCRIPTED_CONSUMERS}
    )
    scripts: dict[tuple[str, uuid.UUID], deque[Behavior]] = field(default_factory=dict)
    dependency_down: bool = False


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "outbox_stateful") as env:
        OutboxMachine.env = env
        yield env


behaviors = st.one_of(
    st.lists(st.just(Behavior.DEFECT), min_size=1, max_size=MAX_ATTEMPTS + 1),
    st.just([Behavior.CRASH_AFTER_EFFECT]),
    st.lists(st.sampled_from([Behavior.DEFECT, Behavior.DEPENDENCY_DOWN]), min_size=1, max_size=3),
)
seconds = st.sampled_from([0, 1, 4, 16, 59, 60, 61, 256, 600, 601])


class OutboxMachine(RuleBasedStateMachine):
    env: DispatchEnvironment

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.organizations = (uuid.uuid4(), uuid.uuid4())
        self.plants = (uuid.uuid4(), uuid.uuid4())
        self.dispatcher: Dispatcher = self.env.dispatcher()

    @initialize()
    def start(self) -> None:
        self.env.run(self.env.quiesce())

    # --- utilidades ------------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self.env.clock.now()

    def _external(self, consumer: str) -> bool:
        return consumer == EXTERNAL

    def _heads(self, consumer: str) -> list[ModelEvent]:
        """La cabeza de cada partición del consumidor, en orden de publicación."""
        heads: dict[str, ModelEvent] = {}
        for event in self.model.events:
            delivery = self.model.deliveries[(consumer, event.event_id)]
            if delivery.status in _OPEN and event.partition_key not in heads:
                heads[event.partition_key] = event
        return sorted(heads.values(), key=lambda event: event.order)

    def _due(self, consumer: str, event: ModelEvent) -> bool:
        next_at = self.model.deliveries[(consumer, event.event_id)].next_at
        return next_at is not None and next_at <= self.now

    def _behavior(self, consumer: str, event: ModelEvent) -> Behavior:
        queued = self.model.scripts.get((consumer, event.event_id))
        if queued:
            return queued.popleft()
        if self._external(consumer) and self.model.dependency_down:
            return Behavior.DEPENDENCY_DOWN
        return Behavior.OK

    def _model_round(self, consumer: str) -> tuple[list[uuid.UUID], bool]:
        """Una ronda en el modelo: eventos invocados y si terminó en caída."""
        circuit = self.model.circuits[consumer]
        probe = False
        if circuit.state != "closed":
            assert circuit.changed_at is not None
            if self.now < circuit.changed_at + PROBE_INTERVAL:
                return [], False
            circuit.state, circuit.changed_at = "half_open", self.now
            probe = True
        due = [event for event in self._heads(consumer) if self._due(consumer, event)]
        invoked: list[uuid.UUID] = []
        for event in due[:1] if probe else due:
            delivery = self.model.deliveries[(consumer, event.event_id)]
            behavior = self._behavior(consumer, event)
            invoked.append(event.event_id)
            if behavior is Behavior.CRASH_AFTER_EFFECT:
                return invoked, True
            if behavior is Behavior.OK:
                delivery.status = "delivered"
                if probe:
                    circuit.state, circuit.changed_at = "closed", None
                continue
            if behavior is Behavior.DEPENDENCY_DOWN and self._external(consumer):
                if probe or circuit.state == "closed":
                    circuit.state, circuit.changed_at = "open", self.now
                break
            # Defecto (o ExternalDependencyDown sin dependencia declarada): un intento.
            if probe:
                circuit.state, circuit.changed_at = "closed", None
            delivery.attempts += 1
            delivery.defects_since_replay += 1
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = "dead_letter"
                delivery.dead_letters += 1
            else:
                delivery.status = "retrying"
                delivery.next_at = self.now + timedelta(
                    seconds=BACKOFF_SECONDS[delivery.attempts - 1]
                )
        return invoked, False

    # --- comandos ----------------------------------------------------------------------------

    @rule(
        organization=st.integers(0, 1),
        partitions=st.lists(st.integers(0, 2), min_size=1, max_size=3),
    )
    def publish(self, organization: int, partitions: list[int]) -> None:
        organization_id = self.organizations[organization]
        plants = [self.plants[p] if p < 2 else None for p in partitions]
        published = self.env.run(self.env.publish(organization_id, plants))
        for event in published:
            self.model.events.append(
                ModelEvent(
                    event.event_id,
                    event.organization_id,
                    event.partition_key,
                    len(self.model.events),
                )
            )
            for consumer in SCRIPTED_CONSUMERS:
                self.model.deliveries[(consumer, event.event_id)] = ModelDelivery(next_at=self.now)

    @precondition(lambda self: bool(self.model.events))
    @rule(consumer=st.sampled_from(SCRIPTED_CONSUMERS), index=st.integers(0), plan=behaviors)
    def script(self, consumer: str, index: int, plan: list[Behavior]) -> None:
        event = self.model.events[index % len(self.model.events)]
        self.model.scripts.setdefault((consumer, event.event_id), deque()).extend(plan)
        self.env.handlers[consumer].plan(event.event_id, plan)

    @rule()
    def dependency_down(self) -> None:
        self.model.dependency_down = True
        self.env.handlers[EXTERNAL].mode = Behavior.DEPENDENCY_DOWN

    @rule()
    def dependency_up(self) -> None:
        self.model.dependency_down = False
        self.env.handlers[EXTERNAL].mode = Behavior.OK

    @rule(seconds=seconds)
    def advance(self, seconds: int) -> None:
        self.env.clock.advance(seconds)

    @rule(consumer=st.sampled_from(SCRIPTED_CONSUMERS))
    def dispatch(self, consumer: str) -> None:
        self._dispatch(consumer)

    def _dispatch(self, consumer: str) -> None:
        handler = self.env.handlers[consumer]
        before = len(handler.invocations)
        resolved_before = {
            event.event_id
            for event in self.model.events
            if self.model.deliveries[(consumer, event.event_id)].status not in _OPEN
        }
        expected, crashed = self._model_round(consumer)
        if crashed:
            with pytest.raises(SimulatedCrash):
                self.env.run(self.dispatcher.dispatch_once(consumer))
        else:
            self.env.run(self.dispatcher.dispatch_once(consumer))
        invoked = handler.invocations[before:]
        assert [i.event_id for i in invoked] == expected
        by_id = {event.event_id: event for event in self.model.events}
        for invocation in invoked:
            # Orden por partición (BR-NUC-77): todo lo anterior de la partición, resuelto.
            event = by_id[invocation.event_id]
            earlier = [
                other.event_id
                for other in self.model.events
                if other.partition_key == event.partition_key and other.order < event.order
            ]
            assert set(earlier) <= resolved_before
            # PR-NUC-33: la organización (y la correlación) del contexto son las del evento.
            assert invocation.context_organization_id == invocation.event_organization_id
            assert invocation.context_correlation_id == invocation.event_correlation_id

    @precondition(
        lambda self: any(d.status == "dead_letter" for d in self.model.deliveries.values())
    )
    @rule(index=st.integers(0))
    def replay(self, index: int) -> None:
        dead = sorted(
            (key for key, d in self.model.deliveries.items() if d.status == "dead_letter"),
            key=lambda key: (key[0], str(key[1])),
        )
        consumer, event_id = dead[index % len(dead)]
        replay, _ = replay_service(self.env)
        self.env.run(replay.replay(operator_context(self.env), event_id, consumer))
        delivery = self.model.deliveries[(consumer, event_id)]
        delivery.status, delivery.attempts, delivery.next_at = "pending", 0, self.now
        delivery.defects_since_replay = 0

    # --- invariantes ---------------------------------------------------------------------------

    @invariant()
    def database_matches_the_model(self) -> None:
        if not self.model.events:
            return
        rows = {(r.consumer, r.event_id): r for r in self.env.run(self.env.deliveries())}
        expected = {key: (d.status, d.attempts) for key, d in self.model.deliveries.items()}
        assert {key: (r.status, r.attempts) for key, r in rows.items()} == expected
        letters: dict[tuple[str, uuid.UUID], int] = {}
        for letter in self.env.run(self.env.dead_letters()):
            key = (letter["consumer_name"], letter["event_id"])
            letters[key] = letters.get(key, 0) + 1
        assert letters == {
            key: d.dead_letters for key, d in self.model.deliveries.items() if d.dead_letters
        }
        for consumer in SCRIPTED_CONSUMERS:
            circuit = self.model.circuits[consumer]
            assert self.env.run(self.env.circuit(consumer)) == (circuit.state, circuit.changed_at)

    @invariant()
    def each_delivery_has_its_effect_once(self) -> None:
        echoes = self.env.run(self.env.echoes())
        for (consumer, event_id), delivery in self.model.deliveries.items():
            delivered = delivery.status == "delivered"
            handler = self.env.handlers[consumer]
            assert echoes[(consumer, event_id)] == (1 if delivered else 0)
            assert handler.completed()[event_id] == (1 if delivered else 0)
            assert handler.effects[event_id] <= 1

    def teardown(self) -> None:
        # Drenar: la dependencia vuelve; los guiones que queden se consumen.
        self.dependency_up()
        # Cada vuelta resuelve o avanza al menos la cabeza de cada partición abierta.
        for _ in range(len(self.model.events) * (MAX_ATTEMPTS + 2) + 10):
            if all(d.status not in _OPEN for d in self.model.deliveries.values()):
                break
            self.env.clock.advance(601)
            for consumer in SCRIPTED_CONSUMERS:
                self._dispatch(consumer)
        self.database_matches_the_model()
        self.each_delivery_has_its_effect_once()
        published = {(c, e.event_id) for e in self.model.events for c in SCRIPTED_CONSUMERS}
        rows = self.env.run(self.env.deliveries())
        resolved = {(r.consumer, r.event_id) for r in rows if r.status not in _OPEN}
        assert resolved == published
        for (_, _), delivery in self.model.deliveries.items():
            if delivery.status == "dead_letter":
                # Ninguna entrega llega a la cola muerta sin ocho defectos propios (nunca por
                # el circuito abierto).
                assert delivery.defects_since_replay == MAX_ATTEMPTS


def test_pr_nuc_30_the_outbox_matches_the_model(environment: DispatchEnvironment) -> None:
    """PR-NUC-30 y PR-NUC-33 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(OutboxMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
