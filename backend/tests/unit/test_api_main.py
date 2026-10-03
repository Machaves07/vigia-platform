"""``vigia-api`` sin base: configuración, composición, arranque, parada y cabeceras (TASK-143).

- ``ApiServerConfig`` estricto: puerto 8000 por defecto, ninguna red de confianza por defecto y
  ninguna red que lo admita todo (nota de VIG-93).
- ``VIGIA_API_RUNTIME`` cerrado al paquete, como ``VIGIA_WORKER_RUNTIME``.
- Con un constructor que falla o una aplicación que no se puede construir, salida
  ``STARTUP_FAILURE_EXIT_CODE`` sin abrir el puerto.
- Con dobles que pasan las comprobaciones, ``/health/live`` y ``/health/ready`` responden 200 y la
  parada termina con 0; con el esquema anterior al mínimo, ``/health/ready`` nunca da 200 y el
  arranque pide la salida con ``STARTUP_FAILURE_EXIT_CODE``.
- ``X-Forwarded-For`` solo cambia la dirección del cliente si la conexión llega de una red
  declarada; ``FORWARDED_ALLOW_IPS`` del proceso no cuenta.

La base es un doble; firma, KMS y almacén son los de ``tests.worker_support``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from tests.worker_support import StubKms, StubSigning, StubStorage
from vigia_platform.shared.api import main as api_main
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE, AppConfig, AppRuntime
from vigia_platform.shared.api.main import (
    API_PORT,
    FORWARDED_VARIABLE,
    RUNTIME_VARIABLE,
    ApiServerConfig,
    build_server,
    resolve_runtime_builder,
    serve,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import DatabaseHealth
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION

_BASE_ENV = {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets"}
BACKEND = Path(__file__).resolve().parents[2]

# --- Configuración -----------------------------------------------------------------------------


def test_server_defaults_trust_no_proxy_and_use_the_balancer_port() -> None:
    server = ApiServerConfig.from_environ({})
    assert server.port == API_PORT == 8000
    assert server.host == "0.0.0.0"  # noqa: S104 - el valor que se comprueba
    assert server.forwarded_allow_ips == ()


def test_server_reads_the_port_and_the_balancer_networks() -> None:
    server = ApiServerConfig.from_environ(
        {"VIGIA_API_PORT": "8080", FORWARDED_VARIABLE: "10.0.0.0/24 10.0.1.0/24 fd00::/64"}
    )
    assert server.port == 8080
    assert server.forwarded_allow_ips == ("10.0.0.0/24", "10.0.1.0/24", "fd00::/64")
    assert ApiServerConfig.from_environ({FORWARDED_VARIABLE: ""}).forwarded_allow_ips == ()
    single = ApiServerConfig.from_environ({FORWARDED_VARIABLE: "10.0.0.7"})
    assert single.forwarded_allow_ips == ("10.0.0.7/32",)


@pytest.mark.parametrize(
    "networks",
    [
        "*",
        "0.0.0.0/0",
        "::/0",
        "10.0.0.0/24 *",
        "10.0.0.1/24",  # bits de máquina: red mal escrita
        "10.0.0.0/33",
        "10.0.0.0/24  10.0.1.0/24",  # doble espacio
        " 10.0.0.0/24",
        "10.0.0.0/24,10.0.1.0/24",
        "10.0.0.0/24\t10.0.1.0/24",
        "10.0.0.0/24 10.0.0.0/24",
        "localhost",
        chr(0xFF11) + chr(0xFF10) + ".0.0.0/24",  # dígitos de ancho completo
        " ".join(f"10.0.{n}.0/24" for n in range(17)),
    ],
)
def test_server_rejects_networks_that_trust_too_much_or_are_malformed(networks: str) -> None:
    with pytest.raises(ValueError):
        ApiServerConfig.from_environ({FORWARDED_VARIABLE: networks})


@pytest.mark.parametrize(
    "port", ["", "0", "65536", "8000 ", "-1", "0x1f", chr(0xFF18) + chr(0xFF10), "1e3"]
)
def test_server_rejects_bad_ports(port: str) -> None:
    with pytest.raises(ValueError):
        ApiServerConfig.from_environ({"VIGIA_API_PORT": port})


def test_server_accepts_the_port_edges_and_sixteen_networks() -> None:
    for port in ("1", "65535"):
        assert ApiServerConfig.from_environ({"VIGIA_API_PORT": port}).port == int(port)
    sixteen = " ".join(f"10.0.{n}.0/24" for n in range(16))
    assert (
        len(ApiServerConfig.from_environ({FORWARDED_VARIABLE: sixteen}).forwarded_allow_ips) == 16
    )


@pytest.mark.parametrize(
    "changes", [{"host": "10.0.0.1"}, {"graceful_shutdown_seconds": 0}, {"extra": 1}]
)
def test_server_rejects_inconsistent_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ApiServerConfig(**changes)


@pytest.mark.parametrize(
    "reference",
    [
        None,
        "",
        "os:system",
        "subprocess:run",
        "vigia_platform:main",
        "vigia_platform.shared.api.main",
        "vigia_platform.shared.api.main:main\n",
        "vigia_platform.shared.api.main:does_not_exist",
        "vigia_platform.shared.api.main:RUNTIME_VARIABLE",
        "tests.unit.test_api_main:builder",
    ],
)
def test_the_runtime_builder_is_closed_to_the_package(reference: str | None) -> None:
    with pytest.raises(ValueError):
        resolve_runtime_builder(reference)


def test_a_package_function_resolves() -> None:
    builder = resolve_runtime_builder("vigia_platform.shared.api.main:build_server")
    assert builder is build_server  # type: ignore[comparison-overlap]


def test_main_rejects_arguments_and_bad_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert api_main.main(["--anything"]) == 2
    for name in (*_BASE_ENV, RUNTIME_VARIABLE, FORWARDED_VARIABLE):
        monkeypatch.delenv(name, raising=False)
    assert api_main.main([]) == STARTUP_FAILURE_EXIT_CODE  # sin configuración
    for name, value in _BASE_ENV.items():
        monkeypatch.setenv(name, value)
    assert api_main.main([]) == STARTUP_FAILURE_EXIT_CODE  # sin constructor
    monkeypatch.setenv(RUNTIME_VARIABLE, "os:system")
    assert api_main.main([]) == STARTUP_FAILURE_EXIT_CODE
    monkeypatch.setenv(RUNTIME_VARIABLE, "vigia_platform.shared.api.main:build_server")
    monkeypatch.setenv(FORWARDED_VARIABLE, "*")
    assert api_main.main([]) == STARTUP_FAILURE_EXIT_CODE


# --- Proceso con dobles ------------------------------------------------------------------------


class FakeDatabase:
    """``health`` con la versión del esquema que diga la prueba."""

    def __init__(self, schema_version: int | None = MINIMUM_SCHEMA_VERSION) -> None:
        self.schema_version = schema_version

    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        return DatabaseHealth(visible_organizations=0, schema_version=self.schema_version)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _config() -> AppConfig:
    return AppConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        startup_deadline_seconds=1.0,
        startup_retry_seconds=0.1,
    )


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _runtime(database: FakeDatabase, failures: list[int]) -> AppRuntime:
    return AppRuntime(
        clock=SystemClock(),
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=StubKms(),  # type: ignore[arg-type]  # el arranque solo usa la clave de datos
        on_startup_failure=failures.append,
        attribute_policy=AttributePolicy(),  # nunca la política global (test_api_app)
    )


async def _wait_status(client: httpx.AsyncClient, url: str, status: int) -> int:
    """Espera (con tope de segundos) a que ``url`` responda ``status``; devuelve el último."""
    last = 0
    for _ in range(100):
        try:
            last = (await client.get(url)).status_code
        except httpx.TransportError:
            last = 0
        if last == status:
            return last
        await asyncio.sleep(0.1)
    return last


def test_the_api_serves_live_and_ready_and_stops_with_zero(
    loop: asyncio.AbstractEventLoop,
) -> None:
    failures: list[int] = []
    port = _free_port()
    server = ApiServerConfig(host="127.0.0.1", port=port)

    async def builder(config: AppConfig) -> AppRuntime:
        return _runtime(FakeDatabase(), failures)

    async def scenario() -> tuple[int, int, int]:
        stop = asyncio.Event()
        running = asyncio.create_task(serve(_config(), server, builder, stop=stop))
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=5) as client:
            live = await _wait_status(client, f"{base}/health/live", 200)
            ready = await _wait_status(client, f"{base}/health/ready", 200)
        stop.set()
        return live, ready, await asyncio.wait_for(running, 30)

    assert loop.run_until_complete(scenario()) == (200, 200, 0)
    assert failures == []


def test_a_schema_older_than_the_image_never_gets_ready_and_asks_to_exit(
    loop: asyncio.AbstractEventLoop,
) -> None:
    failures: list[int] = []
    port = _free_port()
    server = ApiServerConfig(host="127.0.0.1", port=port)
    asked = asyncio.Event()

    def record(code: int) -> None:
        failures.append(code)
        asked.set()

    async def builder(config: AppConfig) -> AppRuntime:
        runtime = _runtime(FakeDatabase(schema_version=MINIMUM_SCHEMA_VERSION - 1), failures)
        return AppRuntime(
            clock=runtime.clock,
            database=runtime.database,
            storage=runtime.storage,
            signing=runtime.signing,
            kms=runtime.kms,
            on_startup_failure=record,
            attribute_policy=AttributePolicy(),
        )

    async def scenario() -> tuple[int, int, int]:
        stop = asyncio.Event()
        running = asyncio.create_task(serve(_config(), server, builder, stop=stop))
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(timeout=5) as client:
            live = await _wait_status(client, f"{base}/health/live", 200)
            await asyncio.wait_for(asked.wait(), 30)
            ready = (await client.get(f"{base}/health/ready")).status_code
        stop.set()
        return live, ready, await asyncio.wait_for(running, 30)

    live, ready, _ = loop.run_until_complete(scenario())
    assert live == 200  # /health/live responde durante el arranque
    assert ready == 503
    assert failures == [STARTUP_FAILURE_EXIT_CODE]


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_after_ready_stops_the_process_with_zero(number: signal.Signals) -> None:
    # uvicorn vuelve a lanzar la señal al terminar (salida 143); vigia-api sale con 0.
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.api_main_process"],
        cwd=BACKEND,
        env={**os.environ, "VIGIA_API_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready = 0
        for _ in range(300):
            try:
                ready = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=5).status_code
            except httpx.TransportError:
                ready = 0
            if ready == 200 or process.poll() is not None:
                break
            time.sleep(0.1)
        assert ready == 200
        process.send_signal(number)
        assert process.wait(timeout=60) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def test_a_failing_builder_or_app_exits_with_the_startup_code_without_listening(
    loop: asyncio.AbstractEventLoop,
) -> None:
    port = _free_port()
    server = ApiServerConfig(host="127.0.0.1", port=port)

    async def broken(config: AppConfig) -> AppRuntime:
        raise RuntimeError("sin secretos")

    async def not_a_runtime(config: AppConfig) -> AppRuntime:
        return object()  # type: ignore[return-value]

    for builder in (broken, not_a_runtime):
        code = loop.run_until_complete(serve(_config(), server, builder))
        assert code == STARTUP_FAILURE_EXIT_CODE
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


# --- Cabeceras del balanceador -----------------------------------------------------------------


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/client")
    async def client(request: Request) -> dict[str, str]:
        assert request.client is not None
        return {"host": request.client.host, "scheme": request.url.scheme}

    return app


async def _seen_from(server: ApiServerConfig, peer: str) -> dict[str, str]:
    uvicorn_server = build_server(_echo_app(), server)
    uvicorn_server.config.load()
    transport = httpx.ASGITransport(
        app=uvicorn_server.config.loaded_app,
        client=(peer, 40000),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://api", timeout=5) as c:
        response = await c.get(
            "/client", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
        )
        seen: dict[str, str] = response.json()
        return seen


@pytest.mark.parametrize(
    ("networks", "peer", "expected"),
    [
        ((), "10.0.0.5", {"host": "10.0.0.5", "scheme": "http"}),
        ((), "127.0.0.1", {"host": "127.0.0.1", "scheme": "http"}),
        (("10.0.0.0/24",), "10.0.0.5", {"host": "203.0.113.7", "scheme": "https"}),
        (("10.0.0.0/24",), "10.0.1.5", {"host": "10.0.1.5", "scheme": "http"}),
        (("10.0.0.0/24",), "127.0.0.1", {"host": "127.0.0.1", "scheme": "http"}),
    ],
)
def test_forwarded_headers_only_count_from_the_declared_networks(
    monkeypatch: pytest.MonkeyPatch,
    loop: asyncio.AbstractEventLoop,
    networks: tuple[str, ...],
    peer: str,
    expected: dict[str, str],
) -> None:
    # La variable de uvicorn no cuenta: solo las redes de ``VIGIA_FORWARDED_ALLOW_IPS``.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    server = ApiServerConfig(forwarded_allow_ips=networks)
    assert loop.run_until_complete(_seen_from(server, peer)) == expected
