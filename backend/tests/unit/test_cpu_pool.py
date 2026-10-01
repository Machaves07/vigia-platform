"""Pool de CPU acotado y su métrica de espera (PAT-NUC-REN-05, TASK-109).

- Nunca corren más de ``max_workers`` tareas a la vez (4 en el pool del proceso).
- ``cpu_pool_wait_ms`` registra el tiempo en cola medido con el ``Clock`` inyectado.
- Resultado, excepción y variables de contexto llegan como con ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.cpu_pool import (
    CPU_POOL_MAX_WORKERS,
    CPU_POOL_THREAD_PREFIX,
    CpuPool,
    get_cpu_pool,
)
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.observability.redaction import AttributePolicy

START = datetime(2026, 9, 29, tzinfo=UTC)


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def metrics(reader: InMemoryMetricReader) -> PlatformMetrics:
    provider = MeterProvider(metric_readers=[reader])
    return PlatformMetrics(provider.get_meter("pruebas"), AttributePolicy())


def _wait_points(reader: InMemoryMetricReader) -> list[HistogramDataPoint]:
    data = reader.get_metrics_data()
    points: list[HistogramDataPoint] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == MetricName.CPU_POOL_WAIT_MS.value:
                    points.extend(
                        point
                        for point in metric.data.data_points
                        if isinstance(point, HistogramDataPoint)
                    )
    return points


@pytest.fixture
def pool(metrics: PlatformMetrics) -> Iterator[CpuPool]:
    cpu_pool = CpuPool(SystemClock(), metrics=metrics)
    yield cpu_pool
    cpu_pool.shutdown()


def test_process_pool_is_shared_and_bounded_to_four() -> None:
    assert get_cpu_pool() is get_cpu_pool()
    assert get_cpu_pool().max_workers == CPU_POOL_MAX_WORKERS == 4


@pytest.mark.parametrize("workers", [0, -1, True, 1.5])
def test_pool_needs_at_least_one_thread(workers: object) -> None:
    with pytest.raises(ValueError, match="al menos un hilo"):
        CpuPool(SystemClock(), max_workers=workers)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_never_more_than_four_tasks_at_once(pool: CpuPool) -> None:
    lock = threading.Lock()
    running = 0
    peak = 0
    release = threading.Event()

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(timeout=5)
        with lock:
            running -= 1

    tasks = [asyncio.ensure_future(pool.run(work)) for _ in range(3 * CPU_POOL_MAX_WORKERS)]
    await asyncio.sleep(0.2)
    with lock:
        assert running == CPU_POOL_MAX_WORKERS
    release.set()
    await asyncio.gather(*tasks)
    assert peak == CPU_POOL_MAX_WORKERS


@pytest.mark.asyncio
async def test_wait_in_queue_is_measured_with_the_injected_clock(
    metrics: PlatformMetrics, reader: InMemoryMetricReader
) -> None:
    clock = SimulatedClock(START)
    single = CpuPool(clock, max_workers=1, metrics=metrics)
    started = threading.Event()
    release = threading.Event()

    def blocker() -> str:
        started.set()
        release.wait(timeout=5)
        return "primera"

    try:
        first = asyncio.ensure_future(single.run(blocker))
        await asyncio.to_thread(started.wait, 5)
        second = asyncio.ensure_future(single.run(lambda: "segunda"))
        await asyncio.sleep(0)  # la segunda queda en cola con la marca de ahora
        clock.advance(0.25)
        release.set()
        assert await first == "primera"
        assert await second == "segunda"
    finally:
        single.shutdown()

    (point,) = _wait_points(reader)
    assert point.count == 2
    assert point.min == 0
    assert point.max == pytest.approx(250.0)
    assert dict(point.attributes or {}) == {}


@pytest.mark.asyncio
async def test_result_exception_context_and_thread_name(pool: CpuPool) -> None:
    variable: contextvars.ContextVar[str] = contextvars.ContextVar("correlacion")
    variable.set("c-01")

    def inside(suffix: str, *, upper: bool) -> tuple[str, str]:
        value = variable.get() + suffix
        return (value.upper() if upper else value), threading.current_thread().name

    value, thread = await pool.run(inside, "-x", upper=True)
    assert value == "C-01-X"
    assert thread.startswith(CPU_POOL_THREAD_PREFIX)
    assert thread != threading.current_thread().name

    def failing() -> None:
        raise LookupError("fallo en el hilo")

    with pytest.raises(LookupError, match="fallo en el hilo"):
        await pool.run(failing)


@pytest.mark.asyncio
async def test_task_cancelled_before_starting_never_runs(metrics: PlatformMetrics) -> None:
    single = CpuPool(SystemClock(), max_workers=1, metrics=metrics)
    started = threading.Event()
    release = threading.Event()
    ran: list[str] = []
    try:
        first = asyncio.ensure_future(single.run(lambda: started.set() or release.wait(5)))
        await asyncio.to_thread(started.wait, 5)
        queued = asyncio.ensure_future(single.run(lambda: ran.append("no")))
        await asyncio.sleep(0)
        queued.cancel()
        # La cancelación llega al futuro del pool una vuelta del bucle después
        # (``_chain_future`` la programa con ``call_soon``): se libera el hilo solo cuando la
        # tarea terminó cancelada y pasó una vuelta más, nunca antes (VIG-130).
        await asyncio.wait([queued])
        await asyncio.sleep(0)
        release.set()
        await first
        with pytest.raises(asyncio.CancelledError):
            await queued
    finally:
        single.shutdown()
    assert ran == []
