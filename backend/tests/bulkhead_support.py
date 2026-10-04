"""Dobles de los mamparos por clase de ruta (LC-GOB-20; TASK-205).

- ``ManualTimer``: el temporizador de la espera de las personas sobre un ``SimulatedClock``.
  ``sleep`` no vuelve hasta que la prueba avanza el reloj con ``advance`` (sin esperar en pared,
  retro 15): la prueba decide si el puesto se libera antes o después de los 2 s.
- ``bulkheads_with_reader``: ``Bulkheads`` con métricas en memoria y su lector.
- ``gauge``: el último valor de un medidor por ``pool_class``.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from vigia_contracts.clock import SimulatedClock

from tests.dispatch_support import metric_points, metrics_with_reader
from vigia_platform.shared.bulkheads import Bulkheads, BulkheadSettings
from vigia_platform.shared.db import RouteClass
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics

__all__ = [
    "START",
    "ManualTimer",
    "bulkheads_with_reader",
    "gauge",
    "until",
]

START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


class ManualTimer:
    """``sleep`` que solo vence cuando la prueba avanza el ``SimulatedClock``."""

    def __init__(self, clock: SimulatedClock | None = None) -> None:
        self.clock = clock if clock is not None else SimulatedClock(START)
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    @property
    def sleeping(self) -> int:
        """Esperas en curso (personas esperando puesto)."""
        return sum(1 for _, future in self._sleepers if not future.done())

    async def sleep(self, seconds: float) -> None:
        deadline = self.clock.monotonic() + seconds
        if seconds <= 0:
            return
        entry = (deadline, asyncio.get_running_loop().create_future())
        self._sleepers.append(entry)
        try:
            await entry[1]
        finally:
            self._sleepers.remove(entry)

    def advance(self, seconds: float) -> None:
        """Avanza el reloj y despierta las esperas vencidas."""
        self.clock.advance(seconds)
        now = self.clock.monotonic()
        for deadline, future in self._sleepers:
            if deadline <= now and not future.done():
                future.set_result(None)


def bulkheads_with_reader(
    *,
    node: int = 35,
    person: int = 15,
    timer: ManualTimer | None = None,
    metrics: PlatformMetrics | None = None,
    reader: InMemoryMetricReader | None = None,
) -> tuple[Bulkheads, ManualTimer, InMemoryMetricReader]:
    timer = timer if timer is not None else ManualTimer()
    if metrics is None or reader is None:
        metrics, reader = metrics_with_reader()
    bulkheads = Bulkheads(
        BulkheadSettings(node=node, person=person),
        clock=timer.clock,
        metrics=metrics,
        sleep=timer.sleep,
    )
    return bulkheads, timer, reader


def gauge(reader: InMemoryMetricReader, name: MetricName, route_class: RouteClass) -> float:
    """Último valor del medidor ``name`` con ``pool_class = route_class``."""
    values = [
        value
        for attributes, value in metric_points(reader, name)
        if attributes == {"pool_class": route_class.value}
    ]
    assert len(values) == 1, values
    return values[0]


async def until(condition: Callable[[], bool], *, rounds: int = 10_000) -> None:
    """Cede el bucle hasta que ``condition`` se cumpla (sin topes de pared, retro 14)."""
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("la condición no se cumplió")
