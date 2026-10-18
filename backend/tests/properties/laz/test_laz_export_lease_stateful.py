"""Arrendamiento de las exportaciones frente a un modelo (TASK-317; PAT-LAZ-RES-01; PBT-06; el
precedente es PR-NUC-42, ``tests/properties/test_leases_stateful.py``).

Para cualquier secuencia de ``acquire(worker, export)``, ``renew``, ``release``, ``finish``
(``ready`` o ``failed``, con la valla), ``crash(worker)`` y ``tick(t)`` sobre tres exportaciones
(dos de una organización y una de otra) y tres procesos:

- **en todo instante, como mucho un proceso tiene el arrendamiento vigente de una exportación**, y
  **como mucho una exportación en curso por organización** con arrendamiento vigente;
- ``acquire`` gana si y solo si la solicitud sigue abierta (``requested`` o ``in_progress``), nadie
  tiene un arrendamiento sin vencer (``lease_until < ahora``) y la organización no tiene otra en
  curso con el suyo vigente; cada adquisición sube ``attempt_count`` en uno; una solicitud
  terminada responde ``settled``;
- tras ``crash``, otro proceso solo la toma **después** de ``lease_until``; el primero ya no puede
  renovar ni cerrar (``renew`` solo alarga un arrendamiento propio sin vencer; ``finish`` lleva la
  misma valla); ``release`` solo libera uno propio.

El modelo es independiente del código. La máquina corre contra ``InMemoryExportLeaseStore`` y
contra ``SqlExportLeaseStore`` sobre PostgreSQL 16 real como ``vigia_app``; en la segunda, después
de cada paso, la fila de la base coincide con el modelo. El reloj es el del modelo. Los bordes
entran siempre: 0, 1, 19, 20, 59, 60 y 61 s, además de valores al azar.

Además, **concurrencia real** (dos pools, como dos procesos, lanzados a la vez): dos procesos sobre
la misma solicitud, o sobre dos solicitudes de la misma organización, y solo uno gana, en cada una
de 15 rondas. La mutación «adquisición no atómica» (leer la fila y escribir después sin condición
ni candado) cae en rojo aquí (Evidence del PR).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, Protocol

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
from tests.export_support import ExportStack, export_stack
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from vigia_platform.accreditation.export.composer_port import ExportPackage
from vigia_platform.accreditation.export.lease import (
    Acquisition,
    ExportLease,
    InMemoryExportLeaseStore,
    LeaseLost,
    SqlExportLeaseStore,
)
from vigia_platform.loop.domain.enums import ExportState
from vigia_platform.shared.signing.keys import to_millisecond

WORKERS: Final = ("w0", "w1", "w2")
EXPORTS: Final = ("a1", "a2", "b1")
"""Dos exportaciones de la organización ``a`` y una de ``b``."""
DURATION: Final = timedelta(seconds=60)
SQL_STEPS: Final = 20
MEMORY_STEPS: Final = 50
ROUNDS: Final = 15

OPEN: Final = frozenset({ExportState.REQUESTED, ExportState.IN_PROGRESS})

seconds = st.one_of(
    st.sampled_from([0, 1, 19, 20, 59, 60, 61]),
    st.integers(0, 150),
    st.integers(0, 2_000).map(lambda ms: ms / 1000),
)


class Runner(Protocol):
    def run(self, awaitable: Any) -> Any: ...


class Backend(Protocol):
    async def reset(self, now: datetime) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
        """Exportaciones nuevas: nombre → (organización, export_id)."""
        ...

    async def acquire(
        self, organization: uuid.UUID, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition: ...

    async def renew(self, lease: ExportLease, now: datetime) -> ExportLease | None: ...

    async def release(self, lease: ExportLease) -> bool: ...

    async def finish(self, lease: ExportLease, state: ExportState, now: datetime) -> bool: ...

    async def row(self, export_id: uuid.UUID) -> tuple[Any, ...]:
        """``(state, lease_owner, lease_until, attempt_count)``."""
        ...


# --- Modelo ----------------------------------------------------------------------------------


@dataclass
class ModelExport:
    organization: str
    state: ExportState = ExportState.REQUESTED
    owner: str | None = None
    until: datetime | None = None
    attempts: int = 0


@dataclass
class ModelWorker:
    owner: str
    held: dict[str, ExportLease] = field(default_factory=dict)
    """Lo que el proceso cree tener (su último ``acquire`` o ``renew`` confirmado)."""


# --- Adaptadores -----------------------------------------------------------------------------


class MemoryBackend:
    def __init__(self) -> None:
        self.store = InMemoryExportLeaseStore()

    async def reset(self, now: datetime) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
        self.store = InMemoryExportLeaseStore()
        organizations = {"a": uuid.uuid4(), "b": uuid.uuid4()}
        ids: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        for name in EXPORTS:
            organization = organizations[name[0]]
            export_id = uuid.uuid4()
            self.store.add(export_id, organization)
            ids[name] = (organization, export_id)
        return ids

    async def acquire(
        self, organization: uuid.UUID, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition:
        return await self.store.acquire(
            make_context(organization_id=organization), export_id, owner, now
        )

    async def renew(self, lease: ExportLease, now: datetime) -> ExportLease | None:
        return await self.store.renew(
            make_context(organization_id=lease.organization_id), lease, now
        )

    async def release(self, lease: ExportLease) -> bool:
        return await self.store.release(make_context(organization_id=lease.organization_id), lease)

    async def finish(self, lease: ExportLease, state: ExportState, now: datetime) -> bool:
        return await self.store.finish(lease, state, now)

    async def row(self, export_id: uuid.UUID) -> tuple[Any, ...]:
        row = self.store.rows[export_id]
        return (row.state.value, row.lease_owner, row.lease_until, row.attempt_count)


_PACKAGE: Final = ExportPackage(
    document_sha256="d" * 64,
    archive_sha256="a" * 64,
    manifest={"manifest_version": 1},
    size_bytes=1,
    artifact_count=1,
    verifier_version="1.0.0",
)


class SqlBackend:
    """``SqlExportLeaseStore`` como ``vigia_app``; las filas, sembradas como superusuario."""

    def __init__(self, stack: ExportStack) -> None:
        self.stack = stack
        self.store = stack.leases()
        self.sites = [stack.site(plants=1, zones=1), stack.site(plants=1, zones=1)]
        self.users = [stack.authz.add_user(site.organization_id) for site in self.sites]

    async def reset(self, now: datetime) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
        ids: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        for name in EXPORTS:
            index = 0 if name[0] == "a" else 1
            site, user = self.sites[index], self.users[index]
            export_id = uuid.uuid4()
            await _insert_request(self.stack, site.organization_id, site, user, export_id, now)
            ids[name] = (site.organization_id, export_id)
        return ids

    def _context(self, organization: uuid.UUID) -> Any:
        return self.stack.event_context(organization)

    async def acquire(
        self, organization: uuid.UUID, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition:
        return await self.store.acquire(self._context(organization), export_id, owner, now)

    async def renew(self, lease: ExportLease, now: datetime) -> ExportLease | None:
        return await self.store.renew(self._context(lease.organization_id), lease, now)

    async def release(self, lease: ExportLease) -> bool:
        return await self.store.release(self._context(lease.organization_id), lease)

    async def finish(self, lease: ExportLease, state: ExportState, now: datetime) -> bool:
        context = self._context(lease.organization_id)
        try:
            if state is ExportState.READY:
                await self.store.finish_ready(context, lease, _PACKAGE, now, now + DURATION)
            else:
                await self.store.finish_failed(
                    context, lease, "accreditation_coverage_unavailable", now
                )
        except LeaseLost:
            return False
        return True

    async def row(self, export_id: uuid.UUID) -> tuple[Any, ...]:
        row = await self.stack.export_row_now(export_id)
        return (row["state"], row["lease_owner"], row["lease_until"], row["attempt_count"])


async def _insert_request(
    stack: ExportStack,
    organization: uuid.UUID,
    site: Any,
    user: uuid.UUID,
    export_id: uuid.UUID,
    now: datetime,
) -> None:
    plant = next(iter(site.plants))
    await stack.authz.sessions.admin.execute(
        "INSERT INTO accreditation.export_request (export_id, organization_id, plant_id,"
        " zone_ids, period_from, period_to, requested_by, user_id, requested_at, package_form,"
        " expires_at, correlation_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,"
        " 'without_clips', $10, $11)",
        export_id,
        organization,
        plant,
        list(site.plants[plant]),
        now - timedelta(days=30),
        now,
        json.dumps({"user_id": str(user), "role_in_use": "coordinator_sst"}),
        user,
        now,
        now + timedelta(days=30),
        uuid.uuid4(),
    )


# --- Máquina ---------------------------------------------------------------------------------

_examples = itertools.count()


class ExportLeaseMachine(RuleBasedStateMachine):
    backend: ClassVar[Backend]
    loop: ClassVar[Runner]
    start: ClassVar[datetime]

    def __init__(self) -> None:
        super().__init__()
        self.now = type(self).start + timedelta(days=next(_examples))
        self.exports = {name: ModelExport(organization=name[0]) for name in EXPORTS}
        self.workers = {name: ModelWorker(self._owner(name)) for name in WORKERS}
        self.ids: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}

    def _owner(self, name: str) -> str:
        return f"{name}:{uuid.uuid4().hex[:8]}"

    def run(self, awaitable: Awaitable[Any]) -> Any:
        return type(self).loop.run(awaitable)

    @initialize()
    def reset(self) -> None:
        self.ids = self.run(self.backend.reset(self.now))

    # --- Reglas --------------------------------------------------------------------------

    @rule(worker=st.sampled_from(WORKERS), export=st.sampled_from(EXPORTS))
    def acquire(self, worker: str, export: str) -> None:
        state, model = self.workers[worker], self.exports[export]
        organization, export_id = self.ids[export]
        acquisition = self.run(self.backend.acquire(organization, export_id, state.owner, self.now))
        other_running = any(
            other is not model
            and other.organization == model.organization
            and other.state is ExportState.IN_PROGRESS
            and other.until is not None
            and other.until >= self.now
            for other in self.exports.values()
        )
        free = (
            model.state in OPEN
            and (model.until is None or model.until < self.now)
            and not other_running
        )
        assert (acquisition.lease is not None) == free, (export, worker, model, self.now)
        assert acquisition.settled == (model.state not in OPEN), (export, model)
        if acquisition.lease is None:
            return
        lease = acquisition.lease
        assert lease.until == self.now + DURATION
        assert lease.attempt_count == model.attempts + 1
        model.owner, model.until = state.owner, lease.until
        model.attempts += 1
        model.state = ExportState.IN_PROGRESS
        state.held[export] = lease

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data())
    def renew(self, data: st.DataObject) -> None:
        worker, export = self._pick_held(data)
        state, model = self.workers[worker], self.exports[export]
        renewed = self.run(self.backend.renew(state.held[export], self.now))
        allowed = model.owner == state.owner and model.until is not None and model.until >= self.now
        assert (renewed is not None) == allowed, (export, worker, model, self.now)
        if renewed is None:
            del state.held[export]  # sin renovación confirmada, deja de trabajar
            return
        assert renewed.until == self.now + DURATION
        model.until = renewed.until
        state.held[export] = renewed

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data())
    def release(self, data: st.DataObject) -> None:
        """Fallo no declarado del intento: libera; la solicitud sigue abierta."""
        worker, export = self._pick_held(data)
        state, model = self.workers[worker], self.exports[export]
        lease = state.held.pop(export)
        released = self.run(self.backend.release(lease))
        assert released == (model.owner == state.owner), (export, worker, model)
        if released:
            model.owner = model.until = None

    @precondition(lambda self: any(w.held for w in self.workers.values()))
    @rule(data=st.data(), ready=st.booleans())
    def finish(self, data: st.DataObject, ready: bool) -> None:
        worker, export = self._pick_held(data)
        state, model = self.workers[worker], self.exports[export]
        lease = state.held.pop(export)
        target = ExportState.READY if ready else ExportState.FAILED
        finished = self.run(self.backend.finish(lease, target, self.now))
        allowed = model.owner == state.owner and model.until is not None and model.until >= self.now
        assert finished == allowed, (export, worker, model, self.now)
        if finished:
            model.state, model.owner, model.until = target, None, None

    @rule(worker=st.sampled_from(WORKERS))
    def crash(self, worker: str) -> None:
        """Señal no capturable: el proceso no libera nada; otro proceso lo sustituye."""
        self.workers[worker] = ModelWorker(self._owner(worker))

    @rule(delta=seconds)
    def tick(self, delta: float) -> None:
        self.now = to_millisecond(self.now + timedelta(seconds=delta))

    def _pick_held(self, data: st.DataObject) -> tuple[str, str]:
        choices = sorted(
            (name, export) for name, state in self.workers.items() for export in state.held
        )
        picked: tuple[str, str] = data.draw(st.sampled_from(choices))
        return picked

    # --- Invariantes ---------------------------------------------------------------------

    @invariant()
    def at_most_one_valid_holder(self) -> None:
        for export in EXPORTS:
            working = [
                name
                for name, state in self.workers.items()
                if export in state.held and self.now <= state.held[export].until
            ]
            assert len(working) <= 1, (export, working, self.now)

    @invariant()
    def at_most_one_running_export_per_organization(self) -> None:
        for organization in ("a", "b"):
            running = [
                export
                for export in EXPORTS
                if export.startswith(organization)
                and any(
                    export in state.held and self.now <= state.held[export].until
                    for state in self.workers.values()
                )
            ]
            assert len(running) <= 1, (organization, running, self.now)

    @invariant()
    def store_matches_the_model(self) -> None:
        if not self.ids:
            return
        for export, model in self.exports.items():
            state, owner, until, attempts = self.run(self.backend.row(self.ids[export][1]))
            assert (state, owner, until, attempts) == (
                model.state.value,
                model.owner,
                model.until,
                model.attempts,
            ), export


def _run_machine(steps: int) -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(ExportLeaseMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=steps))


def test_export_leases_in_memory_match_the_model() -> None:
    loop = DatabaseLoop()
    try:
        ExportLeaseMachine.backend = MemoryBackend()
        ExportLeaseMachine.loop = loop
        ExportLeaseMachine.start = datetime(2026, 10, 1, tzinfo=UTC)
        _run_machine(MEMORY_STEPS)
    finally:
        loop.close()


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_leases") as built:
        yield built


@pytest.mark.integration
def test_export_leases_in_postgresql_match_the_model(stack: ExportStack) -> None:
    ExportLeaseMachine.backend = SqlBackend(stack)
    ExportLeaseMachine.loop = stack.authz.sessions
    ExportLeaseMachine.start = stack.now()
    _run_machine(SQL_STEPS)


# --- Concurrencia real -----------------------------------------------------------------------


def _contenders(stack: ExportStack) -> tuple[SqlExportLeaseStore, SqlExportLeaseStore]:
    return stack.leases(stack.new_database()), stack.leases(stack.new_database())


@pytest.mark.integration
def test_two_processes_on_the_same_request_only_one_wins(stack: ExportStack) -> None:
    site = stack.site(plants=1, zones=1)
    user = stack.authz.add_user(site.organization_id)
    first, second = _contenders(stack)
    context = stack.event_context(site.organization_id)

    async def scenario() -> list[int]:
        winners: list[int] = []
        for _ in range(ROUNDS):
            export_id = uuid.uuid4()
            now = stack.now()
            await _insert_request(stack, site.organization_id, site, user, export_id, now)
            results = await asyncio.gather(
                first.acquire(context, export_id, "p1", now),
                second.acquire(context, export_id, "p2", now),
            )
            winners.append(sum(result.lease is not None for result in results))
            row = await stack.export_row_now(export_id)
            assert row["attempt_count"] == 1
            # Se cierra para que la siguiente ronda de la organización no espere.
            (lease,) = [result.lease for result in results if result.lease is not None]
            await first.finish_failed(context, lease, "accreditation_coverage_unavailable", now)
            stack.tick(1)
        return winners

    assert stack.run(scenario()) == [1] * ROUNDS


@pytest.mark.integration
def test_two_requests_of_one_organization_never_run_at_once(stack: ExportStack) -> None:
    site = stack.site(plants=1, zones=1)
    user = stack.authz.add_user(site.organization_id)
    first, second = _contenders(stack)
    context = stack.event_context(site.organization_id)

    async def scenario() -> list[int]:
        winners: list[int] = []
        for _ in range(ROUNDS):
            ids = (uuid.uuid4(), uuid.uuid4())
            now = stack.now()
            for export_id in ids:
                await _insert_request(stack, site.organization_id, site, user, export_id, now)
            results = await asyncio.gather(
                first.acquire(context, ids[0], "p1", now),
                second.acquire(context, ids[1], "p2", now),
            )
            winners.append(sum(result.lease is not None for result in results))
            for result in results:
                if result.lease is not None:
                    await first.finish_failed(
                        context, result.lease, "accreditation_coverage_unavailable", now
                    )
            # La perdedora queda abierta: se cierra como superusuario para la ronda siguiente.
            for export_id in ids:
                await stack.authz.sessions.admin.execute(
                    "UPDATE accreditation.export_request SET state = 'failed',"
                    " failure_code = 'accreditation_coverage_unavailable'"
                    " WHERE export_id = $1 AND state <> 'failed'",
                    export_id,
                )
            stack.tick(1)
        return winners

    assert stack.run(scenario()) == [1] * ROUNDS
