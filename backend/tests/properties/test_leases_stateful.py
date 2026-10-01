"""PR-NUC-42: arrendamientos de tareas periódicas frente a un modelo (TASK-130; PBT-06; FS-NUC-08).

Para cualquier secuencia de ``acquire(worker, t)``, ``renew``, ``release``, ``advance``,
``crash(worker)`` y ``tick(t)`` sobre dos tareas y tres procesos:

- **en todo instante, como mucho un proceso tiene el arrendamiento vigente de una tarea**: un
  proceso trabaja mientras el arrendamiento que conoce (el de su último ``acquire`` o ``renew``
  confirmado) no ha vencido, y nunca hay dos así para la misma tarea (ninguna tarea corre dos
  veces solapada);
- **tras ``crash``, otro solo la adquiere después de ``lease_until``**: ``acquire`` gana si y solo
  si la tarea vence y nadie tiene un arrendamiento sin vencer (``lease_until < ahora``);
- ``renew`` solo alarga un arrendamiento propio **sin vencer**; ``release`` solo libera uno propio;
- ``advance`` (la valla de cada organización) solo avanza con el arrendamiento vigente y en la
  misma ejecución; una retoma de la misma ejecución continúa por la organización siguiente a la
  última confirmada (``resume_after``), y una ejecución nueva empieza de cero.

El modelo es independiente del código (unas pocas líneas por regla). La máquina corre contra
``InMemoryLeaseStore`` y contra ``SqlLeaseStore`` sobre PostgreSQL 16 real como ``vigia_app``;
en la segunda, después de cada paso, la fila de la base coincide con el modelo. El reloj es el del
modelo: ningún paso depende de la hora del sistema.

Los bordes del reloj entran siempre: 0, 1, 19, 20, 59, 60 y 61 s (la vigencia es de 60 s y la
renovación de 20 s), además de valores al azar.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol

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
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from tests.worker_support import START, WorkerEnvironment, worker_environment
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.worker.leases import (
    InMemoryLeaseStore,
    Lease,
    LeaseSettings,
    SqlLeaseStore,
    TaskOutcome,
)

TASKS = ("alpha", "beta")
WORKERS = ("w0", "w1", "w2")
DURATION = timedelta(seconds=60)
SQL_STEPS = 20
"""Cada paso son una o dos transacciones reales y la comparación con la fila de la base."""
MEMORY_STEPS = 50

seconds = st.one_of(
    st.sampled_from([0, 1, 19, 20, 59, 60, 61]),
    st.integers(0, 150),
    st.integers(0, 2_000).map(lambda ms: ms / 1000),
)


class Runner(Protocol):
    def run(self, awaitable: Any) -> Any: ...


class Backend(Protocol):
    async def acquire(self, task: str, owner: str, now: datetime) -> Lease | None: ...

    async def renew(self, lease: Lease, now: datetime) -> Lease | None: ...

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool: ...

    async def advance(
        self, lease: Lease, organization_id: uuid.UUID, now: datetime
    ) -> Lease | None: ...

    async def reset(self, now: datetime) -> None: ...

    async def row(self, task: str) -> tuple[Any, ...]: ...


# --- Modelo ----------------------------------------------------------------------------------


@dataclass
class ModelTask:
    next_run_at: datetime
    owner: str | None = None
    until: datetime | None = None
    run_at: datetime | None = None
    cursor: uuid.UUID | None = None


@dataclass
class ModelWorker:
    owner: str
    held: dict[str, Lease] = field(default_factory=dict)
    """Lo que el proceso cree tener (lo que devolvió su último ``acquire`` o ``renew``)."""


# --- Adaptadores -----------------------------------------------------------------------------


class MemoryBackend:
    def __init__(self) -> None:
        self.store = InMemoryLeaseStore(LeaseSettings())

    async def reset(self, now: datetime) -> None:
        self.store = InMemoryLeaseStore(LeaseSettings())
        for task in TASKS:
            self.store.add_task(task, now)

    async def acquire(self, task: str, owner: str, now: datetime) -> Lease | None:
        return await self.store.acquire(task, owner, now)

    async def renew(self, lease: Lease, now: datetime) -> Lease | None:
        return await self.store.renew(lease, now)

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool:
        return await self.store.release(lease, now=now, next_run_at=next_run_at, outcome=outcome)

    async def advance(
        self, lease: Lease, organization_id: uuid.UUID, now: datetime
    ) -> Lease | None:
        return await self.store.advance(lease, organization_id, failed=False, now=now)

    async def row(self, task: str) -> tuple[Any, ...]:
        row = self.store.rows[task]
        return (
            row.next_run_at,
            row.lease_owner,
            row.lease_until,
            row.progress_run_at,
            row.progress_organization_id,
        )


class SqlBackend:
    def __init__(self, env: WorkerEnvironment) -> None:
        self.env = env
        self.store = SqlLeaseStore(database=env.database, contexts=env.contexts)

    async def reset(self, now: datetime) -> None:
        for task in TASKS:
            await self.env.put_task(task, now)

    async def acquire(self, task: str, owner: str, now: datetime) -> Lease | None:
        return await self.store.acquire(task, owner, now)

    async def renew(self, lease: Lease, now: datetime) -> Lease | None:
        return await self.store.renew(lease, now)

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool:
        return await self.store.release(lease, now=now, next_run_at=next_run_at, outcome=outcome)

    async def advance(
        self, lease: Lease, organization_id: uuid.UUID, now: datetime
    ) -> Lease | None:
        # La valla corre en la transacción de una organización (la de la iteración).
        task = _Task(lease.task_name)
        context = self.env.contexts.context_for_organization(task, organization_id)
        async with self.env.database.transaction(context) as transaction:
            return await self.store.advance(
                transaction, lease, organization_id, failed=False, now=now
            )

    async def row(self, task: str) -> tuple[Any, ...]:
        row = await self.env.task_row(task)
        return (
            row.next_run_at,
            row.lease_owner,
            row.lease_until,
            row.progress_run_at,
            row.progress_organization_id,
        )


@dataclass(frozen=True)
class _Task:
    """Lo que ``context_for_organization`` lee de una tarea."""

    task_name: str
    unit: ActorUnit = ActorUnit.U02


# --- Máquina ---------------------------------------------------------------------------------

_examples = itertools.count()


class LeaseMachine(RuleBasedStateMachine):
    backend: ClassVar[Backend]
    loop: ClassVar[Runner]

    def __init__(self) -> None:
        super().__init__()
        self.now = START + timedelta(days=next(_examples))
        self.tasks = {task: ModelTask(next_run_at=self.now) for task in TASKS}
        self.workers = {name: ModelWorker(self._owner(name)) for name in WORKERS}
        self.generation = 0

    def _owner(self, name: str) -> str:
        return f"{name}:{uuid.uuid4().hex[:8]}"

    def run(self, awaitable: Awaitable[Any]) -> Any:
        return type(self).loop.run(awaitable)

    @initialize()
    def reset(self) -> None:
        self.run(self.backend.reset(self.now))

    # --- Reglas --------------------------------------------------------------------------

    @rule(worker=st.sampled_from(WORKERS), task=st.sampled_from(TASKS))
    def acquire(self, worker: str, task: str) -> None:
        state, model = self.workers[worker], self.tasks[task]
        lease = self.run(self.backend.acquire(task, state.owner, self.now))
        free = model.next_run_at <= self.now and (model.until is None or model.until < self.now)
        assert (lease is not None) == free, (task, worker, model, self.now)
        if lease is None:
            return
        same_run = model.run_at == model.next_run_at
        expected_cursor = model.cursor if same_run else None
        assert lease.run_at == model.next_run_at
        assert lease.until == self.now + DURATION
        assert lease.resume_after == expected_cursor
        model.owner, model.until = state.owner, lease.until
        model.run_at, model.cursor = model.next_run_at, expected_cursor
        state.held[task] = lease

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data())
    def renew(self, data: st.DataObject) -> None:
        worker, task = self._pick_held(data)
        state, model = self.workers[worker], self.tasks[task]
        renewed = self.run(self.backend.renew(state.held[task], self.now))
        allowed = model.owner == state.owner and model.until is not None and model.until >= self.now
        assert (renewed is not None) == allowed, (task, worker, model, self.now)
        if renewed is None:
            # Sin renovación confirmada el proceso deja de trabajar en la tarea.
            del state.held[task]
            return
        assert renewed.until == self.now + DURATION
        model.until = renewed.until
        state.held[task] = renewed

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data(), finished=st.booleans())
    def release(self, data: st.DataObject, finished: bool) -> None:
        worker, task = self._pick_held(data)
        state, model = self.workers[worker], self.tasks[task]
        lease = state.held.pop(task)
        next_run_at = self.now + timedelta(seconds=30) if finished else lease.run_at
        outcome = TaskOutcome.SUCCEEDED if finished else TaskOutcome.INTERRUPTED
        released = self.run(
            self.backend.release(lease, now=self.now, next_run_at=next_run_at, outcome=outcome)
        )
        assert released == (model.owner == state.owner), (task, worker, model)
        if released:
            model.owner = model.until = None
            model.next_run_at = next_run_at

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data())
    def advance(self, data: st.DataObject) -> None:
        worker, task = self._pick_held(data)
        state, model = self.workers[worker], self.tasks[task]
        lease = state.held[task]
        organization_id = uuid.uuid4()
        advanced = self.run(self.backend.advance(lease, organization_id, self.now))
        allowed = (
            model.owner == state.owner
            and model.until is not None
            and model.until >= self.now
            and model.run_at == lease.run_at
        )
        assert (advanced is not None) == allowed, (task, worker, model, self.now)
        if advanced is not None:
            assert advanced.resume_after == organization_id
            model.cursor = organization_id
            state.held[task] = advanced

    @rule(worker=st.sampled_from(WORKERS))
    def crash(self, worker: str) -> None:
        """Señal no capturable: el proceso no libera nada; otro proceso lo sustituye."""
        self.workers[worker] = ModelWorker(self._owner(worker))

    @rule(delta=seconds)
    def tick(self, delta: float) -> None:
        self.now += timedelta(seconds=delta)

    def _pick_held(self, data: st.DataObject) -> tuple[str, str]:
        choices = sorted(
            (name, task) for name, state in self.workers.items() for task in state.held
        )
        picked: tuple[str, str] = data.draw(st.sampled_from(choices))
        return picked

    # --- Invariantes ---------------------------------------------------------------------

    @invariant()
    def at_most_one_valid_holder(self) -> None:
        """Ninguna tarea tiene dos procesos con un arrendamiento vigente a la vez."""
        for task in TASKS:
            working = [
                name
                for name, state in self.workers.items()
                if task in state.held and self.now <= state.held[task].until
            ]
            assert len(working) <= 1, (task, working, self.now)

    @invariant()
    def store_matches_the_model(self) -> None:
        """La fila (en memoria o en la base) es la del modelo después de cada paso."""
        for task, model in self.tasks.items():
            next_run_at, owner, until, run_at, cursor = self.run(self.backend.row(task))
            assert (next_run_at, owner, until) == (model.next_run_at, model.owner, model.until)
            assert run_at == model.run_at
            assert cursor == model.cursor


def _run_machine(steps: int) -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(LeaseMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=steps))


def test_pr_nuc_42_in_memory_leases_match_the_model() -> None:
    """PR-NUC-42 contra ``InMemoryLeaseStore`` (sin base) con cada semilla del perfil."""
    loop = DatabaseLoop()
    try:
        LeaseMachine.backend = MemoryBackend()
        LeaseMachine.loop = loop
        _run_machine(MEMORY_STEPS)
    finally:
        loop.close()


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "leases_stateful") as env:
        yield env


@pytest.mark.integration
def test_pr_nuc_42_sql_leases_match_the_model(environment: WorkerEnvironment) -> None:
    """PR-NUC-42 contra ``SqlLeaseStore`` en PostgreSQL 16 como ``vigia_app``."""
    LeaseMachine.backend = SqlBackend(environment)
    LeaseMachine.loop = environment
    _run_machine(SQL_STEPS)
