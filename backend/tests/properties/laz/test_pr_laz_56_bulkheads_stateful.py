"""PR-LAZ-56: mamparos por consumidor de U-04 frente a un modelo (TASK-349; LC-LAZ-40; PBT-06).

``RuleBasedStateMachine`` sobre el **despachador real** (``shared.outbox.dispatcher``) con los
**seis** consumidores de U-04 registrados por ``BulkheadedConsumerRegistry`` (el envoltorio que
aplica ``registration.py``: los manejadores no conocen el mamparo) y una base en memoria que
emula lo que el despachador ve: cabezas vencidas por partición (las de una partición bloqueada no
se ofrecen), bloqueo consultivo por (consumidor, partición) hasta el final de la transacción,
intentos y reintento con su ``next_attempt_at``.

Reglas generadas: rondas de cualquier consumidor (varias a la vez, como varios bucles o procesos),
fin de una entrega en curso con éxito o con defecto del manejador, y avance del reloj (vencen los
reintentos). Cada manejador espera a un ``asyncio.Event`` de la prueba: nada de ``sleep`` ni de
topes de pared (retros 14 y 15); el reloj es un ``SimulatedClock``.

Invariantes, tras cada paso:

- **ningún consumidor supera su concurrencia declarada** y la ocupación coincide con el modelo (y
  con ``loop_bulkhead_in_use``);
- una ronda en el tope termina ``saturated`` **sin abrir transacción ni tomar el bloqueo** de la
  partición, y **el contador de intentos no cambia** (solo lo mueve un defecto del manejador);
- uno saturado **no bloquea a los demás**: la ronda de otro consumidor con puesto libre y una
  partición libre queda en curso;
- ``loop_bulkhead_saturated_total`` cuenta exactamente las rondas saturadas.

Tamaños: los del diseño (4/4/2/2/1/2) y otros pequeños generados, para que la saturación aparezca
en pocas rondas. Semilla registrada: perfil ``ci`` con la semilla fija y la de la sesión.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final

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
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.conftest import _seeds_for_profile
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics
from vigia_platform.loop.worker.bulkheads import (
    DEFAULT_CONCURRENCY,
    BulkheadedConsumerRegistry,
    ConsumerBulkheads,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import METER_NAME, PlatformMetrics
from vigia_platform.shared.outbox import dispatcher as dispatcher_module
from vigia_platform.shared.outbox.dispatcher import Dispatcher, DispatchReport, Outcome
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry

CONSUMERS: Final = tuple(sorted(DEFAULT_CONCURRENCY))
START: Final = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
ORGANIZATION: Final = uuid.UUID("0192a6f0-0000-7000-8000-000000000001")
PARTITIONS: Final = 5
"""Particiones por consumidor: más que el mayor tamaño, para que el tope llegue antes que la
falta de particiones libres."""
DEPTH: Final = 2
"""Entregas por partición al empezar."""
RETRY_DUE: Final = timedelta(hours=2)
"""Más que el mayor retraso de los primeros reintentos de ``RetryPolicy``."""

SIZES: Final = st.one_of(
    st.just(dict(DEFAULT_CONCURRENCY)),
    st.fixed_dictionaries({name: st.integers(min_value=1, max_value=3) for name in CONSUMERS}),
)


class HandlerDefect(Exception):
    """Defecto guionizado del manejador: consume un intento."""


# --- Base en memoria -----------------------------------------------------------------------------


@dataclass
class Delivery:
    event_id: uuid.UUID
    created_at: datetime
    attempts: int = 0
    next_attempt_at: datetime = START
    status: str = "pending"


@dataclass
class FakeDatabase:
    """Lo que el despachador lee y escribe, por la identidad de cada sentencia."""

    clock: SimulatedClock
    queues: dict[tuple[str, str], list[Delivery]] = field(default_factory=dict)
    locks: set[tuple[str, str]] = field(default_factory=set)
    lock_attempts: dict[str, int] = field(default_factory=dict)
    data_transactions: dict[str, int] = field(default_factory=dict)

    def head(self, consumer: str, partition: str) -> Delivery | None:
        pending = [d for d in self.queues[(consumer, partition)] if d.status != "delivered"]
        return pending[0] if pending else None

    def due(self, consumer: str, now: datetime) -> list[str]:
        partitions = sorted(p for c, p in self.queues if c == consumer)
        return [
            p
            for p in partitions
            if (consumer, p) not in self.locks
            and (head := self.head(consumer, p)) is not None
            and head.next_attempt_at <= now
        ]

    @contextlib.asynccontextmanager
    async def transaction(self, context: Any) -> AsyncIterator[FakeTransaction]:
        tx = FakeTransaction(self, context)
        if context is not PROVIDER_CONTEXT:
            name = context.consumer
            self.data_transactions[name] = self.data_transactions.get(name, 0) + 1
        try:
            yield tx
        finally:
            self.locks -= tx.held


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def one(self) -> Any:
        assert len(self._rows) == 1
        return self._rows[0]

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        return self.one()


PROVIDER_CONTEXT: Final = SimpleNamespace(kind="provider")


class FakeTransaction:
    def __init__(self, database: FakeDatabase, context: Any) -> None:
        self.database = database
        self.context = context
        self.held: set[tuple[str, str]] = set()
        self.failed = False

    @contextlib.asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        yield

    async def execute(self, statement: Any, params: Mapping[str, Any]) -> _Result:
        db = self.database
        d = dispatcher_module
        if statement is d._READ_BREAKER:
            return _Result(
                [
                    SimpleNamespace(
                        circuit_state="closed", circuit_opened_at=None, probe_interval_seconds=60
                    )
                ]
            )
        if statement is d._DUE_HEADS:
            consumer = params["consumer"]
            due = db.due(consumer, params["due_at"])[: params["max_heads"]]
            return _Result(
                [
                    SimpleNamespace(
                        organization_id=ORGANIZATION,
                        partition_key=p,
                        event_id=head.event_id,
                        correlation_id=head.event_id,
                    )
                    for p in due
                    if (head := db.head(consumer, p)) is not None
                ]
            )
        if statement is d._PARTITION_LOCK:
            key = (params["consumer"], params["partition"])
            db.lock_attempts[key[0]] = db.lock_attempts.get(key[0], 0) + 1
            if key in db.locks:
                return _Result([False])
            db.locks.add(key)
            self.held.add(key)
            return _Result([True])
        if statement is d._LOCK_HEAD:
            consumer, partition = params["consumer"], params["partition"]
            head = db.head(consumer, partition)
            if head is None:
                return _Result([])
            return _Result(
                [
                    SimpleNamespace(
                        event_id=head.event_id,
                        attempts=head.attempts,
                        next_attempt_at=head.next_attempt_at,
                        organization_id=ORGANIZATION,
                        plant_id=None,
                        event_name="loop_probe",
                        partition_key=partition,
                        ledger_sequence=None,
                        payload={},
                        correlation_id=head.event_id,
                        created_at=head.created_at,
                        trace_id=None,
                        span_id=None,
                    )
                ]
            )
        delivery = self._delivery(params)
        if statement is d._MARK_DELIVERED:
            delivery.status = "delivered"
        elif statement is d._MARK_RETRYING:
            delivery.status = "retrying"
            delivery.attempts = params["attempts"]
            delivery.next_attempt_at = params["next_attempt_at"]
        else:  # pragma: no cover - cola muerta: el modelo nunca llega a ocho intentos
            raise AssertionError(f"sentencia inesperada: {statement}")
        return _Result([])

    def _delivery(self, params: Mapping[str, Any]) -> Delivery:
        for (consumer, _), queue in self.database.queues.items():
            if consumer != params["consumer"]:
                continue
            for delivery in queue:
                if delivery.event_id == params["event_id"]:
                    return delivery
        raise AssertionError("entrega desconocida")


class FakeContexts:
    def context_from_event(self, event: Any, *, unit: ActorUnit = ActorUnit.U02) -> Any:
        return SimpleNamespace(kind="event", consumer=CURRENT_CONSUMER.get())

    def provider_audit_context(self) -> Any:
        return PROVIDER_CONTEXT


CURRENT_CONSUMER: Final = contextvars.ContextVar("consumer", default="?")
"""El consumidor de la ronda en curso (cada ronda es su propia tarea): la base cuenta por él las
transacciones con contexto de evento."""


# --- Manejadores guionizados ---------------------------------------------------------------------


@dataclass
class InFlight:
    consumer: str
    event_id: uuid.UUID
    gate: asyncio.Event
    defect: bool = False


def handler_for(name: str, inflight: dict[uuid.UUID, InFlight]) -> Any:
    async def handle(event: OutboxEvent, tx: Any) -> None:
        entry = InFlight(name, event.event_id, asyncio.Event())
        inflight[event.event_id] = entry
        try:
            await entry.gate.wait()
        finally:
            del inflight[event.event_id]
        if entry.defect:
            raise HandlerDefect

    return handle


# --- La máquina ----------------------------------------------------------------------------------


@dataclass
class Model:
    sizes: dict[str, int]
    in_flight: dict[str, int]
    saturated: dict[str, int]
    admitted: dict[str, int]
    attempts: dict[uuid.UUID, int]


class BulkheadMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.clock = SimulatedClock(START)
        self.database = FakeDatabase(self.clock)
        self.inflight: dict[uuid.UUID, InFlight] = {}
        self.rounds: list[asyncio.Task[DispatchReport]] = []
        self.model = Model({}, {}, {}, {}, {})
        self.bulkheads: ConsumerBulkheads | None = None
        self.dispatcher: Dispatcher | None = None
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])

    # --- Utilidades -----------------------------------------------------------------------------

    def _settle(self) -> None:
        async def spin() -> None:
            for _ in range(50):
                await asyncio.sleep(0)

        self.loop.run_until_complete(spin())

    def _metric(self, name: LoopMetricName, consumer: str) -> float | None:
        data = self.reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != name.value:
                        continue
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        if dict(point.attributes or {}) == {"consumer": consumer}:
                            return float(point.value)
        return None

    def _attempts(self) -> dict[uuid.UUID, int]:
        return {d.event_id: d.attempts for queue in self.database.queues.values() for d in queue}

    # --- Reglas ---------------------------------------------------------------------------------

    @initialize(sizes=SIZES)
    def start(self, sizes: dict[str, int]) -> None:
        loop_metrics = LoopMetrics(self.provider.get_meter(METER_NAME))
        self.bulkheads = ConsumerBulkheads(sizes, loop_metrics)
        registry = ConsumerRegistry()
        bulkheaded = BulkheadedConsumerRegistry(registry, self.bulkheads)
        for number, name in enumerate(CONSUMERS):
            # Como un registrador de U-04: un ``Consumer`` sin mamparo, que el registro aplica.
            bulkheaded.register(
                Consumer(
                    consumer_name=name,
                    unit=ActorUnit.U04,
                    subscribed_events=("loop_probe",),
                    handler=handler_for(name, self.inflight),
                )
            )
            for index in range(PARTITIONS):
                # Identificadores fijos: la generación de Hypothesis no puede depender del azar.
                self.database.queues[(name, f"p{index}")] = [
                    Delivery(
                        uuid.UUID(int=(number << 16) | (index << 8) | n),
                        START + timedelta(milliseconds=n),
                    )
                    for n in range(DEPTH)
                ]
        catalog = SimpleNamespace(
            sealed=True,
            consumers=registry,
            event_types=SimpleNamespace(event_names=lambda: ("loop_probe",)),
        )
        self.dispatcher = Dispatcher(
            database=self.database,  # type: ignore[arg-type]
            catalog=catalog,  # type: ignore[arg-type]
            outbox=SimpleNamespace(),  # type: ignore[arg-type]
            contexts=FakeContexts(),
            clock=self.clock,
            metrics=PlatformMetrics(MeterProvider().get_meter("pruebas")),
            batch_size=1,
        )
        self.model = Model(
            sizes=dict(sizes),
            in_flight=dict.fromkeys(CONSUMERS, 0),
            saturated=dict.fromkeys(CONSUMERS, 0),
            admitted=dict.fromkeys(CONSUMERS, 0),
            attempts=self._attempts(),
        )

    async def _round(self, name: str) -> DispatchReport:
        assert self.dispatcher is not None
        CURRENT_CONSUMER.set(name)
        return await self.dispatcher.dispatch_once(name)

    @rule(name=st.sampled_from(CONSUMERS))
    def dispatch(self, name: str) -> None:
        model = self.model
        attempts_before = self._attempts()
        locks_before = self.database.lock_attempts.get(name, 0)
        free = self.database.due(name, self.clock.now())
        task = self.loop.create_task(self._round(name))
        self.rounds.append(task)
        self._settle()
        if model.in_flight[name] >= model.sizes[name]:
            if free:
                # En el tope: termina sin transacción ni bloqueo y sin tocar los intentos.
                assert task.done()
                assert task.result().outcomes == {Outcome.SATURATED: 1}
                model.saturated[name] += 1
            else:
                assert task.done()
                assert task.result().outcomes == {}
            assert self.database.lock_attempts.get(name, 0) == locks_before
        elif free:
            # Con puesto y partición libre: queda en curso, aunque otros estén saturados.
            assert not task.done()
            model.in_flight[name] += 1
            model.admitted[name] += 1
            assert self.database.lock_attempts.get(name, 0) == locks_before + 1
        else:
            assert task.done()
            assert task.result().outcomes == {}
        assert self._attempts() == attempts_before

    @precondition(lambda self: bool(self.inflight))
    @rule(data=st.data(), defect=st.booleans())
    def finish(self, data: st.DataObject, defect: bool) -> None:
        event_id = data.draw(st.sampled_from(sorted(self.inflight)))
        entry = self.inflight[event_id]
        entry.defect = defect
        entry.gate.set()
        self._settle()
        assert event_id not in self.inflight
        self.model.in_flight[entry.consumer] -= 1
        if defect:
            self.model.attempts[event_id] += 1

    @rule()
    def advance(self) -> None:
        self.clock.advance(RETRY_DUE.total_seconds())

    # --- Invariantes ----------------------------------------------------------------------------

    @invariant()
    def no_consumer_exceeds_its_declared_concurrency(self) -> None:
        if self.bulkheads is None:
            return
        for name in CONSUMERS:
            in_use = self.bulkheads.in_use(name)
            assert in_use == self.model.in_flight[name] <= self.model.sizes[name], name
            running = sum(1 for entry in self.inflight.values() if entry.consumer == name)
            assert running == in_use, name

    @invariant()
    def attempts_move_only_with_handler_defects(self) -> None:
        if self.bulkheads is None:
            return
        assert self._attempts() == self.model.attempts

    @invariant()
    def only_admitted_rounds_take_a_partition_lock_or_open_a_transaction(self) -> None:
        if self.bulkheads is None:
            return
        for name in CONSUMERS:
            assert self.database.lock_attempts.get(name, 0) == self.model.admitted[name]
            assert self.database.data_transactions.get(name, 0) == self.model.admitted[name]

    @invariant()
    def the_published_metrics_are_the_real_ones(self) -> None:
        if self.bulkheads is None:
            return
        for name in CONSUMERS:
            assert self._metric(LoopMetricName.LOOP_BULKHEAD_IN_USE, name) == float(
                self.model.in_flight[name]
            )
            assert self._metric(LoopMetricName.LOOP_BULKHEAD_SIZE, name) == float(
                self.model.sizes[name]
            )
            saturated = self._metric(LoopMetricName.LOOP_BULKHEAD_SATURATED_TOTAL, name)
            assert (saturated or 0.0) == float(self.model.saturated[name])

    def teardown(self) -> None:
        for entry in list(self.inflight.values()):
            entry.gate.set()
        self._settle()
        for task in self.rounds:
            if not task.done():
                task.cancel()
        self._settle()
        self.loop.close()
        self.provider.shutdown()


def test_pr_laz_56_bulkheads_match_the_model() -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(BulkheadMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=50))


def test_a_saturated_consumer_does_not_block_another_concurrent_rounds() -> None:
    """Ejemplo concurrente: con ``export_worker`` (1) ocupado, cuatro rondas a la vez del
    ``notification_engine`` entran las cuatro, y una quinta queda saturada sin intento."""
    machine = BulkheadMachine()
    try:
        machine.start(dict(DEFAULT_CONCURRENCY))
        machine.dispatch("export_worker")
        machine.dispatch("export_worker")
        assert machine.model.saturated["export_worker"] == 1
        tasks = [machine.loop.create_task(machine._round("notification_engine")) for _ in range(5)]
        machine.rounds.extend(tasks)
        machine._settle()
        outcomes = [task.result().outcomes for task in tasks if task.done()]
        assert outcomes == [{Outcome.SATURATED: 1}]
        assert machine.bulkheads is not None
        assert machine.bulkheads.in_use("notification_engine") == 4
        assert machine.bulkheads.in_use("export_worker") == 1
        assert all(d.attempts == 0 for q in machine.database.queues.values() for d in q)
    finally:
        machine.teardown()
