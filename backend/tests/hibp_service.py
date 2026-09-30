"""Servicio de filtradas simulado para las pruebas de LC-NUC-01 (sin red, datos sintéticos).

``FakeRangeService`` responde ``GET /range/<prefijo>`` como el servicio real (líneas
``<sufijo de 35>:<cuenta>``, con entradas de relleno de cuenta 0) a partir de un conjunto de
contraseñas sintéticas "filtradas", y guarda cada petición para comprobar qué salió. Su modo
(``up``, ``down``, ``slow``, ``bad``) simula la caída: ``down`` rechaza la conexión, ``slow``
espera ``delay`` segundos antes de responder y ``bad`` responde un cuerpo que no es un rango.

``metric_total(reader, name)`` suma los puntos de un contador del lector en memoria.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import httpx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from vigia_platform.identity.adapters.hibp import (
    HibpBreachChecker,
    LocalBreachList,
    sha1_hex,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.observability.redaction import AttributePolicy

START = datetime(2026, 9, 29, tzinfo=UTC)
RANGE_URL = "https://hibp.test/range/"
PADDING_SUFFIX = "0" * 35

Mode = Literal["up", "down", "slow", "bad"]


class ChunkStream(httpx.AsyncByteStream):
    """Cuerpo servido por trozos, con una pausa opcional antes de cada uno (goteo)."""

    def __init__(self, chunks: Iterable[bytes], pause: float = 0.0) -> None:
        self._chunks = list(chunks)
        self._pause = pause

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._pause:
                await asyncio.sleep(self._pause)
            yield chunk


def streamed(
    status: int,
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
    chunk_size: int = 4096,
    pause: float = 0.0,
) -> httpx.Response:
    """Respuesta como la del transporte real: el cuerpo llega como flujo, no leído de antemano
    (con ``content=`` httpx lo da por consumido y ``aiter_raw`` no funcionaría)."""
    chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)] or [b""]
    return httpx.Response(status, headers=headers, stream=ChunkStream(chunks, pause))


@dataclass
class FakeRangeService:
    breached: set[str] = field(default_factory=set)
    mode: Mode = "up"
    delay: float = 10.0
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, passwords: Iterable[str]) -> None:
        self.breached.update(sha1_hex(password) for password in passwords)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("conexión rechazada", request=request)
        if self.mode == "slow":
            await asyncio.sleep(self.delay)
        if self.mode == "bad":
            return streamed(200, b"<html>mantenimiento</html>")
        prefix = request.url.path.rsplit("/", 1)[-1]
        lines = [f"{digest[5:]}:{42}" for digest in sorted(self.breached) if digest[:5] == prefix]
        lines.append(f"{PADDING_SUFFIX}:0")
        return streamed(200, "\r\n".join(lines).encode("ascii"))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=3.0)


def local_list(passwords: Iterable[str]) -> LocalBreachList:
    return LocalBreachList(sha1_hex(password) for password in passwords)


def metrics_with_reader() -> tuple[PlatformMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return PlatformMetrics(provider.get_meter("pruebas"), AttributePolicy()), reader


def metric_total(reader: InMemoryMetricReader, name: MetricName) -> float:
    data = reader.get_metrics_data()
    total = 0.0
    if data is None:
        return total
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    total += sum(
                        point.value
                        for point in metric.data.data_points
                        if isinstance(point, NumberDataPoint)
                    )
    return total


def checker(
    service: FakeRangeService,
    local: LocalBreachList,
    *,
    clock: SimulatedClock | None = None,
    metrics: PlatformMetrics | None = None,
    timeout_seconds: float = 3.0,
) -> HibpBreachChecker:
    return HibpBreachChecker(
        local,
        clock if clock is not None else SimulatedClock(START),
        client=service.client(),
        range_url=RANGE_URL,
        timeout_seconds=timeout_seconds,
        metrics=metrics,
    )
