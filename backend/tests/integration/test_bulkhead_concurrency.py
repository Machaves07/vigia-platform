"""Mamparos por clase de ruta con peticiones concurrentes a la aplicación real (TASK-205).

La aplicación ``vigia-api`` completa (cadena real de middleware, con el eslabón ``bulkhead`` entre
el límite de cuerpo y el límite de tasa por origen) recibe peticiones **a la vez** desde un
cliente ASGI (``asyncio.gather`` sobre un mismo bucle, que es lo que comparte un trabajador de
uvicorn). Los manejadores de prueba retienen la petición dentro del manejador hasta que la prueba
los suelta; la espera de las personas usa el temporizador sobre ``SimulatedClock`` (nunca la
pared, retro 15).

- 36 peticiones de nodo a la vez: 35 entran en el manejador y la 36.ª recibe
  ``temporarily_unavailable`` con ``retry_after_seconds`` y ``Retry-After`` sin avanzar el reloj;
  una petición de persona simultánea se atiende. Sin el semáforo de nodos (sonda negativa), la
  36.ª entra y la prueba falla.
- 15 personas retenidas: la 16.ª espera; se atiende si un puesto se libera antes de 2 s y recibe
  ``temporarily_unavailable`` a los 2 s si no.
- Un manejador que lanza, una petición cancelada y un cliente que se desconecta devuelven el
  puesto (``bulkhead_in_use`` vuelve a 0).
- Las sondas de salud del balanceador no esperan al mamparo de personas.

No usa contenedores: el mamparo es un semáforo dentro del proceso. Solo datos generados.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request

from tests.bulkhead_support import ManualTimer, bulkheads_with_reader, gauge, until
from tests.middleware_support import Harness, cookie_header
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.app import UnitRegistration
from vigia_platform.shared.api.declarations import NodeRoute, node_route, requires
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.bulkheads import PERSON_WAIT_SECONDS, RETRY_AFTER_SECONDS, Bulkheads
from vigia_platform.shared.db import RouteClass
from vigia_platform.shared.observability.metrics import MetricName

NODE, PERSON = RouteClass.NODE, RouteClass.PERSON
NODE_HOLD = "/api/nodes/update-results?mode=hold"
"""La ruta de nodo que retiene la petición hasta que la prueba la suelta."""


@dataclass
class Holds:
    """Manejadores que retienen la petición hasta que la prueba los suelta."""

    entered: Counter[str] = field(default_factory=Counter)
    release: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def gate(self, name: str) -> asyncio.Semaphore:
        return self.release.setdefault(name, asyncio.Semaphore(0))

    def let_go(self, name: str, count: int = 1) -> None:
        for _ in range(count):
            self.gate(name).release()

    async def hold(self, name: str) -> None:
        self.entered[name] += 1
        await self.gate(name).acquire()


def _unit(holds: Holds) -> UnitRegistration:
    router = APIRouter()

    # Una ruta del contrato (A-51) con tres comportamientos según ``mode``:
    # /api/nodes/update-results?mode={hold|boom|listen}. El arnés no monta las rutas de producción
    # (VIG-162 publica esta), así que la ruta es solo de la prueba.
    @router.post(NodeRoute.UPDATE_RESULT.path, dependencies=[node_route(NodeRoute.UPDATE_RESULT)])
    async def node_route_handler(request: Request, mode: str = "hold") -> dict[str, str]:
        if mode == "boom":
            raise RuntimeError("fallo del manejador")
        if mode == "listen":
            holds.entered["listen"] += 1
            while not await request.is_disconnected():
                await asyncio.sleep(0)
            return {"ok": "gone"}
        await holds.hold("node")
        return {"ok": "node"}

    @router.get("/hold", dependencies=[requires(PermissionKey.HIERARCHY_READ.value)])
    async def person_hold() -> dict[str, str]:
        await holds.hold("person")
        return {"ok": "person"}

    return UnitRegistration("mamparo", routers=(router,))


@dataclass
class Scenario:
    harness: Harness
    holds: Holds
    bulkheads: Bulkheads
    timer: ManualTimer
    reader: Any
    app: Any
    client: httpx.AsyncClient
    cookie: dict[str, str]


async def _scenario(node: int = 35, person: int = 15) -> AsyncIterator[Scenario]:
    holds = Holds()
    harness = Harness(extra_units=(_unit(holds),))
    bulkheads, timer, reader = bulkheads_with_reader(node=node, person=person)
    app = harness.app(bulkheads=bulkheads)
    cookie = cookie_header(harness.session("mamparo"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://app.vigia.test", timeout=30.0
    ) as client:
        try:
            yield Scenario(harness, holds, bulkheads, timer, reader, app, client, cookie)
        finally:
            for name in ("node", "person"):
                holds.let_go(name, 100)


def _run(test: Any) -> None:
    async def main() -> None:
        generator = _scenario()
        scenario = await anext(generator)
        try:
            await test(scenario)
        finally:
            await generator.aclose()

    asyncio.run(main())


def _is_temporarily_unavailable(response: httpx.Response) -> bool:
    body = response.json()
    return (
        response.status_code == 503
        and body["code"] == ApiErrorCode.TEMPORARILY_UNAVAILABLE.value
        and body["retry_after_seconds"] == RETRY_AFTER_SECONDS
        and response.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    )


# --- Nodos ---------------------------------------------------------------------------------------


def test_36_simultaneous_nodes_admit_35_reject_one_at_once_and_a_person_is_served() -> None:
    async def test(s: Scenario) -> None:
        nodes = [asyncio.create_task(s.client.post(NODE_HOLD)) for _ in range(36)]
        # Las 36 llegan a la vez: 35 quedan dentro del manejador y una sale ya rechazada.
        await until(lambda: s.holds.entered["node"] == 35 and sum(t.done() for t in nodes) == 1)
        (rejected,) = [task.result() for task in nodes if task.done()]
        assert _is_temporarily_unavailable(rejected)
        assert rejected.json()["code"] != ApiErrorCode.RATE_LIMITED.value
        assert s.timer.clock.monotonic() == 0  # de inmediato: el reloj no avanzó
        assert s.bulkheads.in_use(NODE) == 35
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 35
        # Una persona simultánea se atiende con los 35 nodos todavía dentro.
        person = await s.client.get("/me", headers=s.cookie)
        assert person.status_code == 200
        assert s.holds.entered["node"] == 35
        s.holds.let_go("node", 35)
        responses = await asyncio.gather(*[t for t in nodes if not t.done()])
        assert [r.status_code for r in responses] == [200] * 35
        assert s.bulkheads.in_use(NODE) == 0
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 0

    _run(test)


def test_a_node_rejected_by_the_bulkhead_is_served_once_a_slot_frees() -> None:
    async def test(s: Scenario) -> None:
        nodes = [asyncio.create_task(s.client.post(NODE_HOLD)) for _ in range(35)]
        await until(lambda: s.holds.entered["node"] == 35)
        assert _is_temporarily_unavailable(await s.client.post(NODE_HOLD))
        s.holds.let_go("node")
        await until(lambda: s.bulkheads.in_use(NODE) == 34)
        retry = asyncio.create_task(s.client.post(NODE_HOLD))
        await until(lambda: s.holds.entered["node"] == 36)
        s.holds.let_go("node", 35)
        assert (await retry).status_code == 200
        await asyncio.gather(*nodes)

    _run(test)


# --- Personas ------------------------------------------------------------------------------------


async def _fill_persons(s: Scenario) -> list[asyncio.Task[httpx.Response]]:
    persons = [asyncio.create_task(s.client.get("/hold", headers=s.cookie)) for _ in range(15)]
    await until(lambda: s.holds.entered["person"] == 15)
    return persons


def test_the_16th_person_waits_and_is_served_if_a_slot_frees_before_2_seconds() -> None:
    async def test(s: Scenario) -> None:
        persons = await _fill_persons(s)
        sixteenth = asyncio.create_task(s.client.get("/me", headers=s.cookie))
        await until(lambda: s.timer.sleeping == 1)
        s.timer.advance(PERSON_WAIT_SECONDS - 0.001)
        await asyncio.sleep(0)
        assert not sixteenth.done()
        s.holds.let_go("person")  # un puesto se libera antes de los 2 s
        await until(sixteenth.done)
        assert (await sixteenth).status_code == 200
        s.holds.let_go("person", 14)
        assert [r.status_code for r in await asyncio.gather(*persons)] == [200] * 15
        assert s.bulkheads.in_use(PERSON) == 0

    _run(test)


def test_the_16th_person_gets_temporarily_unavailable_at_2_seconds() -> None:
    async def test(s: Scenario) -> None:
        persons = await _fill_persons(s)
        sixteenth = asyncio.create_task(s.client.get("/me", headers=s.cookie))
        await until(lambda: s.timer.sleeping == 1)
        s.timer.advance(PERSON_WAIT_SECONDS)
        await until(sixteenth.done)
        assert _is_temporarily_unavailable(await sixteenth)
        assert s.timer.clock.monotonic() == PERSON_WAIT_SECONDS
        # Los nodos no se ven afectados por el mamparo de personas lleno.
        node = asyncio.create_task(s.client.post(NODE_HOLD))
        await until(lambda: s.holds.entered["node"] == 1)
        s.holds.let_go("node")
        assert (await node).status_code == 200
        s.holds.let_go("person", 15)
        await asyncio.gather(*persons)
        assert s.bulkheads.in_use(PERSON) == 0

    _run(test)


def test_health_probes_do_not_wait_for_the_person_bulkhead() -> None:
    async def test(s: Scenario) -> None:
        persons = await _fill_persons(s)
        probes = [
            asyncio.create_task(s.client.get(path)) for path in ("/health/live", "/health/ready")
        ]
        await until(lambda: all(probe.done() for probe in probes))
        live, ready = (probe.result() for probe in probes)
        assert live.status_code == 200
        # Sin el supervisor de arranque, ``ready`` no está lista; lo que importa es que respondió
        # sin esperar un puesto de persona.
        assert ready.status_code in (200, 503)
        assert s.timer.sleeping == 0
        s.holds.let_go("person", 15)
        await asyncio.gather(*persons)

    _run(test)


# --- El puesto vuelve siempre --------------------------------------------------------------------


def test_a_raising_handler_returns_the_slot() -> None:
    async def test(s: Scenario) -> None:
        response = await s.client.post("/api/nodes/update-results?mode=boom")
        # Clase node: un error interno es el transitorio del contrato, nunca un ApiError.
        assert response.status_code == 503
        assert response.json()["code"] == "temporarily_unavailable"
        assert s.bulkheads.in_use(NODE) == 0
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 0

    _run(test)


def test_a_cancelled_request_returns_the_slot() -> None:
    async def test(s: Scenario) -> None:
        request = asyncio.create_task(s.client.post(NODE_HOLD))
        await until(lambda: s.holds.entered["node"] == 1)
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 1
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert s.bulkheads.in_use(NODE) == 0
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 0

    _run(test)


def test_a_client_disconnect_returns_the_slot() -> None:
    async def test(s: Scenario) -> None:
        disconnected = asyncio.Event()
        sent: list[MutableMapping[str, Any]] = []

        async def receive() -> MutableMapping[str, Any]:
            if not sent and not disconnected.is_set():
                disconnected.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            await until(lambda: s.holds.entered["listen"] == 1)
            return {"type": "http.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            # El cliente ya no está: escribir la respuesta falla.
            sent.append(message)
            raise ConnectionResetError("cliente desconectado")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/nodes/update-results",
            "raw_path": b"/api/nodes/update-results",
            "root_path": "",
            "query_string": b"mode=listen",
            "headers": [(b"host", b"app.vigia.test")],
            "client": ("192.0.2.10", 50000),
            "server": ("app.vigia.test", 443),
        }
        with pytest.raises(ConnectionResetError):
            await s.app(scope, receive, send)
        assert s.holds.entered["listen"] == 1
        assert s.bulkheads.in_use(NODE) == 0
        assert gauge(s.reader, MetricName.BULKHEAD_IN_USE, NODE) == 0

    _run(test)
