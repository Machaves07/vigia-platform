"""FS-NUC-06 a nivel de módulo: servicio de filtradas lento o caído (LC-NUC-01, TASK-122).

"Latencia inyectada por encima de 3 s y luego rechazo de conexión → respaldo local;
``hibp_fallback_used``; ninguna alta bloqueada ni contraseña filtrada de la lista local aceptada."

- Con sockets reales (servidor local de asyncio): un servidor que acepta y no responde, uno que
  envía las cabeceras y gotea el cuerpo, y un puerto cerrado. La consulta termina en el tope de
  3 s **total** (no por tramo), se mide la duración contra ese tope y la decisión es la del
  respaldo local: nunca se rechaza ni se acepta por la caída.
- Con el servicio simulado (``tests/hibp_service.py``): respuestas que no son un rango (estado,
  cuerpo, tamaño, compresión), el cortacircuito (abre, semiabierto con una prueba, vuelve a
  abrir) y la cancelación.
- El respaldo local: formato estricto, vacío, ausente; y la ruta por defecto.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.hibp_service import (
    START,
    FakeRangeService,
    checker,
    local_list,
    metric_total,
    metrics_with_reader,
    streamed,
)
from vigia_platform.identity.adapters.hibp import (
    BREAKER_FAILURE_THRESHOLD,
    BREAKER_OPEN_SECONDS,
    DEFAULT_LOCAL_LIST_PATH,
    HIBP_RANGE_URL,
    MAX_RESPONSE_BYTES,
    PREFIX_LENGTH,
    TIMEOUT_SECONDS,
    BreakerState,
    CircuitBreaker,
    HibpBreachChecker,
    LocalBreachList,
    sha1_hex,
)
from vigia_platform.identity.auth.passwords import (
    BreachCheck,
    BreachSource,
    PasswordService,
    PolicyViolation,
)
from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.observability.metrics import MetricName

LEAKED = "clave-filtrada-01"
"""Contraseña sintética del respaldo local."""
FRESH = "clave-nueva-y-larga-02"
"""Contraseña sintética que no está en ninguna muestra."""
EMAIL = "persona@planta.test"
MARGIN_SECONDS = 0.75
"""Holgura sobre el tope de 3 s para la planificación del bucle en un equipo cargado."""

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@contextlib.asynccontextmanager
async def tcp_server(handler: Handler) -> AsyncIterator[str]:
    """Servidor HTTP mínimo en 127.0.0.1; devuelve la URL de ``/range/``."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/range/"
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 1)


async def _silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Acepta la conexión, lee la petición y nunca responde."""
    await reader.readuntil(b"\r\n\r\n")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(3600)
    writer.close()


async def _drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Cabeceras al instante y el cuerpo a 1 byte cada 0,4 s: ningún tramo supera 3 s."""
    await reader.readuntil(b"\r\n\r\n")
    body = f"{sha1_hex(FRESH)[5:]}:7\r\n".encode() * 10
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
    await writer.drain()
    with contextlib.suppress(Exception):
        for byte in body:
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0.4)
    writer.close()


def _closed_port_url() -> str:
    """URL a un puerto local sin nadie escuchando: conexión rechazada."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/range/"


def _real_checker(range_url: str) -> tuple[HibpBreachChecker, InMemoryMetricReader]:
    """Adaptador con su propio cliente httpx (el de producción) contra ``range_url``."""
    metrics, reader = metrics_with_reader()
    breach_checker = HibpBreachChecker(
        local_list([LEAKED]), SimulatedClock(START), range_url=range_url, metrics=metrics
    )
    return breach_checker, reader


def _state(breach_checker: HibpBreachChecker) -> BreakerState:
    """Estado del circuito leído de nuevo (sin que mypy lo dé por fijo tras un ``assert``)."""
    return breach_checker.breaker.state


async def _timed(awaitable: Awaitable[BreachCheck]) -> tuple[BreachCheck, float]:
    clock = SystemClock()
    started = clock.monotonic()
    result = await awaitable
    return result, clock.monotonic() - started


# --- FS-NUC-06 con sockets reales ------------------------------------------------------------


@pytest.mark.parametrize("handler", [_silent, _drip], ids=["sin-respuesta", "goteo"])
@pytest.mark.asyncio
async def test_latency_over_three_seconds_falls_back_within_the_cap(handler: Handler) -> None:
    async with tcp_server(handler) as url:
        breach_checker, reader = _real_checker(url)
        try:
            result, elapsed = await _timed(breach_checker.check(FRESH))
            leaked, leaked_elapsed = await _timed(breach_checker.check(LEAKED))
        finally:
            await breach_checker.aclose()
    assert result == BreachCheck(breached=False, source=BreachSource.LOCAL_FALLBACK)
    assert TIMEOUT_SECONDS <= elapsed < TIMEOUT_SECONDS + MARGIN_SECONDS
    # La filtrada del respaldo se rechaza sin esperar al servicio.
    assert leaked == BreachCheck(breached=True, source=BreachSource.LOCAL_LIST)
    assert leaked_elapsed < 0.5
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 1


@pytest.mark.asyncio
async def test_connection_refused_falls_back_immediately() -> None:
    breach_checker, reader = _real_checker(_closed_port_url())
    try:
        result, elapsed = await _timed(breach_checker.check(FRESH))
        leaked = await breach_checker.check(LEAKED)
    finally:
        await breach_checker.aclose()
    assert result == BreachCheck(breached=False, source=BreachSource.LOCAL_FALLBACK)
    assert elapsed < TIMEOUT_SECONDS
    assert leaked == BreachCheck(breached=True, source=BreachSource.LOCAL_LIST)
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 1


@pytest.mark.asyncio
async def test_fs_nuc_06_latency_then_refusal_never_blocks_nor_accepts_a_leak() -> None:
    """El escenario completo con el servicio de contraseñas del módulo: altas con el servicio
    lento y luego caído; ninguna bloqueada, ninguna filtrada del respaldo aceptada."""
    metrics, reader = metrics_with_reader()
    pool = CpuPool(SystemClock(), max_workers=1)
    results = []
    async with tcp_server(_silent) as url:
        breach_checker = HibpBreachChecker(
            local_list([LEAKED]), SimulatedClock(START), range_url=url, metrics=metrics
        )
        service = PasswordService(breach_checker, pool)
        results.append(await service.check_policy(FRESH, EMAIL))
        results.append(await service.check_policy(LEAKED, EMAIL))
        await breach_checker.aclose()
    breach_checker = HibpBreachChecker(
        local_list([LEAKED]), SimulatedClock(START), range_url=_closed_port_url(), metrics=metrics
    )
    service = PasswordService(breach_checker, pool)
    results.append(await service.check_policy(FRESH + "-b", EMAIL))
    results.append(await service.check_policy(LEAKED, EMAIL))
    await breach_checker.aclose()
    pool.shutdown()

    fresh_slow, leaked_slow, fresh_down, leaked_down = results
    for fresh in (fresh_slow, fresh_down):
        assert fresh.ok
        assert fresh.breach_source is BreachSource.LOCAL_FALLBACK
    for leaked in (leaked_slow, leaked_down):
        assert leaked.violations == (PolicyViolation.BREACHED,)
        assert leaked.breach_source is BreachSource.LOCAL_LIST
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 2


@pytest.mark.asyncio
async def test_default_client_answers_from_a_real_socket() -> None:
    """Con el cliente propio del adaptador y un servidor real que responde, decide el servicio."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        prefix = request.split(b" ")[1].rsplit(b"/", 1)[-1].decode()
        assert prefix == sha1_hex(FRESH)[:5]
        body = f"{sha1_hex(FRESH)[5:]}:3\r\n{'0' * 35}:0".encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    async with tcp_server(answer) as url:
        breach_checker, reader = _real_checker(url)
        try:
            result = await breach_checker.check(FRESH)
        finally:
            await breach_checker.aclose()
    assert result == BreachCheck(breached=True, source=BreachSource.REMOTE)
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 0


# --- Respuestas que no son un rango ----------------------------------------------------------


def _bad_responses() -> dict[str, httpx.Response]:
    line = f"{sha1_hex(FRESH)[5:]}:1".encode()
    return {
        # Cuerpos con forma de rango: solo el estado los invalida.
        "503": streamed(503, line),
        "429": streamed(429, line),
        "301": streamed(301, line, headers={"Location": "https://otro.test/"}),
        "204": streamed(204, line),
        "vacio": streamed(200, b""),
        "html": streamed(200, b"<html></html>"),
        "sufijo-corto": streamed(200, line[1:]),
        "minusculas": streamed(200, line.lower()),
        "cuenta-negativa": streamed(200, line.replace(b":1", b":-1")),
        "no-ascii": streamed(200, line + "é".encode()),
        "gzip": streamed(200, line, headers={"Content-Encoding": "gzip"}),
        "demasiado-grande": streamed(200, (line + b"\r\n") * (MAX_RESPONSE_BYTES // 30)),
    }


@pytest.mark.parametrize("case", sorted(_bad_responses()))
@pytest.mark.asyncio
async def test_malformed_answers_fall_back_never_trusted(case: str) -> None:
    metrics, reader = metrics_with_reader()
    service = FakeRangeService()
    service.add([FRESH])  # aunque el servicio "la conozca", una respuesta ilegible no decide

    async def handler(request: httpx.Request) -> httpx.Response:
        service.requests.append(request)
        return _bad_responses()[case]

    breach_checker = HibpBreachChecker(
        local_list([LEAKED]),
        SimulatedClock(START),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=3.0),
        range_url="https://hibp.test/range/",
        metrics=metrics,
    )
    result = await breach_checker.check(FRESH)
    await breach_checker.aclose()
    assert result.source is BreachSource.LOCAL_FALLBACK
    assert result.breached is False
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 1
    assert len(service.requests) == 1  # sin seguir redirecciones


@pytest.mark.asyncio
async def test_padding_entries_do_not_count_as_breached() -> None:
    """Una entrada de relleno (cuenta 0) con el mismo sufijo no es una filtrada."""
    padding = sha1_hex(FRESH)[5:]

    async def handler(request: httpx.Request) -> httpx.Response:
        return streamed(200, f"{padding}:0\r\n{'A' * 35}:9".encode())

    metrics, reader = metrics_with_reader()
    breach_checker = HibpBreachChecker(
        local_list([LEAKED]),
        SimulatedClock(START),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=3.0),
        range_url="https://hibp.test/range/",
        metrics=metrics,
    )
    result = await breach_checker.check(FRESH)
    await breach_checker.aclose()
    assert result == BreachCheck(breached=False, source=BreachSource.REMOTE)
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == 0


# --- Cortacircuito ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breaker_opens_after_three_failures_and_skips_the_network() -> None:
    clock = SimulatedClock(START)
    metrics, reader = metrics_with_reader()
    service = FakeRangeService(mode="down")
    breach_checker = checker(service, local_list([LEAKED]), clock=clock, metrics=metrics)
    for _ in range(BREAKER_FAILURE_THRESHOLD):
        assert (await breach_checker.check(FRESH)).source is BreachSource.LOCAL_FALLBACK
    assert _state(breach_checker) is BreakerState.OPEN
    assert len(service.requests) == BREAKER_FAILURE_THRESHOLD

    service.mode = "up"
    clock.advance(BREAKER_OPEN_SECONDS - 0.001)
    assert (await breach_checker.check(FRESH)).source is BreachSource.LOCAL_FALLBACK
    assert len(service.requests) == BREAKER_FAILURE_THRESHOLD  # abierto: no sale a la red
    assert metric_total(reader, MetricName.HIBP_FALLBACK_USED) == BREAKER_FAILURE_THRESHOLD + 1

    clock.advance(0.001)
    assert (await breach_checker.check(FRESH)).source is BreachSource.REMOTE  # prueba y cierra
    assert _state(breach_checker) is BreakerState.CLOSED
    await breach_checker.aclose()


@pytest.mark.asyncio
async def test_two_failures_do_not_open_and_a_success_resets_the_count() -> None:
    service = FakeRangeService(mode="down")
    breach_checker = checker(service, local_list([LEAKED]))
    for mode in ("down", "down", "up", "down", "down"):
        service.mode = mode
        await breach_checker.check(FRESH)
    assert _state(breach_checker) is BreakerState.CLOSED
    await breach_checker.aclose()


@pytest.mark.asyncio
async def test_half_open_probe_failure_reopens_for_another_period() -> None:
    clock = SimulatedClock(START)
    service = FakeRangeService(mode="down")
    breach_checker = checker(service, local_list([LEAKED]), clock=clock)
    for _ in range(BREAKER_FAILURE_THRESHOLD):
        await breach_checker.check(FRESH)
    clock.advance(BREAKER_OPEN_SECONDS)
    await breach_checker.check(FRESH)  # la prueba falla
    assert _state(breach_checker) is BreakerState.OPEN
    requests = len(service.requests)
    clock.advance(BREAKER_OPEN_SECONDS - 1)
    await breach_checker.check(FRESH)
    assert len(service.requests) == requests
    await breach_checker.aclose()


@pytest.mark.asyncio
async def test_half_open_lets_a_single_probe_through() -> None:
    clock = SimulatedClock(START)
    service = FakeRangeService(mode="slow", delay=0.2)
    breach_checker = checker(service, local_list([LEAKED]), clock=clock)
    for _ in range(BREAKER_FAILURE_THRESHOLD):
        breach_checker.breaker.record_failure()
    clock.advance(BREAKER_OPEN_SECONDS)
    service.mode = "slow"
    results = await asyncio.gather(*(breach_checker.check(FRESH) for _ in range(5)))
    assert len(service.requests) == 1
    assert [r.source for r in results].count(BreachSource.REMOTE) == 1
    assert [r.source for r in results].count(BreachSource.LOCAL_FALLBACK) == 4
    assert _state(breach_checker) is BreakerState.CLOSED
    await breach_checker.aclose()


@pytest.mark.asyncio
async def test_cancelled_probe_releases_the_half_open_slot() -> None:
    clock = SimulatedClock(START)
    service = FakeRangeService(mode="slow", delay=10)
    breach_checker = checker(service, local_list([LEAKED]), clock=clock)
    for _ in range(BREAKER_FAILURE_THRESHOLD):
        breach_checker.breaker.record_failure()
    clock.advance(BREAKER_OPEN_SECONDS)
    probe = asyncio.create_task(breach_checker.check(FRESH))
    await asyncio.sleep(0.05)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    service.mode = "up"
    assert (await breach_checker.check(FRESH)).source is BreachSource.REMOTE
    await breach_checker.aclose()


def test_breaker_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(SimulatedClock(START), failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(SimulatedClock(START), open_seconds=0)
    with pytest.raises(ValueError):
        HibpBreachChecker(local_list([LEAKED]), SimulatedClock(START), timeout_seconds=0)


# --- Registro del respaldo -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_log_has_the_reason_and_nothing_of_the_secret() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    configure_logging("INFO", stream=stream)
    try:
        breach_checker = checker(FakeRangeService(mode="down"), local_list([LEAKED]))
        await breach_checker.check(FRESH)
        await breach_checker.aclose()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved[0]:
            root.addHandler(handler)
        root.setLevel(saved[1])
    [line] = [json.loads(text) for text in stream.getvalue().splitlines()]
    assert line["message"] == "consulta de filtradas resuelta con el respaldo local"
    assert line["reason"] == "hibp_unavailable"
    assert line["level"] == "WARNING"
    raw = stream.getvalue()
    assert FRESH not in raw
    assert sha1_hex(FRESH)[:5] not in raw


# --- Respaldo local ---------------------------------------------------------------------------


def test_local_list_loads_a_well_formed_file(tmp_path: Path) -> None:
    path = tmp_path / "top.txt"
    digests = sorted(sha1_hex(f"sintetica-{n:03d}") for n in range(50))
    path.write_text("".join(f"{digest}\n" for digest in digests), encoding="ascii")
    loaded = LocalBreachList.from_file(path)
    assert len(loaded) == 50
    assert sha1_hex("sintetica-007") in loaded
    assert sha1_hex("sintetica-999") not in loaded
    assert sha1_hex("sintetica-007").lower() not in loaded
    assert 12345 not in loaded


@pytest.mark.parametrize(
    "content",
    [
        "",
        "\n\n",
        "abc\n",
        f"{'a' * 40}\n",  # minúsculas
        f"{'A' * 39}\n",
        f"{'A' * 41}\n",
        f"{'A' * 40}:12\n",
        f" {'A' * 40}\n",
        f"{'A' * 40}\t\n",
        f"{'A' * 40}\n{'G' * 40}\n",
        chr(0xFF21) * 40 + "\n",  # «A» de ancho completo: no es ASCII
    ],
    ids=[
        "vacio",
        "lineas-vacias",
        "texto",
        "minusculas",
        "39",
        "41",
        "cuenta",
        "espacio",
        "tabulador",
        "no-hex",
        "ancho-completo",
    ],
)
def test_local_list_rejects_malformed_or_empty_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "top.txt"
    path.write_text(content, encoding="utf-8", newline="")
    with pytest.raises(ValueError):  # UnicodeDecodeError también es un ValueError
        LocalBreachList.from_file(path)


def test_local_list_accepts_crlf_line_ends(tmp_path: Path) -> None:
    """Un archivo con CRLF (copia desde Windows) se lee igual: el contenido sigue estricto."""
    path = tmp_path / "top.txt"
    path.write_text(f"{sha1_hex(LEAKED)}\r\n", encoding="ascii", newline="")
    assert sha1_hex(LEAKED) in LocalBreachList.from_file(path)


def test_local_list_missing_file_fails_at_startup(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        LocalBreachList.from_file(tmp_path / "no-existe.txt")


def test_design_values_are_pinned() -> None:
    """Valores del diseño (NFR-NUC-26, NFR-NUC-36): cambiarlos es cambiar el diseño."""
    assert TIMEOUT_SECONDS == 3.0
    assert PREFIX_LENGTH == 5
    assert HIBP_RANGE_URL == "https://api.pwnedpasswords.com/range/"


def test_default_local_list_path_is_backend_resources() -> None:
    backend = Path(__file__).resolve().parents[2]
    assert backend / "resources" / "pwned-top100k.txt" == DEFAULT_LOCAL_LIST_PATH
