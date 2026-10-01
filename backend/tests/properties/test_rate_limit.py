"""PR-NUC-44 y límites de tasa de la cadena (LC-NUC-22; PAT-NUC-ESC-03; NFR-NUC-25; nº 10 y 37).

- Cubo de fichas: en cualquier ventana de un minuto un proceso acepta al menos el presupuesto (si
  se le pide) y nunca más del presupuesto más la ráfaga; toda respuesta ``Limited`` lleva
  ``retry_after_seconds`` mayor que cero y, pasado ese tiempo, la siguiente petición entra.
- Claves cerradas (``session:``, ``origin:``, ``public:``, ``node:<node_id>:<operación>``),
  memoria acotada.
- En la cadena: 60 por minuto por origen en públicas, 1 200 por origen en autenticadas, 600 por
  sesión; ``Retry-After`` y ``retry_after_seconds``; métrica ``rate_limited_total``. Los estáticos
  de la lista pública y las rutas de nodos quedan fuera: mil peticiones a ``/assets/*`` desde un
  origen no producen ningún ``429`` de la aplicación.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from vigia_contracts.clock import SimulatedClock

from tests.middleware_support import (
    ORIGIN,
    STORE_ORIGIN,
    Harness,
    cookie_header,
)
from tests.signing_support import START
from vigia_platform.shared.api.app import platform_units
from vigia_platform.shared.observability.metrics import METER_NAME, PlatformMetrics
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.ratelimit import (
    AUTHENTICATED_ORIGIN_BUDGET,
    MAX_RETRY_AFTER_SECONDS,
    PUBLIC_ORIGIN_BUDGET,
    SESSION_BUDGET,
    Allowed,
    Budget,
    Limited,
    RateLimiter,
    node_key,
    origin_key,
    public_key,
    session_key,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "static"
KEY = session_key("a" * 64)
WINDOW = 60.0

# --- PR-NUC-44 · el cubo de fichas ---------------------------------------------------------------

BUDGETS = st.builds(
    Budget,
    limit=st.integers(1, 50),
    window_seconds=st.sampled_from([1, 10, 60]),
    burst=st.one_of(st.none(), st.integers(1, 50)),
)
GAPS = st.lists(
    st.one_of(st.just(0.0), st.floats(0, 5, allow_nan=False, allow_infinity=False)),
    min_size=1,
    max_size=300,
)
"""Separaciones entre peticiones; los ceros forman ráfagas."""


def _run(budget: Budget, gaps: list[float]) -> list[tuple[float, Allowed | Limited]]:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock)
    results: list[tuple[float, Allowed | Limited]] = []
    for gap in gaps:
        clock.advance(gap)
        results.append((clock.monotonic(), limiter.check(KEY, budget)))
    return results


@given(budget=BUDGETS, gaps=GAPS)
def test_pr_nuc_44_never_more_than_budget_plus_burst_in_any_window(
    budget: Budget, gaps: list[float]
) -> None:
    results = _run(budget, gaps)
    accepted = [moment for moment, verdict in results if isinstance(verdict, Allowed)]
    window = budget.window_seconds
    for start in accepted:
        inside = [moment for moment in accepted if start <= moment < start + window]
        assert len(inside) <= budget.limit + budget.capacity
    for _, verdict in results:
        if isinstance(verdict, Limited):
            assert 1 <= verdict.retry_after_seconds <= MAX_RETRY_AFTER_SECONDS


PUBLISHED_SHAPE = st.builds(
    Budget,
    limit=st.integers(1, 50),
    window_seconds=st.sampled_from([1, 10, 60]),
    burst=st.one_of(st.none(), st.integers(50, 100)),
)
"""Presupuestos con la forma de los publicados: ráfaga igual al límite (o mayor). Con una ráfaga
menor que el límite, una demanda discreta pierde las fichas que el cubo lleno no puede guardar."""


@given(budget=PUBLISHED_SHAPE, warmup=GAPS, density=st.integers(2, 8))
def test_pr_nuc_44_at_least_the_budget_in_any_window_under_demand(
    budget: Budget, warmup: list[float], density: int
) -> None:
    # Tras cualquier historia previa, quien pide más deprisa que el presupuesto obtiene al menos
    # el presupuesto en cada ventana de ``window`` segundos. La demanda es una rejilla de paso
    # ``step``: una ficha que aparece entre dos peticiones se usa en la siguiente, así que la
    # ventana medida incluye un paso más (y el nanosegundo de la cuantización del reloj).
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock)
    for gap in warmup:
        clock.advance(gap)
        limiter.check(KEY, budget)
    window = budget.window_seconds
    ticks_per_window = budget.limit * density
    step = window / ticks_per_window
    accepted: list[int] = []
    for tick in range(ticks_per_window * 3 + 1):
        if isinstance(limiter.check(KEY, budget), Allowed):
            accepted.append(tick)
        clock.advance(step)
    for first in range(0, ticks_per_window * 2 + 1, max(1, ticks_per_window // 4)):
        inside = [tick for tick in accepted if first <= tick <= first + ticks_per_window + 1]
        assert len(inside) >= budget.limit, (first, len(inside))


@given(budget=BUDGETS, idle=st.floats(0, 86_400, allow_nan=False))
def test_a_long_idle_period_never_banks_more_than_the_burst(budget: Budget, idle: float) -> None:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock)
    limiter.check(KEY, budget)
    clock.advance(idle)
    burst = [limiter.check(KEY, budget) for _ in range(budget.capacity * 3)]
    assert sum(isinstance(verdict, Allowed) for verdict in burst) <= budget.capacity


@given(budget=BUDGETS, gaps=GAPS)
def test_retry_after_is_honest(budget: Budget, gaps: list[float]) -> None:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock)
    for gap in gaps:
        clock.advance(gap)
        verdict = limiter.check(KEY, budget)
        if isinstance(verdict, Limited):
            clock.advance(verdict.retry_after_seconds)
            assert isinstance(limiter.check(KEY, budget), Allowed)


def test_the_published_budgets() -> None:
    assert (SESSION_BUDGET.limit, SESSION_BUDGET.window_seconds) == (600, 60)
    assert (AUTHENTICATED_ORIGIN_BUDGET.limit, AUTHENTICATED_ORIGIN_BUDGET.window_seconds) == (
        1_200,
        60,
    )
    assert (PUBLIC_ORIGIN_BUDGET.limit, PUBLIC_ORIGIN_BUDGET.window_seconds) == (60, 60)
    for budget in (SESSION_BUDGET, AUTHENTICATED_ORIGIN_BUDGET, PUBLIC_ORIGIN_BUDGET):
        assert budget.capacity == budget.limit


def test_a_fresh_bucket_admits_exactly_its_burst_then_limits() -> None:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock)
    budget = Budget(60)
    assert all(isinstance(limiter.check(KEY, budget), Allowed) for _ in range(60))
    verdict = limiter.check(KEY, budget)
    assert verdict == Limited(1)
    clock.advance(0.999)
    assert isinstance(limiter.check(KEY, budget), Limited)
    clock.advance(0.001)
    assert isinstance(limiter.check(KEY, budget), Allowed)


def test_keys_are_independent() -> None:
    limiter = RateLimiter(SimulatedClock(START))
    budget = Budget(1)
    assert isinstance(limiter.check(session_key("a" * 64), budget), Allowed)
    assert isinstance(limiter.check(session_key("b" * 64), budget), Allowed)
    assert isinstance(limiter.check(session_key("a" * 64), budget), Limited)


# --- Claves cerradas y memoria acotada -----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "session:" + "A" * 64,
        "session:" + "a" * 63,
        "origin:203.0.113.9",
        "public:" + "a" * 64 + "\n",
        "node:" + str(uuid.uuid4()).upper() + ":heartbeat",
        "node:" + str(uuid.uuid4()) + ":Heartbeat",
        "node:" + str(uuid.uuid4()) + ":",
        "user:" + "a" * 64,
        "",
    ],
)
def test_keys_outside_the_closed_list_are_refused(key: str) -> None:
    with pytest.raises(ValueError):
        RateLimiter(SimulatedClock(START)).check(key, Budget(1))


def test_node_keys_for_u03() -> None:
    node_id = uuid.uuid4()
    key = node_key(node_id, "ingest_records")
    assert key == f"node:{node_id}:ingest_records"
    assert isinstance(RateLimiter(SimulatedClock(START)).check(key, Budget(60)), Allowed)
    for operation in ("", "Ingest", "a b", "x" * 65, "op;drop"):
        with pytest.raises(ValueError):
            node_key(node_id, operation)
    with pytest.raises(ValueError):
        node_key("no-es-uuid", "heartbeat")


def test_origin_keys_never_contain_the_address() -> None:
    address = "203.0.113.9"
    for key in (origin_key(address), public_key(address)):
        assert address not in key and len(key.split(":", 1)[1]) == 64
    assert origin_key(address) != origin_key("203.0.113.10")
    assert origin_key(address).split(":")[1] == public_key(address).split(":")[1]
    assert origin_key("x" * 300) == origin_key("unknown") == origin_key(None)
    assert origin_key(" 203.0.113.9 ") == origin_key(address)


def test_memory_is_bounded_and_full_buckets_are_pruned() -> None:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock, max_keys=10, prune_every=1_000_000)
    budget = Budget(1)
    for index in range(50):
        limiter.check(session_key(f"{index:064x}"), budget)
        assert len(limiter) <= 10
    # El cubo recién creado nunca se descarta a sí mismo: su consumo cuenta.
    newest = session_key(f"{49:064x}")
    assert isinstance(limiter.check(newest, budget), Limited)
    clock.advance(WINDOW)
    assert limiter.prune() == 10
    assert len(limiter) == 0


def test_periodic_pruning_drops_idle_buckets() -> None:
    clock = SimulatedClock(START)
    limiter = RateLimiter(clock, prune_every=3)
    limiter.check(session_key("a" * 64), Budget(10))
    clock.advance(WINDOW)
    limiter.check(session_key("b" * 64), Budget(10))
    limiter.check(session_key("b" * 64), Budget(10))
    assert len(limiter) == 1


# --- En la cadena --------------------------------------------------------------------------------


def _metrics() -> tuple[PlatformMetrics, InMemoryMetricReader, AttributePolicy]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    policy = AttributePolicy()
    return PlatformMetrics(provider.get_meter(METER_NAME), policy), reader, policy


def _limited(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    points: dict[tuple[str, str], int] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "rate_limited_total":
                    for point in metric.data.data_points:
                        attributes = dict(point.attributes or {})
                        key = (str(attributes.get("route")), str(attributes.get("rate_limit")))
                        points[key] = int(point.value)
    return points


def _static_app(harness: Harness, **runtime: Any) -> Any:
    return harness.world.app(
        units=(*platform_units(),),
        static_dir=FIXTURE,
        csp_store_origins=(STORE_ORIGIN,),
        public_origin=ORIGIN,
        runtime=runtime,
    )


def test_a_thousand_asset_requests_from_one_origin_get_no_429() -> None:
    harness = Harness()
    with TestClient(_static_app(harness)) as client:
        statuses = [client.get("/assets/index-3f9a1c2b.js").status_code for _ in range(1_000)]
        statuses += [client.get("/assets/no-existe.js").status_code for _ in range(100)]
        statuses += [client.get("/version.json").status_code for _ in range(100)]
        statuses += [client.get("/robots.txt").status_code for _ in range(100)]
        navigate = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}
        statuses += [client.get("/login", headers=navigate).status_code for _ in range(100)]
        statuses += [client.get("/health/ready").status_code for _ in range(100)]
    assert 429 not in statuses
    assert statuses.count(200) == 1_000 + 100 + 100 + 100 + 100


def test_public_routes_allow_60_per_minute_per_origin_with_retry_after_and_metric() -> None:
    harness = Harness()
    metrics, reader, policy = _metrics()
    app = harness.world.app(
        units=(*platform_units(),),
        runtime={"metrics": metrics, "attribute_policy": policy},
        public_origin=ORIGIN,
    )
    with TestClient(app) as client:
        statuses = [client.get("/health/live").status_code for _ in range(60)]
        limited = client.get("/health/live")
        unmatched = client.get("/no-existe")
        harness.world.clock.advance(1.0)
        again = client.get("/health/live")
    assert statuses == [200] * 60
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert limited.json()["retry_after_seconds"] == 1 and limited.headers["Retry-After"] == "1"
    assert unmatched.status_code == 429, "lo que no es ninguna ruta cuenta como público"
    assert again.status_code == 200
    assert _limited(reader) == {("/health/live", "public"): 1, ("unmatched", "public"): 1}


def test_a_session_allows_600_per_minute() -> None:
    harness = Harness()
    cookie = harness.session("tasa-sesion")
    metrics, reader, policy = _metrics()
    with TestClient(harness.app(metrics=metrics, attribute_policy=policy)) as client:
        statuses = [
            client.get("/me", headers=cookie_header(cookie)).status_code for _ in range(600)
        ]
        limited = client.get("/me", headers=cookie_header(cookie))
        other = harness.session("otra-sesion")
        fresh = client.get("/me", headers=cookie_header(other))
    assert statuses == [200] * 600
    assert limited.status_code == 429 and limited.json()["retry_after_seconds"] >= 1
    assert fresh.status_code == 200
    assert _limited(reader) == {("/me", "session"): 1}


def test_an_origin_allows_1200_per_minute_on_authenticated_routes() -> None:
    harness = Harness()
    cookies = [harness.session(f"origen-{index}") for index in range(3)]
    with TestClient(harness.app()) as client:
        statuses = [
            client.get("/me", headers=cookie_header(cookies[index % 3])).status_code
            for index in range(1_200)
        ]
        limited = client.get("/me", headers=cookie_header(cookies[0]))
        unauthenticated = client.get("/me")
    assert statuses == [200] * 1_200
    assert limited.status_code == 429
    assert unauthenticated.status_code == 429, "el origen ya agotó su presupuesto autenticado"


def test_node_routes_are_outside_the_origin_limiter() -> None:
    harness = Harness()
    with TestClient(harness.app()) as client:
        statuses = [client.get("/api/nodes/x").status_code for _ in range(1_300)]
    # Más que cualquier presupuesto por origen (1 200): el nodo no pasa por ese limitador.
    assert 429 not in statuses


def test_the_public_and_the_authenticated_buckets_are_separate() -> None:
    harness = Harness()
    cookie = harness.session("separados")
    with TestClient(harness.app()) as client:
        public = [client.get("/health/live").status_code for _ in range(61)]
        private = client.get("/me", headers=cookie_header(cookie))
    assert public[-1] == 429 and private.status_code == 200
