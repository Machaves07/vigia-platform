"""``vigia-worker`` sin base: rutas, configuración, arranque, bucles y parada (TASK-130).

- **Metapropiedad** (PAT-NUC-ESC-04): la aplicación del worker no registra ninguna ruta salvo
  ``/health/live`` (ni documentación, ni esquema, ni ``/health/ready``).
- **Un bucle de despacho por consumidor registrado** y parada ordenada: al fijar la parada, cada
  bucle la recibe, el proceso sale con 0 y libera la base.
- **Mismas comprobaciones de arranque que la API**: con el esquema anterior al mínimo, el proceso
  nunca arranca bucles y sale con ``STARTUP_FAILURE_EXIT_CODE``; ``/health/live`` responde
  mientras tanto.
- ``WorkerConfig`` estricto y ``VIGIA_WORKER_RUNTIME`` cerrado al paquete.

La base es un doble: ``health`` responde lo que diga la prueba y toda transacción falla como una
base caída (el planificador y la métrica de antigüedad lo toleran). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from tests.worker_support import StubKms, StubSigning, StubStorage, system_contexts
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import DatabaseHealth, TemporarilyUnavailable, Transaction
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.outbox.registries import Consumer, OutboxCatalog
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.worker import main as worker_main
from vigia_platform.shared.worker.leases import SqlLeaseStore
from vigia_platform.shared.worker.main import (
    LIVE_PATH,
    LOOP_FAILURE_EXIT_CODE,
    RUNTIME_VARIABLE,
    WorkerConfig,
    WorkerProcess,
    WorkerRuntime,
    create_worker_app,
    resolve_runtime_builder,
)
from vigia_platform.shared.worker.scheduler import PeriodicScheduler

# --- Metapropiedad -----------------------------------------------------------------------------


def test_the_worker_app_registers_only_health_live() -> None:
    app = create_worker_app()
    routes = [
        (getattr(r, "path", None), sorted(getattr(r, "methods", []) or [])) for r in app.routes
    ]
    assert routes == [(LIVE_PATH, ["GET"])]
    assert all(isinstance(route, APIRoute) for route in app.routes)
    assert LIVE_PATH == "/health/live"


def test_only_health_live_answers() -> None:
    async def probe() -> dict[str, int]:
        transport = httpx.ASGITransport(app=create_worker_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://w", timeout=5) as c:
            codes = {}
            for path in ("/health/live", "/health/ready", "/openapi.json", "/docs", "/redoc"):
                codes[path] = (await c.get(path)).status_code
            live = await c.get("/health/live")
            assert live.json() == {"status": "live"}
            assert live.headers["cache-control"] == "no-store"
            assert (await c.post("/health/live")).status_code == 405
            return codes

    assert asyncio.run(probe()) == {
        "/health/live": 200,
        "/health/ready": 404,
        "/openapi.json": 404,
        "/docs": 404,
        "/redoc": 404,
    }


# --- Configuración -----------------------------------------------------------------------------

_BASE_ENV = {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets"}


def test_config_defaults_are_the_design_values() -> None:
    config = WorkerConfig.from_environ(_BASE_ENV)
    assert config.health_port == 8001
    assert config.health_host == "127.0.0.1"
    assert config.lease_settings.duration.total_seconds() == 60
    assert config.lease_settings.renew_every.total_seconds() == 20
    assert config.shutdown_grace_seconds < 120
    assert config.app_config().environment == "pilot"


@pytest.mark.parametrize(
    "port", ["", "0", "65536", "99999", "8001 ", "-1", "0x1f", "\uff18\uff10\uff10\uff11", "1e3"]
)
def test_config_rejects_bad_health_ports(port: str) -> None:
    with pytest.raises(ValueError):
        WorkerConfig.from_environ({**_BASE_ENV, "VIGIA_WORKER_HEALTH_PORT": port})


def test_config_accepts_the_port_edges() -> None:
    for port in ("1", "65535"):
        config = WorkerConfig.from_environ({**_BASE_ENV, "VIGIA_WORKER_HEALTH_PORT": port})
        assert config.health_port == int(port)


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "production"},
        {"environment": ""},
        {"renew_seconds": 60.0},
        {"renew_seconds": 61.0},
        {"lease_margin_seconds": 40.0},
        {"shutdown_grace_seconds": 120.1},
        {"health_host": "10.0.0.1"},
        {"extra": 1},
    ],
)
def test_config_rejects_inconsistent_values(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {"environment": "test", "data_key_id": "alias/vigia-secrets"}
    values.update(changes)
    with pytest.raises(ValueError):
        WorkerConfig(**values)


@pytest.mark.parametrize(
    "reference",
    [
        None,
        "",
        "os:system",
        "subprocess:run",
        "vigia_platform:main",
        "vigia_platform.shared.worker.main",
        "vigia_platform.shared.worker.main:main\n",
        "vigia_platform.shared.worker.main:does_not_exist",
        "vigia_platform.shared.worker.main:RUNTIME_VARIABLE",
        "tests.worker_process:build_runtime",
    ],
)
def test_the_runtime_builder_is_closed_to_the_package(reference: str | None) -> None:
    with pytest.raises(ValueError):
        resolve_runtime_builder(reference)


def test_a_package_function_resolves() -> None:
    builder = resolve_runtime_builder("vigia_platform.shared.worker.main:create_worker_app")
    assert builder is create_worker_app  # type: ignore[comparison-overlap]


def test_main_rejects_arguments_and_bad_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert worker_main.main(["--anything"]) == 2
    for name in (*_BASE_ENV, RUNTIME_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    assert worker_main.main([]) == STARTUP_FAILURE_EXIT_CODE
    for name, value in _BASE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(RUNTIME_VARIABLE, "os:system")
    assert worker_main.main([]) == STARTUP_FAILURE_EXIT_CODE


# --- Proceso con dobles ------------------------------------------------------------------------


class FakeDatabase:
    """``health`` configurable; toda transacción falla como con la base caída."""

    def __init__(self, schema_version: int | None = MINIMUM_SCHEMA_VERSION) -> None:
        self.schema_version = schema_version
        self.disposed = False

    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        return DatabaseHealth(visible_organizations=0, schema_version=self.schema_version)

    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        raise TemporarilyUnavailable()
        yield  # pragma: no cover

    async def dispose(self) -> None:
        self.disposed = True


class RecordingDispatcher:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None:
        self.started.append(consumer_name)
        await stop.wait()
        self.stopped.append(consumer_name)


async def _nothing(*_: Any) -> None:
    return None


def _catalog() -> OutboxCatalog:
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    register_alerts_consumer(catalog.consumers)
    catalog.consumers.register(
        Consumer(
            consumer_name="unit_probe_consumer",
            unit=ActorUnit.U03,
            subscribed_events=("security_alert",),
            handler=_nothing,
        )
    )
    return catalog


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _process(
    database: FakeDatabase, dispatcher: Any, *, shutdown_grace_seconds: float = 5.0
) -> tuple[WorkerProcess, int]:
    catalog = _catalog()
    clock = SystemClock()

    async def seal() -> None:
        catalog.check()
        catalog.seal()

    port = _free_port()
    config = WorkerConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        health_port=port,
        startup_deadline_seconds=0.5,
        startup_retry_seconds=0.1,
        shutdown_grace_seconds=shutdown_grace_seconds,
    )
    runtime = WorkerRuntime(
        clock=clock,
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=StubKms(),
        catalog=catalog,
        dispatcher=dispatcher,
        contexts=system_contexts(clock, uuid.uuid4()),
        registries=(seal,),
    )
    return WorkerProcess(config, runtime), port


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_one_dispatch_loop_per_consumer_and_a_graceful_stop(
    loop: asyncio.AbstractEventLoop,
) -> None:
    database, dispatcher = FakeDatabase(), RecordingDispatcher()
    process, port = _process(database, dispatcher)

    async def scenario() -> tuple[int, int]:
        stop = asyncio.Event()
        running = asyncio.create_task(process.run(stop))
        await asyncio.wait_for(process.started.wait(), 10)
        async with httpx.AsyncClient(timeout=5) as client:
            live = (await client.get(f"http://127.0.0.1:{port}{LIVE_PATH}")).status_code
        stop.set()
        return live, await asyncio.wait_for(running, 10)

    live, code = loop.run_until_complete(scenario())
    assert live == 200
    assert code == 0
    assert sorted(dispatcher.started) == ["alert_metrics", "unit_probe_consumer"]
    assert sorted(dispatcher.stopped) == sorted(dispatcher.started)
    assert database.disposed


def test_a_failed_startup_never_starts_loops_and_exits_with_the_startup_code(
    loop: asyncio.AbstractEventLoop,
) -> None:
    database, dispatcher = (
        FakeDatabase(schema_version=MINIMUM_SCHEMA_VERSION - 1),
        RecordingDispatcher(),
    )
    process, port = _process(database, dispatcher)

    async def scenario() -> tuple[int, int]:
        stop = asyncio.Event()
        running = asyncio.create_task(process.run(stop))
        await asyncio.sleep(0.2)
        async with httpx.AsyncClient(timeout=5) as client:
            live = (await client.get(f"http://127.0.0.1:{port}{LIVE_PATH}")).status_code
        return live, await asyncio.wait_for(running, 10)

    live, code = loop.run_until_complete(scenario())
    assert live == 200  # /health/live responde durante el arranque
    assert code == STARTUP_FAILURE_EXIT_CODE
    assert dispatcher.started == []
    assert not process.started.is_set()
    assert database.disposed


def test_a_stop_during_startup_exits_cleanly(loop: asyncio.AbstractEventLoop) -> None:
    database, dispatcher = FakeDatabase(schema_version=None), RecordingDispatcher()
    process, _ = _process(database, dispatcher)

    async def scenario() -> int:
        stop = asyncio.Event()
        running = asyncio.create_task(process.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        return await asyncio.wait_for(running, 10)

    assert loop.run_until_complete(scenario()) == 0
    assert dispatcher.started == []


# --- Resiliencia de los bucles -----------------------------------------------------------------


class DyingDispatcher(RecordingDispatcher):
    """El bucle de un consumidor muere con un defecto; los demás esperan la parada."""

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None:
        if consumer_name == "unit_probe_consumer":
            self.started.append(consumer_name)
            raise RuntimeError("defecto del bucle")
        await super().run(consumer_name, stop)


def test_a_dead_loop_is_logged_stops_the_rest_and_exits_with_an_error(
    loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    database, dispatcher = FakeDatabase(), DyingDispatcher()
    process, _ = _process(database, dispatcher)

    async def scenario() -> int:
        return await asyncio.wait_for(process.run(asyncio.Event()), 10)

    with caplog.at_level(logging.ERROR):
        code = loop.run_until_complete(scenario())
    assert code == LOOP_FAILURE_EXIT_CODE != 0
    assert dispatcher.stopped == ["alert_metrics"]  # el resto se paró en orden
    dead = [r for r in caplog.records if "murió" in r.getMessage()]
    assert len(dead) == 1 and dead[0].exc_info is not None
    assert database.disposed


class StuckDispatcher:
    """Un bucle que no atiende la parada: solo la cancelación lo termina."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.append(consumer_name)
            raise


def test_the_graceful_stop_is_capped_and_cancels_what_remains(
    loop: asyncio.AbstractEventLoop,
) -> None:
    dispatcher = StuckDispatcher()
    process, _ = _process(FakeDatabase(), dispatcher, shutdown_grace_seconds=0.2)

    async def scenario() -> tuple[int | None, float]:
        stop = asyncio.Event()
        running = asyncio.create_task(process.run(stop))
        await asyncio.wait_for(process.started.wait(), 10)
        stopped_at = loop.time()
        stop.set()
        # Sin cancelar ``running``: si la parada no tiene tope, la prueba falla en vez de colgarse.
        done, _ = await asyncio.wait({running}, timeout=5)
        return (running.result() if done else None), loop.time() - stopped_at

    code, elapsed = loop.run_until_complete(scenario())
    assert code == 0, "la parada ordenada no terminó en 5 s"
    assert sorted(dispatcher.cancelled) == ["alert_metrics", "unit_probe_consumer"]
    assert elapsed < 5


def _bare_scheduler(poll_seconds: float) -> PeriodicScheduler:
    clock = SystemClock()
    database = FakeDatabase()
    contexts = system_contexts(clock, uuid.uuid4())
    return PeriodicScheduler(
        database=database,
        registry=_catalog().periodic_tasks,
        leases=SqlLeaseStore(database=database, contexts=contexts),
        contexts=contexts,
        clock=clock,
        owner="unit-worker",
        poll_seconds=poll_seconds,
    )


def test_the_scheduler_survives_an_unexpected_error_and_keeps_polling(
    loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = _bare_scheduler(poll_seconds=0.01)
    stop = asyncio.Event()
    rounds: list[int] = []

    async def flaky(_: Any = None) -> list[Any]:
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("defecto inesperado en una ronda")
        stop.set()
        return []

    monkeypatch.setattr(scheduler, "run_pending", flaky)
    monkeypatch.setattr(scheduler, "_report_last_success", _nothing)
    loop.run_until_complete(asyncio.wait_for(scheduler.run(stop), 10))
    assert len(rounds) == 2


def test_the_scheduler_waits_between_rounds(
    loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = _bare_scheduler(poll_seconds=0.1)
    stop = asyncio.Event()
    rounds: list[int] = []

    async def count(_: Any = None) -> list[Any]:
        rounds.append(1)
        if len(rounds) > 50:  # sin espera nunca cede el bucle: se corta aquí
            stop.set()
        return []

    async def scenario() -> None:
        loop.call_later(0.35, stop.set)
        await asyncio.wait_for(scheduler.run(stop), 10)

    monkeypatch.setattr(scheduler, "run_pending", count)
    monkeypatch.setattr(scheduler, "_report_last_success", _nothing)
    loop.run_until_complete(scenario())
    assert 1 <= len(rounds) <= 6
