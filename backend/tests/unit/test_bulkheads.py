"""Mamparos por clase de ruta (LC-GOB-20; NFR-GOB-19; TASK-205).

- Lo que U-02 ya entregó del pendiente nº 37 sigue en su sitio: clase de ruta por prefijo, dos
  pools de la raíz de composición con 10 y 5 conexiones sin desbordamiento y espera de 5 s (nota
  de la revisión de VIG-137), clave del limitador por nodo.
- ``BulkheadSettings`` y ``RuntimeConfig``: 35 y 15 por defecto; una reserva de personas por
  debajo del 30 % del total impide arrancar con un error que nombra ``VIGIA_BULKHEAD_PERSON``
  (bordes exactos: 30 % se admite, un puesto menos no).
- ``Bulkheads``: el 36.º nodo se rechaza al instante con ``retry_after_seconds``; la 16.ª persona
  espera y se atiende si se libera un puesto antes de 2 s, y si no recibe el rechazo a los 2 s
  (reloj simulado, nunca la pared); el puesto vuelve ante excepción y cancelación.
- Métricas ``bulkhead_in_use`` y ``bulkhead_size`` por ``pool_class``; espera y rechazos por
  clase.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy.pool import QueuePool

from tests.bulkhead_support import START, ManualTimer, bulkheads_with_reader, gauge, until
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.runtime_support import database_secret, runtime_environ
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.api.middleware import route_class_of
from vigia_platform.shared.bulkheads import (
    PERSON_WAIT_SECONDS,
    RETRY_AFTER_SECONDS,
    Bulkheads,
    BulkheadSaturated,
    BulkheadSettings,
    reserve_problem,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.db import PoolClass, ProcessKind, RouteClass
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.ratelimit import node_key
from vigia_platform.shared.runtime.config import (
    RuntimeConfig,
    RuntimeConfigInvalid,
    report_invalid,
)
from vigia_platform.shared.runtime.core import open_database
from vigia_platform.shared.runtime.db_credentials import (
    DatabaseCredentials,
    parse_database_secret,
)

NODE, PERSON = RouteClass.NODE, RouteClass.PERSON


def _read(**changes: str | None) -> RuntimeConfig:
    return RuntimeConfig.from_environ(runtime_environ(**changes))


# --- Lo ya hecho del pendiente nº 37 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/nodes", NODE),
        ("/api/nodes/findings", NODE),
        ("/api/nodesx", PERSON),
        ("/api/node", PERSON),
        ("/me", PERSON),
        ("/", PERSON),
    ],
)
def test_the_route_class_comes_from_the_node_prefix(path: str, expected: RouteClass) -> None:
    assert route_class_of(path) is expected


def test_the_node_limiter_key_is_per_node_and_operation() -> None:
    node_id = uuid.UUID("0192f0c4-0000-7000-8000-0000000000aa")
    assert node_key(node_id, "findings") == f"node:{node_id}:findings"


class _NoReader:
    async def read(self, secret_id: str) -> str:
        raise AssertionError("abrir los pools no relee el secreto")


def test_the_composition_root_opens_two_pools_of_10_and_5_without_overflow() -> None:
    """Nota de la revisión de VIG-137: los tamaños de la raíz salen de la configuración."""
    config = _read()
    credentials = DatabaseCredentials(
        _NoReader(), config.db_app_secret, parse_database_secret(database_secret())
    )
    database = open_database(config, ProcessKind.API, credentials, metrics_with_reader()[0])
    try:
        pools: dict[PoolClass, Any] = cast(Any, database)._pools
        assert set(pools) == {PoolClass.NODE, PoolClass.PERSON}
        shapes = {}
        for pool_class, pool in pools.items():
            queue = pool.engine.sync_engine.pool
            assert isinstance(queue, QueuePool)
            shapes[pool_class] = (queue.size(), queue._max_overflow, queue._timeout)
        assert shapes == {PoolClass.NODE: (10, 0, 5.0), PoolClass.PERSON: (5, 0, 5.0)}
    finally:
        asyncio.run(database.dispose())


# --- Tamaños y reserva del 30 % ------------------------------------------------------------------


def test_the_design_sizes_are_the_defaults() -> None:
    settings = BulkheadSettings()
    assert (settings.node, settings.person, settings.total) == (35, 15, 50)
    config = _read()
    assert config.bulkheads == settings


@pytest.mark.parametrize(
    ("node", "person", "accepted"),
    [
        (35, 15, True),  # 30 % exacto
        (36, 15, False),  # 29,4 %
        (35, 14, False),
        (7, 3, True),  # 30 % exacto
        (8, 3, False),
        (1, 1, True),
        (1_000, 1_000, True),
        (0, 1, False),  # fuera de límites
        (1, 0, False),
        (1_001, 1_000, False),
        (1_000, 1_001, False),
    ],
)
def test_settings_enforce_bounds_and_the_person_reserve(
    node: int, person: int, accepted: bool
) -> None:
    if accepted:
        assert BulkheadSettings(node=node, person=person).total == node + person
    else:
        with pytest.raises(ValidationError):
            BulkheadSettings(node=node, person=person)


@given(st.integers(1, 1_000), st.integers(1, 1_000))
def test_the_reserve_is_exactly_30_percent_of_the_total(node: int, person: int) -> None:
    ok = person * 10 >= 3 * (node + person)
    assert (reserve_problem(node, person) is None) is ok
    if ok:
        BulkheadSettings(node=node, person=person)
    else:
        with pytest.raises(ValidationError, match="VIGIA_BULKHEAD_PERSON"):
            BulkheadSettings(node=node, person=person)


def test_settings_are_strict() -> None:
    with pytest.raises(ValidationError):
        BulkheadSettings(node="35", person=15)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BulkheadSettings(node=35, person=15, total=50)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BulkheadSettings(node=35.0, person=15)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("node", "person"),
    [("35", "14"), ("36", "15"), ("1000", "15"), ("8", "3"), (None, "14"), ("100", None)],
)
def test_a_person_reserve_below_30_percent_stops_reading_and_names_the_variable(
    node: str | None, person: str | None
) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _read(VIGIA_BULKHEAD_NODE=node, VIGIA_BULKHEAD_PERSON=person)
    assert raised.value.variable == "VIGIA_BULKHEAD_PERSON"
    assert "VIGIA_BULKHEAD_PERSON" in str(raised.value) and "30 %" in str(raised.value)
    stream = io.StringIO()
    assert report_invalid(raised.value, stream)
    assert json.loads(stream.getvalue())["variable"] == "VIGIA_BULKHEAD_PERSON"


@pytest.mark.parametrize(
    ("node", "person"), [("35", "15"), ("7", "3"), ("1", "1"), ("1000", "1000"), ("1", "1000")]
)
def test_sizes_at_the_reserve_boundary_and_the_bounds_are_read(node: str, person: str) -> None:
    config = _read(VIGIA_BULKHEAD_NODE=node, VIGIA_BULKHEAD_PERSON=person)
    assert (config.bulkhead_node, config.bulkhead_person) == (int(node), int(person))
    assert config.bulkheads.total == int(node) + int(person)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VIGIA_BULKHEAD_NODE", "0"),
        ("VIGIA_BULKHEAD_NODE", "1001"),
        ("VIGIA_BULKHEAD_PERSON", "0"),
        ("VIGIA_BULKHEAD_PERSON", "1001"),
        ("VIGIA_BULKHEAD_PERSON", "-15"),
        ("VIGIA_BULKHEAD_PERSON", "15.0"),
    ],
)
def test_sizes_out_of_bounds_name_their_variable(variable: str, value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        _read(**{variable: value})
    assert raised.value.variable == variable


def test_a_config_built_directly_also_enforces_the_reserve() -> None:
    values = RuntimeConfig.from_environ(runtime_environ()).model_dump()
    with pytest.raises(ValidationError, match="VIGIA_BULKHEAD_PERSON"):
        RuntimeConfig.model_validate({**values, "bulkhead_person": 14})


def test_vigia_api_does_not_start_with_a_short_person_reserve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vigia_platform.shared.api import main as api_main
    from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE

    for name, value in runtime_environ(VIGIA_BULKHEAD_PERSON="14").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("VIGIA_PUBLIC_ORIGIN", "https://app.vigia.test")
    monkeypatch.setenv(
        api_main.RUNTIME_VARIABLE, "vigia_platform.shared.runtime.api:build_api_runtime"
    )
    assert api_main.main([]) == STARTUP_FAILURE_EXIT_CODE
    lines = [line for line in capsys.readouterr().err.splitlines() if "config_invalid" in line]
    assert [json.loads(line)["variable"] for line in lines] == ["VIGIA_BULKHEAD_PERSON"]


# --- Comportamiento del semáforo -----------------------------------------------------------------


def test_the_36th_node_is_rejected_at_once_and_a_person_is_still_served() -> None:
    async def scenario() -> None:
        bulkheads, timer, reader = bulkheads_with_reader()
        async with contextlib.AsyncExitStack() as held:
            for _ in range(35):
                await held.enter_async_context(bulkheads.slot(NODE))
            assert bulkheads.in_use(NODE) == 35
            with pytest.raises(BulkheadSaturated) as raised:
                async with bulkheads.slot(NODE):
                    raise AssertionError("el 36.º nodo no debe entrar")
            assert raised.value.route_class is NODE
            assert raised.value.retry_after_seconds == RETRY_AFTER_SECONDS
            assert timer.clock.monotonic() == 0  # rechazo inmediato, sin espera
            async with bulkheads.slot(PERSON):
                assert bulkheads.in_use(PERSON) == 1
        assert bulkheads.in_use(NODE) == bulkheads.in_use(PERSON) == 0
        rejected = metric_points(reader, MetricName.BULKHEAD_REJECTED_TOTAL)
        assert rejected == [({"pool_class": "node"}, 1.0)]

    asyncio.run(scenario())


def test_the_rejection_is_temporarily_unavailable_never_rate_limited() -> None:
    error = translate(BulkheadSaturated(NODE, retry_after_seconds=RETRY_AFTER_SECONDS))
    assert error.code is ApiErrorCode.TEMPORARILY_UNAVAILABLE
    assert error.retry_after_seconds == RETRY_AFTER_SECONDS
    assert 1 <= RETRY_AFTER_SECONDS <= 60


@pytest.mark.parametrize("retry", [0, 61, 5.0, True])
def test_the_retry_after_of_the_bulkhead_is_an_integer_from_1_to_60(retry: Any) -> None:
    with pytest.raises(ValueError, match="retry_after_seconds"):
        Bulkheads(clock=SimulatedClock(START), retry_after_seconds=retry)


def test_the_16th_person_waits_and_is_served_if_a_slot_frees_within_2_seconds() -> None:
    async def scenario() -> None:
        bulkheads, timer, reader = bulkheads_with_reader()
        release = asyncio.Event()
        served = asyncio.Event()

        async def hold() -> None:
            async with bulkheads.slot(PERSON):
                await release.wait()

        async def sixteenth() -> None:
            async with bulkheads.slot(PERSON):
                served.set()

        holders = [asyncio.create_task(hold()) for _ in range(15)]
        await until(lambda: bulkheads.in_use(PERSON) == 15)
        waiting = asyncio.create_task(sixteenth())
        await until(lambda: timer.sleeping == 1)
        timer.advance(PERSON_WAIT_SECONDS - 0.001)
        await asyncio.sleep(0)
        assert not served.is_set() and not waiting.done()
        release.set()  # un puesto se libera antes de los 2 s
        await until(waiting.done)
        await waiting
        assert served.is_set()
        await asyncio.gather(*holders)
        assert bulkheads.in_use(PERSON) == 0
        assert metric_points(reader, MetricName.BULKHEAD_REJECTED_TOTAL) == []

    asyncio.run(scenario())


def test_the_16th_person_is_rejected_after_2_seconds_without_a_free_slot() -> None:
    async def scenario() -> None:
        bulkheads, timer, reader = bulkheads_with_reader()
        release = asyncio.Event()

        async def hold() -> None:
            async with bulkheads.slot(PERSON):
                await release.wait()

        async def sixteenth() -> None:
            async with bulkheads.slot(PERSON):
                raise AssertionError("sin puesto libre no entra")

        holders = [asyncio.create_task(hold()) for _ in range(15)]
        await until(lambda: bulkheads.in_use(PERSON) == 15)
        waiting = asyncio.create_task(sixteenth())
        await until(lambda: timer.sleeping == 1)
        timer.advance(PERSON_WAIT_SECONDS)
        await until(waiting.done)
        with pytest.raises(BulkheadSaturated) as raised:
            await waiting
        assert raised.value.route_class is PERSON
        assert timer.clock.monotonic() == PERSON_WAIT_SECONDS
        release.set()
        await asyncio.gather(*holders)
        assert bulkheads.in_use(PERSON) == 0
        # La espera rechazada no se quedó con ningún puesto: caben otra vez 15.
        async with contextlib.AsyncExitStack() as held:
            for _ in range(15):
                await held.enter_async_context(bulkheads.slot(PERSON))
        assert metric_points(reader, MetricName.BULKHEAD_REJECTED_TOTAL) == [
            ({"pool_class": "person"}, 1.0)
        ]

    asyncio.run(scenario())


def test_persons_never_take_node_slots_and_nodes_never_take_person_slots() -> None:
    async def scenario() -> None:
        bulkheads, _, _ = bulkheads_with_reader()
        async with contextlib.AsyncExitStack() as held:
            for _ in range(15):
                await held.enter_async_context(bulkheads.slot(PERSON))
            for _ in range(35):
                await held.enter_async_context(bulkheads.slot(NODE))
            assert (bulkheads.in_use(NODE), bulkheads.in_use(PERSON)) == (35, 15)

    asyncio.run(scenario())


def test_a_slot_returns_when_the_block_raises() -> None:
    async def scenario() -> None:
        bulkheads, _, reader = bulkheads_with_reader()
        for route_class in RouteClass:
            with pytest.raises(RuntimeError):
                async with bulkheads.slot(route_class):
                    raise RuntimeError("fallo del manejador")
            assert bulkheads.in_use(route_class) == 0
            assert gauge(reader, MetricName.BULKHEAD_IN_USE, route_class) == 0

    asyncio.run(scenario())


def test_a_cancelled_holder_returns_its_slot() -> None:
    async def scenario() -> None:
        bulkheads, _, reader = bulkheads_with_reader()
        entered = asyncio.Event()

        async def hold() -> None:
            async with bulkheads.slot(NODE):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        assert gauge(reader, MetricName.BULKHEAD_IN_USE, NODE) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bulkheads.in_use(NODE) == 0
        assert gauge(reader, MetricName.BULKHEAD_IN_USE, NODE) == 0

    asyncio.run(scenario())


def test_a_person_cancelled_while_waiting_takes_no_slot() -> None:
    async def scenario() -> None:
        bulkheads, timer, _ = bulkheads_with_reader(node=7, person=3)
        release = asyncio.Event()

        async def hold() -> None:
            async with bulkheads.slot(PERSON):
                await release.wait()

        holders = [asyncio.create_task(hold()) for _ in range(3)]
        await until(lambda: bulkheads.in_use(PERSON) == 3)
        waiting = asyncio.create_task(hold())
        await until(lambda: timer.sleeping == 1)
        waiting.cancel()
        await until(waiting.done)
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert timer.sleeping == 0
        release.set()
        await asyncio.gather(*holders)
        assert bulkheads.in_use(PERSON) == 0
        async with contextlib.AsyncExitStack() as held:
            for _ in range(3):
                await held.enter_async_context(bulkheads.slot(PERSON))

    asyncio.run(scenario())


def test_a_person_cancelled_just_as_a_slot_frees_does_not_keep_it() -> None:
    """El puesto se libera y, antes de que el bucle dé otra vuelta, la espera se cancela: la
    espera ya despertada no se queda con el puesto ni lo devuelve dos veces."""

    async def scenario() -> None:
        bulkheads, timer, _ = bulkheads_with_reader(node=7, person=3)
        semaphore = cast(Any, bulkheads)._semaphores[PERSON]
        held = contextlib.AsyncExitStack()
        for _ in range(3):
            await held.enter_async_context(bulkheads.slot(PERSON))
        entered = asyncio.Event()

        async def wait_for_a_slot() -> None:
            async with bulkheads.slot(PERSON):
                entered.set()

        waiting = asyncio.create_task(wait_for_a_slot())
        await until(lambda: timer.sleeping == 1)
        await held.aclose()  # libera los tres puestos sin ceder el bucle
        waiting.cancel()
        await until(waiting.done)
        assert waiting.cancelled() and not entered.is_set()
        assert bulkheads.in_use(PERSON) == 0
        assert semaphore._value == 3  # ni un puesto perdido ni uno de más

    asyncio.run(scenario())


def test_the_route_class_must_be_a_route_class() -> None:
    async def scenario() -> None:
        bulkheads, _, _ = bulkheads_with_reader()
        with pytest.raises(TypeError):
            async with bulkheads.slot("node"):  # type: ignore[arg-type]
                pass

    asyncio.run(scenario())


# --- Métricas ------------------------------------------------------------------------------------


def test_size_and_in_use_are_published_per_pool_class() -> None:
    async def scenario() -> None:
        bulkheads, _, reader = bulkheads_with_reader()
        assert gauge(reader, MetricName.BULKHEAD_SIZE, NODE) == 35
        assert gauge(reader, MetricName.BULKHEAD_SIZE, PERSON) == 15
        async with bulkheads.slot(NODE), bulkheads.slot(NODE), bulkheads.slot(PERSON):
            assert gauge(reader, MetricName.BULKHEAD_IN_USE, NODE) == 2
            assert gauge(reader, MetricName.BULKHEAD_IN_USE, PERSON) == 1
        assert gauge(reader, MetricName.BULKHEAD_IN_USE, NODE) == 0
        assert gauge(reader, MetricName.BULKHEAD_IN_USE, PERSON) == 0

    asyncio.run(scenario())


def test_the_wait_is_a_histogram_per_class_never_per_node() -> None:
    async def scenario() -> None:
        bulkheads, timer, reader = bulkheads_with_reader(node=7, person=3)
        async with contextlib.AsyncExitStack() as held:
            for _ in range(3):
                await held.enter_async_context(bulkheads.slot(PERSON))
            waiting = asyncio.create_task(held.enter_async_context(bulkheads.slot(PERSON)))
            await until(lambda: timer.sleeping == 1)
            timer.advance(PERSON_WAIT_SECONDS)
            await until(waiting.done)
            with pytest.raises(BulkheadSaturated):
                await waiting
            async with bulkheads.slot(NODE):
                pass
        data = reader.get_metrics_data()
        assert data is not None
        histograms = {
            tuple(sorted((point.attributes or {}).items())): (point.count, point.max)
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == MetricName.BULKHEAD_WAIT_MS.value
            for point in metric.data.data_points
        }
        assert histograms == {
            (("pool_class", "person"),): (4, PERSON_WAIT_SECONDS * 1000),
            (("pool_class", "node"),): (1, 0.0),
        }

    asyncio.run(scenario())


def test_metrics_never_carry_a_node_or_a_path() -> None:
    timer = ManualTimer()
    bulkheads, _, reader = bulkheads_with_reader(timer=timer)

    async def scenario() -> None:
        async with bulkheads.slot(NODE):
            pass

    asyncio.run(scenario())
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name.startswith("bulkhead_"):
                    for point in metric.data.data_points:
                        assert set(point.attributes or {}) == {"pool_class"}
