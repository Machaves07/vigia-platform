"""Garantías concurrentes del latido contra PostgreSQL 16 real y dos instancias (TASK-223).

**Una sola vez por ``heartbeat_id``** (NFR-GOB-15, 47; BR-GOB-71): el mismo latido enviado 6 veces
a la vez, repartido entre **dos instancias** (cada una con su ``Database``, sus servicios, su
``NodeApiGate`` y su aplicación; nada compartido en memoria), produce una fila de historia, una
escritura de la proyección y como mucho un ``node_communication_state_changed``, y las 6 respuestas
son ``200``. Lo garantiza el candado de la fila del nodo en ``fleet.node_inventory``
(``SELECT … FOR UPDATE``), también en el primer latido (``INSERT … ON CONFLICT DO NOTHING``).

**Una sola firma por renovación** (A-55): 6 latidos distintos del nodo llegan a la vez con el sobre
de compuertas a menos de 24 h de vencer; la primera firma se retiene hasta que los otros cinco
esperan la exclusión de la zona (``pg_locks``, sin topes de pared); al soltarla, ninguno vuelve a
firmar porque el umbral se comprueba otra vez con la exclusión tomada.

**Orden de los candados entre operaciones** (AGENTS.md; orden del módulo ``fleet.heartbeat``): el
latido y otra operación que comparte candados se lanzan a la vez: la primera escribe su primer
registro y retiene su transacción ``HOLD_SECONDS``; la segunda arranca entonces. Con el orden único
las dos terminan con el resultado de ir una tras otra; con el orden invertido (mutaciones del PR)
acaban en interbloqueo (``temporarily_unavailable``):

- latido con cambio de URL y vuelta a ``reachable`` contra la revocación del mismo nodo (ficha →
  identidad → cadena);
- latido con cambio de ``model_version`` y vuelta a ``reachable`` contra la recaptura del encuadre
  de una zona del nodo (regresión → cadena).

Cada prueba corre tres veces (``attempt``). Topes de la base 60 s, llegada 30 s (retro 15).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Final

import httpx
import pytest

from tests.heartbeat_support import (
    LIVE_VIEW_URL,
    HeartbeatStack,
    Instance,
    NodeSite,
    heartbeat_stack,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.fleet.adapters.postgres.inventory_projection import (
    PostgresInventoryProjection,
)
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.common import FleetDependencies
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.domain.heartbeat import InventoryRow
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

COPIES: Final = 6
ATTEMPTS: Final = range(3)
HOLD_SECONDS: Final = 5.0
"""Cuánto retiene la primera operación su transacción tras su primer registro (no decide nada)."""
ARRIVAL_SECONDS: Final = 30.0
POLL_SECONDS: Final = 0.05
REASON: Final = "Equipo retirado por mantenimiento"
HOUR: Final = dt.timedelta(hours=1)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "fleet_heartbeat_concurrency") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: HeartbeatStack) -> Iterator[None]:
    # Cada prueba cierra las instancias que crea: sus pools no se acumulan en el módulo.
    yield
    stack.run(stack.release())


class CountingInventory(PostgresInventoryProjection):
    """La proyección real; cuenta las escrituras de la fila del nodo que llegan a hacerse."""

    def __init__(self) -> None:
        self.writes = 0

    async def insert_first(self, transaction: Transaction, row: InventoryRow) -> bool:
        inserted = await super().insert_first(transaction, row)
        self.writes += int(inserted)
        return inserted

    async def update(self, transaction: Transaction, row: InventoryRow) -> None:
        await super().update(transaction, row)
        self.writes += 1


def _two(stack: HeartbeatStack, **changes: Any) -> tuple[Instance, Instance]:
    return stack.instance(**changes), stack.instance(**changes)


# --- Una sola vez por heartbeat_id ---------------------------------------------------------------


@pytest.mark.parametrize("attempt", ATTEMPTS)
@pytest.mark.parametrize("first", [True, False], ids=["primer-latido", "nodo-con-fila"])
def test_the_same_heartbeat_six_times_at_once_on_two_instances_is_accepted_once(
    stack: HeartbeatStack, first: bool, attempt: int
) -> None:
    site = stack.site(zones=2)
    if not first:
        stack.tick()
        assert stack.post(site).status_code == 200
        stack.set_communication_state(site.node_id, "mute")  # la vuelta a reachable está en juego
    before = len(stack.communication(site))
    counting = CountingInventory()
    a, b = _two(stack, inventory=counting)
    stack.tick()
    body = stack.body(site)

    async def at_once() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(stack.send(site, body, (a, b)[copy % 2]) for copy in range(COPIES))
            )
        )

    responses = stack.run(at_once())
    assert [response.status_code for response in responses] == [200] * COPIES, [
        response.text for response in responses
    ]
    rows = [
        row
        for row in stack.history(site.node_id)
        if str(row["heartbeat_id"]) == body["heartbeat_id"]
    ]
    assert len(rows) == 1
    assert counting.writes == 1
    assert len(stack.communication(site)) - before == 1
    # Las seis copias, atienda la que atienda, reciben los mismos bytes (BR-CTR-26, VIG-182).
    assert {response.content for response in responses} == {responses[0].content}


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_six_distinct_heartbeats_at_once_after_mute_write_reachable_once(
    stack: HeartbeatStack, attempt: int
) -> None:
    # Cada latido decide la transición con la fila del nodo bloqueada: con una lectura vieja,
    # los seis verían ``mute`` y escribirían seis ``reachable`` seguidos.
    site = stack.site()
    stack.tick()
    assert stack.post(site).status_code == 200
    stack.set_communication_state(site.node_id, "mute")
    a, b = _two(stack)
    stack.tick()
    bodies = [stack.body(site, uptime_seconds=4000 + copy) for copy in range(COPIES)]

    async def at_once() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(stack.send(site, body, (a, b)[copy % 2]) for copy, body in enumerate(bodies))
            )
        )

    responses = stack.run(at_once())
    assert [response.status_code for response in responses] == [200] * COPIES
    assert [record["state"] for record in stack.communication(site)] == ["reachable"] * 2
    assert len(stack.history(site.node_id)) == 1 + COPIES


# --- A-55: una sola firma por renovación ---------------------------------------------------------


async def _wait_for_waiters(stack: HeartbeatStack, count: int) -> None:
    """Hasta que ``count`` sesiones esperan un candado consultivo (sin tope de pared que decida)."""
    admin = stack.authz.sessions.admin
    deadline = asyncio.get_running_loop().time() + ARRIVAL_SECONDS
    while True:
        waiting = await admin.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        )
        if waiting >= count:
            return
        assert asyncio.get_running_loop().time() < deadline, f"esperan {waiting} de {count}"
        await asyncio.sleep(POLL_SECONDS)


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_concurrent_heartbeats_renew_an_expiring_envelope_once(
    stack: HeartbeatStack, attempt: int
) -> None:
    site = stack.site(gate_issued_at=stack.now() - dt.timedelta(days=6, hours=12))
    zone = site.zones[0]
    old = stack.gate_text(zone)
    a, b = _two(stack)
    signer = stack.signer
    before = signer.calls
    held = threading.Event()
    signer.entered.clear()
    signer.gate = held

    async def race() -> list[httpx.Response]:
        stack.tick()
        sends = [
            asyncio.ensure_future(stack.send(site, stack.body(site), (a, b)[copy % 2]))
            for copy in range(COPIES)
        ]
        try:
            await asyncio.to_thread(signer.entered.wait, ARRIVAL_SECONDS)
            assert signer.entered.is_set(), "nadie llegó a firmar"
            await _wait_for_waiters(stack, COPIES - 1)
        finally:
            held.set()
        return list(await asyncio.gather(*sends))

    try:
        responses = stack.run(race())
    finally:
        signer.gate = None
    assert [response.status_code for response in responses] == [200] * COPIES
    assert signer.calls - before == 1
    renewed = stack.gate_text(zone)
    assert renewed != old
    assert all(renewed.encode("utf-8") in response.content for response in responses)


# --- Orden de los candados entre operaciones -----------------------------------------------------


class HoldingWriter:
    """Un escritor que, tras el **primer** registro, avisa y retiene la transacción."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.arrived = asyncio.Event()

    async def write(self, *args: Any, **kwargs: Any) -> Any:
        written = await self._inner.write(*args, **kwargs)
        if not self.arrived.is_set():
            self.arrived.set()
            await asyncio.sleep(HOLD_SECONDS)
        return written

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _race(
    stack: HeartbeatStack,
    writer: HoldingWriter,
    first: Callable[[], Awaitable[Any]],
    second: Callable[[], Awaitable[Any]],
) -> list[Any]:
    """``first`` hasta su primer registro; entonces ``second``; los dos resultados."""

    async def race() -> list[Any]:
        held = asyncio.ensure_future(first())
        arrival = asyncio.ensure_future(writer.arrived.wait())
        await asyncio.wait({held, arrival}, timeout=ARRIVAL_SECONDS, return_when="FIRST_COMPLETED")
        arrival.cancel()
        assert writer.arrived.is_set(), "la primera operación no llegó a escribir"
        follower = asyncio.ensure_future(second())
        return list(await asyncio.gather(held, follower, return_exceptions=True))

    outcomes: list[Any] = stack.run(race())
    return outcomes


def _installer(stack: HeartbeatStack, site: NodeSite) -> ScopeContext:
    authz = stack.authz
    installer = authz.add_provider_user()
    concession = authz.add_concession(
        site.organization_id, installer, granted_at=authz.now() - HOUR
    )
    cookie = authz.open_session(authz.provider_organization_id, installer)
    scope = stack.run(authz.contexts.context_from_session(cookie, concession_id=concession))
    context: ScopeContext = scope.context
    return context


def _revocations(stack: HeartbeatStack, instance: Instance, writer: Any) -> NodeRevocationService:
    sessions = stack.authz.sessions
    hierarchy = HierarchyService(
        IdentityDependencies(
            database=instance.database,
            writer=writer,
            audit=sessions.audit,
            outbox=stack.outbox,
            authorizer=stack.authz.authorizer,
            free_text=stack.free_text,
            clock=sessions.clock,
            provider_organization_id=stack.authz.provider_organization_id,
        )
    )
    return NodeRevocationService(
        FleetDependencies(
            database=instance.database,
            writer=writer,
            audit=sessions.audit,
            authorizer=stack.authz.authorizer,
            free_text=stack.free_text,
            clock=sessions.clock,
            identity=hierarchy,
            nodes=PostgresNodeFleetStore(instance.database),
        )
    )


def _ready_for_a_transition(stack: HeartbeatStack, site: NodeSite) -> None:
    stack.tick()
    assert stack.post(site).status_code == 200
    stack.set_communication_state(site.node_id, "mute")
    stack.tick()


@pytest.mark.parametrize("attempt", ATTEMPTS)
@pytest.mark.parametrize(
    "heartbeat_first", [True, False], ids=["latido-primero", "revocación-primero"]
)
def test_a_heartbeat_and_a_revocation_of_the_same_node_never_deadlock(
    stack: HeartbeatStack, heartbeat_first: bool, attempt: int
) -> None:
    site = stack.site()
    _ready_for_a_transition(stack, site)
    installer = _installer(stack, site)
    holding = HoldingWriter(stack.writer)
    heartbeat_writer = holding if heartbeat_first else stack.writer
    instance = stack.instance(writer=heartbeat_writer)
    revocations = _revocations(stack, instance, stack.writer if heartbeat_first else holding)
    body = stack.body(site, live_view_local_url=LIVE_VIEW_URL)

    async def heartbeat() -> httpx.Response:
        return await stack.send(site, body, instance)

    async def revoke() -> Any:
        return await revocations.revoke(installer, site.node_id, REASON)

    first, second = (heartbeat, revoke) if heartbeat_first else (revoke, heartbeat)
    outcomes = _race(stack, holding, first, second)
    response, revoked = outcomes if heartbeat_first else outcomes[::-1]
    assert isinstance(response, httpx.Response), response
    assert response.status_code == 200, response.text
    assert not isinstance(revoked, BaseException), revoked
    states = [record["state"] for record in stack.communication(site)]
    document = response.json()
    if heartbeat_first:
        # El latido confirmó antes: su transición y su URL quedan; la revocación vino después.
        assert states == ["reachable", "reachable"] and document["revoked"] is False
        assert stack.urls(site.node_id) == (LIVE_VIEW_URL, LIVE_VIEW_URL)
    else:
        # La revocación confirmó antes: el latido la lee en su transacción (BR-GOB-66, 76).
        assert states == ["reachable"] and document["revoked"] is True
        assert stack.urls(site.node_id) == (None, None)


@pytest.mark.parametrize("attempt", ATTEMPTS)
@pytest.mark.parametrize(
    "heartbeat_first", [True, False], ids=["latido-primero", "recaptura-primero"]
)
def test_a_model_change_and_a_framing_recapture_of_the_same_zone_never_deadlock(
    stack: HeartbeatStack, heartbeat_first: bool, attempt: int
) -> None:
    site = stack.site()
    zone = site.zones[0]
    _ready_for_a_transition(stack, site)
    installer = _installer(stack, site)
    holding = HoldingWriter(stack.writer)
    sessions = stack.authz.sessions

    def regression(database: Any, writer: Any) -> RegressionService:
        return RegressionService(
            repository=PostgresRegressionRepository(database),
            catalog=PostgresCatalogRepository(database),
            database=database,
            writer=writer,
            authorizer=stack.authz.authorizer,
            audit=sessions.audit,
            free_text=stack.free_text,
            clock=sessions.clock,
        )

    database = stack.database()
    heartbeat_writer = holding if heartbeat_first else stack.writer
    instance = stack.instance(
        database,
        writer=heartbeat_writer,
        regression=regression(database, heartbeat_writer),
    )
    recaptures = regression(database, stack.writer if heartbeat_first else holding)
    body = stack.body(site, model_version="modelo-2.0")

    async def heartbeat() -> httpx.Response:
        return await stack.send(site, body, instance)

    async def recapture() -> Any:
        return await recaptures.mark_framing_recaptured(
            installer, zone, site.cameras[zone][0], "Recaptura sintética del encuadre de la celda"
        )

    first, second = (heartbeat, recapture) if heartbeat_first else (recapture, heartbeat)
    outcomes = _race(stack, holding, first, second)
    response, recaptured = outcomes if heartbeat_first else outcomes[::-1]
    assert isinstance(response, httpx.Response), response
    assert response.status_code == 200, response.text
    assert not isinstance(recaptured, BaseException), recaptured
    causes = [mark["content"]["cause"] for mark in stack.regression_marks(site)]
    expected = ["model_version_change", "framing_recaptured"]
    assert causes == (expected if heartbeat_first else expected[::-1])
    assert [record["state"] for record in stack.communication(site)] == ["reachable", "reachable"]
