"""FS-NUC-01 · Base inaccesible durante una escritura y una lectura (PR-NUC-41, PR-NUC-32,
NFR-NUC-37; PAT-NUC-RES-01 y 03).

**Inyección**: el contenedor de PostgreSQL del escenario se **pausa en mitad de cada operación**:
dentro de la transacción de una escritura de ``EscritorExpediente``, justo antes de una sentencia
elegida con la semilla (o del ``COMMIT``), y dentro de una lectura, entre el ``SET LOCAL`` y la
consulta. Los ajustes de la base son los de producción de ``vigia-api`` (conexión 5 s,
``statement_timeout`` 10 s, tope de comando 11 s, tope del intento 16 s).

**Resultado esperado**:

- escritura: ``TemporarilyUnavailable`` con ``retry_after_seconds = 5`` (en la interfaz,
  ``temporarily_unavailable`` y ``Retry-After: 5``) dentro del tope de comando; tras reanudar,
  **ningún registro parcial ni evento** (con la pausa en el ``COMMIT``, resultado desconocido: o
  todo o nada, nunca a medias);
- lectura: **un reintento** (dos intentos) y luego ``TemporarilyUnavailable``, dentro del tope de
  una lectura (32,1 s);
- ``/health/ready`` falla en menos de 2 s y ``/health/live`` responde 200; al reanudar, ``ready``
  vuelve a 200 y la misma base escribe sin intervención.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.api_support import World
from tests.outbox_support import PROBE_EVENT, StatementFaults, probe_payload
from tests.resilience.harness import WALL, Container, ScenarioRun, scenario, wait_until
from tests.resilience.stack import LedgerStack, ledger_stack
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    clips_of,
    order_document,
    organization_counts,
    unit_context,
    verify_ledger_chains,
)
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.shared.api.app import StartupSupervisor
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.api.health import READINESS_BUDGET_SECONDS
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.db import (
    ConnectionPort,
    Database,
    DatabaseSettings,
    PoolPort,
    ProcessKind,
    SslMode,
    TemporarilyUnavailable,
    Transaction,
)
from vigia_platform.shared.outbox.publish import NewEvent

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

API = DatabaseSettings(url="postgresql+asyncpg://x", process=ProcessKind.API)
"""Los topes de producción de ``vigia-api`` (solo se leen sus propiedades)."""
MARGIN_SECONDS = 3.0
"""Holgura de planificación sobre cada tope medido."""


@pytest.fixture(scope="module")
def stack() -> Iterator[LedgerStack]:
    with ledger_stack("fs_nuc_01") as stack:
        yield stack


async def _pause(container: Container) -> None:
    await asyncio.to_thread(container.pause)


@dataclass
class PausingDatabase:
    """La base del escritor con la pausa del contenedor inyectada en su próxima transacción.

    ``statement`` (desde 1) es la sentencia de la transacción antes de la que se pausa; ``None``
    pausa justo antes del ``COMMIT``. Las lecturas previas del escritor (idempotencia y zona) no
    se tocan: la pausa cae en mitad de la escritura.
    """

    database: Database
    container: Container
    statement: int | None = None
    statements: int = 0

    @property
    def process(self) -> ProcessKind:
        return self.database.process

    async def read(self, context: ScopeContext, statement: Any, parameters: Any = None) -> Any:
        return await self.database.read(context, statement, parameters)

    def transaction(self, context: ScopeContext) -> contextlib.AbstractAsyncContextManager[Any]:
        return self._transaction(context)

    @contextlib.asynccontextmanager
    async def _transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        async with self.database.transaction(context) as transaction:
            counter = StatementFaults.install(
                transaction,
                self.statement,
                (lambda: _pause(self.container)) if self.statement is not None else None,
            )
            yield transaction
            self.statements = counter.statements
            if self.statement is None:
                await _pause(self.container)


class _PausingConnection:
    """Conexión cuyo ``execute`` número ``pause_at`` pausa el contenedor antes de enviarse."""

    def __init__(self, connection: ConnectionPort, owner: _PausingPool) -> None:
        self._connection = connection
        self._owner = owner

    async def execute(self, statement: Any, parameters: Any) -> Any:
        self._owner.executed += 1
        if self._owner.executed == self._owner.pause_at:
            await _pause(self._owner.container)
        return await self._connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _PausingPool:
    def __init__(self, pool: PoolPort, container: Container) -> None:
        self._pool = pool
        self.container = container
        self.acquired = 0
        self.executed = 0
        self.pause_at: int | None = None

    async def acquire(self) -> ConnectionPort:
        self.acquired += 1
        return _PausingConnection(await self._pool.acquire(), self)  # type: ignore[return-value]

    async def dispose(self) -> None:
        await self._pool.dispose()


def _context(place: Place) -> ScopeContext:
    return unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)


async def _write(writer: EscritorExpediente, place: Place, document: dict[str, Any]) -> Any:
    """Un ``order_probe`` con su evento: registro, evidencia y evento en la misma transacción."""
    event = NewEvent(event_name=PROBE_EVENT, payload=probe_payload(zone_id=str(place.zone_id)))
    return await writer.write(_context(place), ORDER_TYPE, document, events=(event,))


def _resume(stack: LedgerStack) -> None:
    if stack.container.status() == "paused":
        stack.container.unpause()
    stack.container.wait_ready()


def _write_paused(stack: LedgerStack, run: ScenarioRun, statement: int | None) -> dict[str, Any]:
    """Una escritura con la pausa en ``statement``: lo que se lanzó, cuánto tardó y qué quedó."""
    env = stack.env
    place = Place.new()
    document = order_document(place, clips=1)
    for clip in clips_of(document):
        env.storage.put(clip)
    pausing = PausingDatabase(stack.database(), stack.container, statement)
    writer = stack.writer(pausing)
    started = WALL.monotonic()
    with pytest.raises(TemporarilyUnavailable) as raised:
        stack.run(_write(writer, place, document))
    elapsed = WALL.monotonic() - started
    _resume(stack)
    counts = stack.run(organization_counts(stack.migrated, place.organization_id))
    error = raised.value
    api = translate(error)
    return {
        "statement": "commit" if statement is None else statement,
        "seconds": round(elapsed, 2),
        "retry_after_seconds": error.retry_after_seconds,
        "commit_outcome_unknown": error.commit_outcome_unknown,
        "api_code": api.code.value,
        "api_retry_after_seconds": api.retry_after_seconds,
        "rows": {table: count for table, count in counts.items() if count},
    }


def test_fs_nuc_01_database_paused_during_a_write_and_a_read(stack: LedgerStack) -> None:
    with scenario(
        "FS-NUC-01",
        title="Base inaccesible durante una escritura y una lectura",
        injection="contenedor de PostgreSQL pausado en mitad de cada operación",
        expected=(
            "escritura: temporarily_unavailable con retry_after_seconds, ningún registro parcial"
            " ni evento; lectura: un reintento y luego temporarily_unavailable; ready falla,"
            " live no"
        ),
    ) as run:
        # Cuántas sentencias tiene la transacción de una escritura con un clip y un evento.
        env = stack.env
        probe_place = Place.new()
        probe_document = order_document(probe_place, clips=1)
        for clip in clips_of(probe_document):
            env.storage.put(clip)
        counting = PausingDatabase(stack.database(), stack.container, statement=10_000)
        receipt = stack.run(_write(stack.writer(counting), probe_place, probe_document))
        assert isinstance(receipt, Receipt), receipt
        total = counting.statements
        assert total >= 3, "la escritura de prueba tiene registro, evidencia y evento"

        # Escritura: pausa antes de una sentencia elegida con la semilla y antes del COMMIT.
        chosen = run.random.randint(1, total)
        writes = [_write_paused(stack, run, chosen), _write_paused(stack, run, None)]
        run.observe(write_statements=total, writes=writes)
        for write in writes:
            assert write["retry_after_seconds"] == 5
            assert write["api_code"] == ApiErrorCode.TEMPORARILY_UNAVAILABLE.value
            assert write["api_retry_after_seconds"] == 5
            assert write["seconds"] <= API.command_timeout_seconds + MARGIN_SECONDS
        # En mitad de la transacción: nada de nada (ni registro, ni evidencia, ni evento).
        assert writes[0]["rows"] == {}, writes[0]
        assert writes[0]["commit_outcome_unknown"] is False
        # En el COMMIT el resultado es desconocido para el cliente, pero nunca a medias.
        assert writes[1]["commit_outcome_unknown"] is True
        committed = writes[1]["rows"]
        assert committed == {} or {
            "ledger.ledger_record",
            "ledger.evidence",
            "shared.outbox_event",
        } <= set(committed), committed

        # Lectura: pausa entre el SET LOCAL y la consulta del primer intento.
        reading = stack.database()
        pool = _PausingPool(reading._pools[_person_pool(reading)], stack.container)
        reading._pools[_person_pool(reading)] = pool  # type: ignore[assignment]
        context = _context(probe_place)
        statement = text("SELECT count(*) FROM ledger.ledger_record")
        assert stack.run(reading.read(context, statement))  # conexión abierta y sana
        pool.acquired = pool.executed = 0
        pool.pause_at = 2  # 1 = SET LOCAL, 2 = la consulta
        started = WALL.monotonic()
        with pytest.raises(TemporarilyUnavailable) as read_error:
            stack.run(reading.read(context, statement))
        read_seconds = WALL.monotonic() - started
        _resume(stack)
        run.observe(
            read={
                "attempts": pool.acquired,
                "seconds": round(read_seconds, 2),
                "budget_seconds": API.read_timeout_seconds,
                "commit_outcome_unknown": read_error.value.commit_outcome_unknown,
            }
        )
        assert pool.acquired == 2, "una lectura se reintenta exactamente una vez"
        assert (
            API.attempt_timeout_seconds
            <= read_seconds
            <= API.read_timeout_seconds + (MARGIN_SECONDS)
        )

        # Salud: ready falla rápido y live responde mientras la base está pausada.
        health = _health(stack, run)
        run.observe(health=health)

        # Sin intervención: la misma base vuelve a escribir y las cadenas siguen íntegras.
        again = order_document(probe_place, clips=1)
        for clip in clips_of(again):
            env.storage.put(clip)
        after = stack.run(_write(stack.writer(counting), probe_place, again))
        assert isinstance(after, Receipt), after
        lengths = stack.run(verify_ledger_chains(stack.migrated, probe_place.organization_id))
        run.observe(chains_after={str(k): v for k, v in lengths.items()})


def _person_pool(database: Database) -> Any:
    (pool_class,) = [p for p in database._pools if p.value == "person"]
    return pool_class


def _health(stack: LedgerStack, run: ScenarioRun) -> dict[str, Any]:
    database = Database.create(
        DatabaseSettings(
            url=stack.migrated.as_role("vigia_app").sqlalchemy_url,
            process=ProcessKind.API,
            sslmode=SslMode.DISABLE,
        )
    )
    world = World()
    app = world.app(runtime={"database": database, "clock": WALL, "sleep": asyncio.sleep})
    with TestClient(app) as client:
        supervisor = app.state.vigia_readiness
        assert isinstance(supervisor, StartupSupervisor)
        wait_until(lambda: supervisor.started, timeout=30, message="la API no arrancó")
        assert client.get("/health/ready").status_code == 200
        stack.container.pause()
        try:
            probes = []
            for _ in range(3):  # sondas repetidas del balanceador
                started = WALL.monotonic()
                ready = client.get("/health/ready")
                ready_seconds = WALL.monotonic() - started
                live = client.get("/health/live")
                probes.append(
                    {
                        "ready_status": ready.status_code,
                        "ready_code": ready.json().get("code"),
                        "ready_seconds": round(ready_seconds, 3),
                        "live_status": live.status_code,
                    }
                )
        finally:
            _resume(stack)
        recovered = wait_until(
            lambda: client.get("/health/ready").status_code == 200,
            timeout=30,
            message="ready no volvió a 200 al reanudar la base",
        )
        assert client.portal is not None
        client.portal.call(database.dispose)
    for probe in probes:
        assert probe["ready_status"] == 503 and probe["ready_code"] == "temporarily_unavailable"
        assert probe["ready_seconds"] < READINESS_BUDGET_SECONDS
        assert probe["live_status"] == 200
    assert world.exits == []
    return {"probes": probes, "recovered": bool(recovered)}
