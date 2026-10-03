"""Banco de la bandeja de salida de NFR-NUC-01 (TASK-142, VIG-91): de la confirmación al consumidor.

Sobre PostgreSQL 16 en contenedor, como ``vigia_app``. El despachador corre su bucle real
(``Dispatcher.run``) con la espera de reposo de producción (1 s entre rondas sin trabajo) para el
consumidor ``dispatch_plain``; cada ronda del banco publica un evento en su transacción y mide
hasta que el manejador lo recibe. La medida incluye la confirmación de la publicación (unos
milisegundos), así que es una cota superior de «entrega de un evento de la bandeja al consumidor
desde la confirmación» (objetivo p95 5 s). El bucle del despachador solo avanza mientras corre
la ronda (el bucle de la prueba se detiene entre rondas), así que la cifra no incluye una espera
de reposo completa: es la latencia con el despachador despierto, no la del peor caso de sondeo.

Solo datos generados. Solo corre con ``--hypothesis-profile=nightly``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest

from tests.benchmarks.conftest import Measure
from tests.dispatch_support import PLAIN, DispatchEnvironment, dispatch_environment
from tests.integration.conftest import PostgresEndpoint

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

POLL_SECONDS = 0.002
"""Cada cuánto mira la prueba si el manejador ya recibió el evento."""


@pytest.fixture(scope="module")
def dispatch(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "bench_outbox") as environment:
        yield environment


def test_event_delivery_from_commit_to_consumer(
    dispatch: DispatchEnvironment, measure: Measure
) -> None:
    handler = dispatch.handlers[PLAIN]
    dispatcher = dispatch.dispatcher()
    organization = uuid.uuid4()
    stop = asyncio.Event()
    seen = 0

    async def start() -> asyncio.Task[None]:
        return asyncio.ensure_future(dispatcher.run(PLAIN, stop))

    async def publish_and_wait() -> None:
        nonlocal seen
        (event,) = await dispatch.publish(organization, [uuid.uuid4()])
        while True:
            delivered = handler.invocations[seen:]
            seen += len(delivered)
            if any(invocation.event_id == event.event_id for invocation in delivered):
                return
            await asyncio.sleep(POLL_SECONDS)

    task = dispatch.run(start())
    try:

        def target() -> None:
            dispatch.run(asyncio.wait_for(publish_and_wait(), timeout=30))

        measure(
            "outbox_delivery",
            "Entrega de un evento de la bandeja al consumidor desde la confirmación",
            target,
            objective_ms=5000,
            details={"idle_seconds": 1.0, "includes_publish_commit": True},
        )
    finally:
        stop.set()
        dispatch.run(task)
