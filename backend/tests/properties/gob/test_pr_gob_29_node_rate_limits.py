"""PR-GOB-29: límites de tasa de las rutas del contrato (NFR-GOB-33 y su nota; BR-GOB-62; TASK-206).

Sobre ``NodeRateLimits`` con el cubo de fichas real de U-02 y un reloj simulado:

- **por sujeto** (``node:<node_id>:<operación>``) y **por origen** (``enrollment:<hmac>:…``), en
  cualquier ventana deslizante una instancia acepta **al menos** el presupuesto si se le pide más
  deprisa que su reposición y **nunca más** del presupuesto más la ráfaga;
- todo ``rate_limited`` lleva ``retry_after_seconds`` entre **1 y 60** (el cubo admite hasta
  3 600) y, cuando la espera real cabe en 60 s, pasada esa espera la siguiente entra;
- el **mínimo de NFR-CTR-02 nunca se limita**: un nodo que no pasa de 60 registros en ningún
  minuto deslizante, o que pide concesiones al ritmo de 240 por minuto, nunca recibe
  ``rate_limited``;
- hallazgos, detecciones y eventos comparten un cubo; cada nodo y cada origen tienen el suyo; la
  clave del origen nunca lleva la dirección;
- el **freno global** (``node_rate_brake_set`` en la auditoría) acota el total de la instancia con
  su caché de 10 s, y la métrica ``rate_limited_total`` lleva la causa (``node``, ``origin`` o
  ``brake``).

Semilla registrada por el perfil (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from vigia_contracts.models.enumerations import RejectionCode

from tests.signing_support import START
from vigia_platform.node_api.limits import (
    BRAKE_CACHE_SECONDS,
    ENROLLMENT_NODE_BUDGET,
    ENROLLMENT_ORIGIN_BUDGETS,
    MINIMUM_PER_MINUTE,
    NODE_BUDGETS,
    EmergencyBrake,
    NodeRateLimits,
    check_minimum,
)
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.observability.metrics import METER_NAME, PlatformMetrics
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.ratelimit import (
    CONTRACT_MAX_RETRY_AFTER_SECONDS,
    EnrollmentWindow,
    RateLimiter,
    brake_filters,
    enrollment_origin_key,
    parse_brake,
)

ORIGIN = "198.51.100.23"
LIMITED_ROUTES = sorted(NODE_BUDGETS, key=lambda route: route.name)


def _limits(clock: SimulatedClock, **kwargs: object) -> NodeRateLimits:
    return NodeRateLimits(RateLimiter(clock), **kwargs)  # type: ignore[arg-type]


def _admit(
    limits: NodeRateLimits, route: NodeRoute, node_id: uuid.UUID | None, address: str = ORIGIN
) -> NodeRejection | None:
    try:
        asyncio.run(limits.admit(route, node_id=node_id, address=address))
    except NodeRejection as rejection:
        return rejection
    return None


def _check_retry(rejection: NodeRejection | None) -> None:
    if rejection is not None:
        assert rejection.code is RejectionCode.RATE_LIMITED
        assert rejection.retryable
        assert rejection.retry_after_seconds is not None
        assert 1 <= rejection.retry_after_seconds <= CONTRACT_MAX_RETRY_AFTER_SECONDS


GAPS = st.lists(
    st.one_of(st.just(0.0), st.floats(0, 120, allow_nan=False, allow_infinity=False)),
    min_size=1,
    max_size=200,
)


# --- Nunca más del presupuesto más la ráfaga -----------------------------------------------------


@given(route=st.sampled_from(LIMITED_ROUTES), gaps=GAPS)
def test_pr_gob_29_per_node_never_more_than_budget_plus_burst_in_any_window(
    route: NodeRoute, gaps: list[float]
) -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    budget = NODE_BUDGETS[route].budget
    accepted: list[float] = []
    for gap in gaps:
        clock.advance(gap)
        rejection = _admit(limits, route, node)
        _check_retry(rejection)
        if rejection is None:
            accepted.append(clock.monotonic())
    for start in accepted:
        inside = [moment for moment in accepted if start <= moment < start + budget.window_seconds]
        assert len(inside) <= budget.limit + budget.capacity


@given(gaps=st.lists(st.floats(0, 4_000, allow_nan=False), min_size=1, max_size=120))
def test_pr_gob_29_per_origin_never_more_than_budget_plus_burst_in_any_window(
    gaps: list[float],
) -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock, origin_secret=b"s" * 32)
    accepted: list[float] = []
    for gap in gaps:
        clock.advance(gap)
        rejection = _admit(limits, NodeRoute.ENROLLMENT, None)
        _check_retry(rejection)
        if rejection is None:
            accepted.append(clock.monotonic())
    for window, budget in ENROLLMENT_ORIGIN_BUDGETS.items():
        for start in accepted:
            inside = [
                moment for moment in accepted if start <= moment < start + budget.window_seconds
            ]
            assert len(inside) <= budget.limit + budget.capacity, window


# --- Al menos el presupuesto bajo demanda --------------------------------------------------------


@given(
    route=st.sampled_from(LIMITED_ROUTES),
    warmup=st.lists(st.floats(0, 30, allow_nan=False), max_size=40),
    density=st.integers(2, 4),
)
def test_pr_gob_29_at_least_the_budget_in_any_window_under_demand(
    route: NodeRoute, warmup: list[float], density: int
) -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    for gap in warmup:
        clock.advance(gap)
        _admit(limits, route, node)
    budget = NODE_BUDGETS[route].budget
    ticks = budget.limit * density
    step = budget.window_seconds / ticks
    accepted: list[int] = []
    for tick in range(ticks * 3 + 1):
        if _admit(limits, route, node) is None:
            accepted.append(tick)
        clock.advance(step)
    for first in range(0, ticks * 2 + 1, max(1, ticks // 4)):
        inside = [tick for tick in accepted if first <= tick <= first + ticks + 1]
        assert len(inside) >= budget.limit, (route, first, len(inside))


def test_pr_gob_29_the_origin_admits_its_quarter_and_its_day() -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock, origin_secret=b"s" * 32)
    quarter = ENROLLMENT_ORIGIN_BUDGETS[EnrollmentWindow.QUARTER]
    day = ENROLLMENT_ORIGIN_BUDGETS[EnrollmentWindow.DAY]
    assert (quarter.limit, quarter.window_seconds, day.limit, day.window_seconds) == (
        5,
        900,
        20,
        86_400,
    )
    results = [_admit(limits, NodeRoute.ENROLLMENT, None) for _ in range(6)]
    assert results[:5] == [None] * 5 and results[5] is not None
    _check_retry(results[5])
    accepted = 5
    for _ in range(40):
        clock.advance(quarter.window_seconds / quarter.limit)
        if _admit(limits, NodeRoute.ENROLLMENT, None) is None:
            accepted += 1
    # En las 2 horas siguientes manda el tope del día: su ráfaga de 20 más lo que repone en ese
    # tiempo (20 por día, continuo: menos de 2 fichas), aunque el de 15 minutos dejaría 45.
    refill = int(day.limit * 40 * quarter.window_seconds / quarter.limit / day.window_seconds)
    assert day.capacity <= accepted <= day.capacity + refill + 1


def test_pr_gob_29_enrollment_by_node_id_is_5_every_15_minutes() -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    assert (ENROLLMENT_NODE_BUDGET.budget.limit, ENROLLMENT_NODE_BUDGET.budget.window_seconds) == (
        5,
        900,
    )
    for _ in range(5):
        limits.admit_enrollment_node(node)
    with pytest.raises(NodeRejection) as raised:
        limits.admit_enrollment_node(node)
    _check_retry(raised.value)
    limits.admit_enrollment_node(uuid.uuid4())  # otro nodo, otro cubo


# --- retry_after_seconds -------------------------------------------------------------------------


@given(route=st.sampled_from(LIMITED_ROUTES), gaps=GAPS)
def test_pr_gob_29_retry_after_is_between_1_and_60_and_honest_when_it_fits(
    route: NodeRoute, gaps: list[float]
) -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    budget = NODE_BUDGETS[route].budget
    for gap in gaps:
        clock.advance(gap)
        rejection = _admit(limits, route, node)
        _check_retry(rejection)
        # Espera real hasta la siguiente ficha: window / limit (rotación, 30 min; latido, 15 s).
        if rejection is not None and budget.window_seconds / budget.limit <= 60:
            assert rejection.retry_after_seconds is not None
            clock.advance(rejection.retry_after_seconds)
            assert _admit(limits, route, node) is None


def test_pr_gob_29_hourly_budgets_clamp_retry_after_to_60() -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    for _ in range(2):
        assert _admit(limits, NodeRoute.CREDENTIAL_ROTATION, node) is None
    rejection = _admit(limits, NodeRoute.CREDENTIAL_ROTATION, node)
    assert rejection is not None and rejection.retry_after_seconds == 60


# --- Mínimo de NFR-CTR-02 ------------------------------------------------------------------------


def test_no_budget_is_below_the_nfr_ctr_02_minimum() -> None:
    assert check_minimum() == []
    assert MINIMUM_PER_MINUTE == {"ingest": 60, "clip_upload": 240}
    ingest = NODE_BUDGETS[NodeRoute.FINDING].budget
    grants = NODE_BUDGETS[NodeRoute.CLIP_UPLOAD].budget
    assert (ingest.limit, ingest.capacity, grants.limit, grants.capacity) == (240, 60, 480, 120)


def _sixty_per_sliding_minute(moments: Sequence[float]) -> bool:
    return all(
        sum(1 for other in moments if start <= other < start + 60) <= 60 for start in moments
    )


@given(
    gaps=st.lists(
        st.one_of(st.just(0.0), st.floats(0, 90, allow_nan=False, allow_infinity=False)),
        min_size=1,
        max_size=240,
    ),
    routes=st.lists(
        st.sampled_from(
            [NodeRoute.FINDING, NodeRoute.DETECTION_REVIEW, NodeRoute.OBSERVABILITY_EVENT]
        ),
        min_size=240,
        max_size=240,
    ),
)
def test_pr_gob_29_sixty_records_in_any_sliding_minute_are_never_limited(
    gaps: list[float], routes: list[NodeRoute]
) -> None:
    moments: list[float] = []
    moment = 0.0
    for gap in gaps:
        moment += gap
        moments.append(moment)
    # Solo las secuencias que respetan el mínimo: ≤ 60 registros en cualquier minuto deslizante.
    kept: list[float] = []
    for candidate in moments:
        if _sixty_per_sliding_minute([*kept, candidate]):
            kept.append(candidate)
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    previous = 0.0
    for index, candidate in enumerate(kept):
        clock.advance(candidate - previous)
        previous = candidate
        assert _admit(limits, routes[index], node) is None, (index, candidate)


@given(jitter=st.lists(st.floats(0, 2, allow_nan=False), min_size=1, max_size=600))
def test_pr_gob_29_grants_at_240_per_minute_are_never_limited(jitter: list[float]) -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()
    for extra in jitter:
        assert _admit(limits, NodeRoute.CLIP_UPLOAD, node) is None
        clock.advance(60 / MINIMUM_PER_MINUTE["clip_upload"] + extra)


# --- Claves ---------------------------------------------------------------------------------------


def test_the_three_records_share_one_bucket_and_each_node_has_its_own() -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node, other = uuid.uuid4(), uuid.uuid4()
    for index in range(60):
        route = (NodeRoute.FINDING, NodeRoute.DETECTION_REVIEW, NodeRoute.OBSERVABILITY_EVENT)[
            index % 3
        ]
        assert _admit(limits, route, node) is None
    assert _admit(limits, NodeRoute.OBSERVABILITY_EVENT, node) is not None
    assert _admit(limits, NodeRoute.FINDING, other) is None
    assert _admit(limits, NodeRoute.CLIP_UPLOAD, node) is None  # otra operación, otro cubo


def test_each_origin_has_its_own_bucket_and_the_key_never_holds_the_address() -> None:
    clock = SimulatedClock(START)
    limits = _limits(clock, origin_secret=b"s" * 32)
    for _ in range(5):
        assert _admit(limits, NodeRoute.ENROLLMENT, None, "203.0.113.1") is None
    assert _admit(limits, NodeRoute.ENROLLMENT, None, "203.0.113.1") is not None
    assert _admit(limits, NodeRoute.ENROLLMENT, None, "203.0.113.2") is None
    for window in EnrollmentWindow:
        key = enrollment_origin_key("203.0.113.1", window, secret=b"s" * 32)
        assert "203.0.113.1" not in key and key.endswith(f":{window.value}")
        assert key != enrollment_origin_key("203.0.113.1", window, secret=b"t" * 32)


# --- Freno global ---------------------------------------------------------------------------------


class _Source:
    def __init__(self, value: int | None) -> None:
        self.value = value
        self.reads = 0
        self.fail = False

    async def current(self) -> int | None:
        self.reads += 1
        if self.fail:
            raise ConnectionError("base caída")
        return self.value


@given(per_minute=st.integers(1, 50), nodes=st.integers(1, 6), gaps=GAPS)
def test_pr_gob_29_the_brake_bounds_the_whole_instance(
    per_minute: int, nodes: int, gaps: list[float]
) -> None:
    clock = SimulatedClock(START)
    source = _Source(per_minute)
    limits = _limits(clock, brake=EmergencyBrake(source, clock))
    population = [uuid.uuid4() for _ in range(nodes)]
    accepted: list[float] = []
    for index, gap in enumerate(gaps):
        clock.advance(gap)
        rejection = _admit(limits, NodeRoute.FINDING, population[index % nodes])
        _check_retry(rejection)
        if rejection is None:
            accepted.append(clock.monotonic())
    for start in accepted:
        inside = [moment for moment in accepted if start <= moment < start + 60]
        assert len(inside) <= 2 * per_minute


class _SlowSource(_Source):
    """Fuente que cede el bucle a mitad de lectura: así se solapan las peticiones concurrentes."""

    def __init__(self, value: int | None, release: asyncio.Event) -> None:
        super().__init__(value)
        self.release = release

    async def current(self) -> int | None:
        self.reads += 1
        await self.release.wait()
        return self.value


def test_concurrent_requests_never_pass_more_than_the_burst() -> None:
    # Garantía «nunca más del presupuesto más la ráfaga» con peticiones a la vez en un proceso:
    # el cubo comprueba y consume sin ceder el bucle.
    clock = SimulatedClock(START)
    limits = _limits(clock)
    node = uuid.uuid4()

    async def one() -> bool:
        try:
            await limits.admit(NodeRoute.FINDING, node_id=node, address=ORIGIN)
        except NodeRejection:
            return False
        return True

    async def burst() -> list[bool]:
        return list(await asyncio.gather(*(one() for _ in range(300))))

    results = asyncio.run(burst())
    assert sum(results) == NODE_BUDGETS[NodeRoute.FINDING].budget.capacity == 60


def test_concurrent_requests_read_a_stale_brake_once() -> None:
    # Garantía «una lectura del freno por caché vencida»: 50 peticiones a la vez con la caché
    # vencida leen la auditoría una sola vez (el candado de EmergencyBrake).
    clock = SimulatedClock(START)

    async def scenario() -> tuple[int, list[object]]:
        release = asyncio.Event()
        source = _SlowSource(7, release)
        brake = EmergencyBrake(source, clock)
        tasks = [asyncio.ensure_future(brake.budget()) for _ in range(50)]
        while source.reads == 0:
            await asyncio.sleep(0)
        for _ in range(10):
            await asyncio.sleep(0)  # el resto llega al candado mientras la lectura espera
        release.set()
        budgets = await asyncio.gather(*tasks)
        return source.reads, [budget.limit if budget else None for budget in budgets]

    reads, limits_seen = asyncio.run(scenario())
    assert reads == 1
    assert limits_seen == [7] * 50


def test_the_brake_is_read_with_a_short_cache_and_survives_a_read_failure() -> None:
    clock = SimulatedClock(START)
    source = _Source(None)
    brake = EmergencyBrake(source, clock)
    assert asyncio.run(brake.budget()) is None
    source.value = 1
    clock.advance(BRAKE_CACHE_SECONDS - 0.5)
    assert asyncio.run(brake.budget()) is None and source.reads == 1
    clock.advance(0.5)
    budget = asyncio.run(brake.budget())
    assert budget is not None and budget.limit == 1 and source.reads == 2
    source.fail = True
    clock.advance(BRAKE_CACHE_SECONDS)
    assert asyncio.run(brake.budget()) == budget  # el último valor leído


@pytest.mark.parametrize(
    ("filters", "value"),
    [
        ({"per_minute": "off"}, None),
        ({"per_minute": "120"}, 120),
        ({"per_minute": "0"}, None),
        ({"per_minute": "-1"}, None),
        ({"per_minute": "1e3"}, None),
        ({"per_minute": "١٢"}, None),
        ({"per_minute": 120}, None),
        ({}, None),
        ("120", None),
    ],
)
def test_the_brake_filters_round_trip(filters: object, value: int | None) -> None:
    assert parse_brake(filters) == value
    if value is not None:
        assert parse_brake(brake_filters(value)) == value
    assert parse_brake(brake_filters(None)) is None


def test_rate_limited_is_measured_by_cause() -> None:
    reader = InMemoryMetricReader()
    metrics = PlatformMetrics(
        MeterProvider(metric_readers=[reader]).get_meter(METER_NAME), AttributePolicy()
    )
    clock = SimulatedClock(START)
    limits = _limits(
        clock, brake=EmergencyBrake(_Source(1), clock), metrics=metrics, origin_secret=b"s" * 32
    )
    node = uuid.uuid4()
    assert _admit(limits, NodeRoute.HEARTBEAT, node) is None
    assert _admit(limits, NodeRoute.HEARTBEAT, node) is not None  # freno (1 por minuto)
    clock.advance(60)
    no_brake = _limits(clock, metrics=metrics, origin_secret=b"s" * 32)
    for _ in range(5):
        _admit(no_brake, NodeRoute.HEARTBEAT, node)  # el quinto latido: nodo
    for _ in range(6):
        _admit(no_brake, NodeRoute.ENROLLMENT, None)  # el sexto intento: origen
    data = reader.get_metrics_data()
    causes: dict[str, int] = {}
    for resource in data.resource_metrics if data is not None else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "rate_limited_total":
                    for point in metric.data.data_points:
                        cause = str(point.attributes.get("rate_limit"))
                        causes[cause] = causes.get(cause, 0) + int(point.value)
    assert causes == {"brake": 1, "node": 1, "origin": 1}
