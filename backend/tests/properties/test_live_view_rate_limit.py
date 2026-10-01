"""PR-NUC-45: límite exacto de 30 tokens por usuario cada 10 minutos (TASK-128; BR-NUC-90).

"Para cualquier secuencia de emisiones concurrentes generadas (varios procesos simulados) sobre
un mismo usuario, nunca existen más de 30 emisiones en ninguna ventana deslizante de 10 minutos,
contadas sobre la tabla" (PAT-NUC-ESC-03).

Cada proceso simulado tiene su propio motor y pool (``shared.db`` como ``vigia_app``), su escritor
de auditoría y su servicio de firma; comparten la base y el reloj. Cada lote lanza a la vez, con
``asyncio.gather``, las peticiones de todos los procesos sobre el mismo usuario.

- **Sin desfase**: todas las peticiones de un lote llevan el mismo instante, así que el oráculo
  es exacto e independiente del orden: se conceden ``min(pedidas, 30 - en la ventana)`` y cada
  rechazo es ``rate_limited`` con ``retry_after_seconds`` hasta que sale la más antigua.
- **Con relojes desfasados** (uno atrasado y otro adelantado): el invariante sobre la tabla.

La ventana es ``(t - 600 s, t]``: dos emisiones separadas exactamente 600 s no comparten ventana.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.live_view_support import (
    LiveViewEnvironment,
    SimulatedProcess,
    SkewedClock,
    live_view_environment,
)
from vigia_platform.shared.context import Role, ScopeContext
from vigia_platform.shared.tokens import (
    RATE_LIMIT_TOKENS,
    RATE_LIMIT_WINDOW,
    IssuedLiveViewToken,
    LiveViewRejection,
    LiveViewTokenRejected,
)

pytestmark = pytest.mark.integration

WINDOW_SECONDS = int(RATE_LIMIT_WINDOW.total_seconds())
EXAMPLES = 30
"""Por semilla: cada ejemplo son decenas de transacciones concurrentes reales."""
SKEWS = (timedelta(seconds=-7), timedelta(0), timedelta(seconds=5))


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[LiveViewEnvironment]:
    with live_view_environment(postgres_endpoint, "live_view_rate_limit") as environment:
        yield environment


@pytest.fixture(scope="module")
def target(env: LiveViewEnvironment) -> dict[str, Any]:
    """Una organización con dos zonas, cada una con su nodo vigente."""
    site = env.add_site(plants=1, zones_per_plant=2)
    zones = []
    for plant_id, zone_id in site.zones():
        node_id = env.add_node(site.organization_id, plant_id)
        env.assign_node(site.organization_id, plant_id, zone_id, node_id)
        zones.append(zone_id)
    return {"organization_id": site.organization_id, "zones": zones}


@pytest.fixture(scope="module")
def processes(env: LiveViewEnvironment) -> list[SimulatedProcess]:
    return [env.process() for _ in range(3)]


@pytest.fixture(scope="module")
def skewed(env: LiveViewEnvironment) -> list[SimulatedProcess]:
    return [env.process(SkewedClock(env.clock, skew)) for skew in SKEWS]


def _new_user(env: LiveViewEnvironment, target: dict[str, Any]) -> tuple[uuid.UUID, ScopeContext]:
    user = env.user_with_role(target["organization_id"], Role.COORDINATOR_SST)
    return user, env.session_context(target["organization_id"], user)


async def _attempt(
    process: SimulatedProcess, context: ScopeContext, zone_id: uuid.UUID
) -> IssuedLiveViewToken | LiveViewTokenRejected:
    try:
        return await process.service.issue(context, zone_id)
    except LiveViewTokenRejected as rejected:
        return rejected


def _batch(
    env: LiveViewEnvironment,
    processes: list[SimulatedProcess],
    context: ScopeContext,
    zones: list[uuid.UUID],
    counts: list[int],
) -> list[IssuedLiveViewToken | LiveViewTokenRejected]:
    async def everything() -> list[IssuedLiveViewToken | LiveViewTokenRejected]:
        return list(
            await asyncio.gather(
                *(
                    _attempt(process, context, zones[(index + n) % len(zones)])
                    for index, (process, count) in enumerate(zip(processes, counts, strict=True))
                    for n in range(count)
                )
            )
        )

    results: list[IssuedLiveViewToken | LiveViewTokenRejected] = env.run(everything())
    return results


def _issued_times(env: LiveViewEnvironment, user_id: uuid.UUID) -> list[datetime]:
    rows = env.fetch(
        "SELECT issued_at FROM identity.live_view_token_issuance WHERE user_id = $1"
        " ORDER BY issued_at",
        user_id,
    )
    return [row["issued_at"] for row in rows]


def _max_in_any_window(times: list[datetime]) -> int:
    """El máximo de emisiones en una ventana ``(t - 600 s, t]``: basta con las que acaban en una."""
    return max(
        (sum(1 for other in times if end - RATE_LIMIT_WINDOW < other <= end) for end in times),
        default=0,
    )


BATCHES = st.lists(
    st.tuples(
        st.one_of(
            st.integers(0, 2 * WINDOW_SECONDS),
            st.sampled_from([WINDOW_SECONDS - 1, WINDOW_SECONDS, WINDOW_SECONDS + 1]),
        ),
        st.lists(st.integers(0, 14), min_size=3, max_size=3),
    ),
    min_size=1,
    max_size=6,
)
"""Lotes: segundos desde el anterior y peticiones simultáneas de cada uno de los tres procesos."""


@given(batches=BATCHES)
@settings(max_examples=EXAMPLES, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pr_nuc_45_exact_limit_with_concurrent_processes(
    env: LiveViewEnvironment,
    target: dict[str, Any],
    processes: list[SimulatedProcess],
    batches: list[tuple[int, list[int]]],
) -> None:
    clock = env.clock
    start = clock.now()
    user, context = _new_user(env, target)
    model: list[datetime] = []
    try:
        moment = start.replace(microsecond=0) + timedelta(seconds=1)
        for gap, counts in batches:
            moment += timedelta(seconds=gap)
            clock.set(moment + timedelta(milliseconds=250))
            in_window = [t for t in model if t > moment - RATE_LIMIT_WINDOW]
            expected = min(sum(counts), max(0, RATE_LIMIT_TOKENS - len(in_window)))
            results = _batch(env, processes, context, target["zones"], counts)
            granted = [r for r in results if isinstance(r, IssuedLiveViewToken)]
            rejected = [r for r in results if isinstance(r, LiveViewTokenRejected)]
            assert len(granted) == expected
            assert len(granted) + len(rejected) == sum(counts)
            model += [moment] * expected
            if rejected:
                oldest = min(t for t in model if t > moment - RATE_LIMIT_WINDOW)
                retry = int((oldest + RATE_LIMIT_WINDOW - moment).total_seconds())
                for rejection in rejected:
                    assert rejection.code is LiveViewRejection.RATE_LIMITED
                    assert rejection.api_code == "rate_limited"
                    assert rejection.retry_after_seconds == retry
                    assert 1 <= retry <= WINDOW_SECONDS
            times = _issued_times(env, user)
            assert times == sorted(model)
            assert _max_in_any_window(times) <= RATE_LIMIT_TOKENS
    finally:
        clock.set(start)


@given(batches=BATCHES)
@settings(max_examples=EXAMPLES, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_pr_nuc_45_never_more_than_30_in_a_window_with_skewed_clocks(
    env: LiveViewEnvironment,
    target: dict[str, Any],
    skewed: list[SimulatedProcess],
    batches: list[tuple[int, list[int]]],
) -> None:
    clock = env.clock
    start = clock.now()
    user, context = _new_user(env, target)
    try:
        for gap, counts in batches:
            clock.advance(gap + 0.5)
            results = _batch(env, skewed, context, target["zones"], counts)
            assert all(
                isinstance(r, IssuedLiveViewToken) or r.code is LiveViewRejection.RATE_LIMITED
                for r in results
            )
            assert _max_in_any_window(_issued_times(env, user)) <= RATE_LIMIT_TOKENS
    finally:
        clock.set(start)


# --- Bordes ------------------------------------------------------------------------------------


def _issue(
    env: LiveViewEnvironment, context: ScopeContext, zone_id: uuid.UUID
) -> IssuedLiveViewToken:
    issued: IssuedLiveViewToken = env.run(env.service().issue(context, zone_id))
    return issued


def _rejected(
    env: LiveViewEnvironment, context: ScopeContext, zone_id: uuid.UUID
) -> LiveViewTokenRejected:
    with pytest.raises(LiveViewTokenRejected) as raised:
        _issue(env, context, zone_id)
    return raised.value


def test_thirty_pass_the_thirty_first_waits_until_the_oldest_leaves(
    env: LiveViewEnvironment, target: dict[str, Any]
) -> None:
    clock = env.clock
    start = clock.now()
    user, context = _new_user(env, target)
    zone = target["zones"][0]
    try:
        base = start.replace(microsecond=0) + timedelta(seconds=1)
        clock.set(base)
        _issue(env, context, zone)
        clock.set(base + timedelta(seconds=100))
        for _ in range(RATE_LIMIT_TOKENS - 1):
            _issue(env, context, target["zones"][1])
        rejected = _rejected(env, context, zone)
        assert rejected.retry_after_seconds == WINDOW_SECONDS - 100
        clock.set(base + timedelta(seconds=WINDOW_SECONDS - 1, milliseconds=999))
        assert _rejected(env, context, zone).retry_after_seconds == 1
        # A los 600 s exactos la primera ya no está en la ventana: cabe una, y solo una.
        clock.set(base + timedelta(seconds=WINDOW_SECONDS))
        _issue(env, context, zone)
        rejected = _rejected(env, context, zone)
        assert rejected.retry_after_seconds == 100
        assert len(_issued_times(env, user)) == RATE_LIMIT_TOKENS + 1
        assert _max_in_any_window(_issued_times(env, user)) == RATE_LIMIT_TOKENS
    finally:
        clock.set(start)


def test_rejection_leaves_no_issuance_nor_audit(
    env: LiveViewEnvironment, target: dict[str, Any]
) -> None:
    clock = env.clock
    start = clock.now()
    user, context = _new_user(env, target)
    try:
        clock.set(start.replace(microsecond=0) + timedelta(seconds=1))
        for _ in range(RATE_LIMIT_TOKENS):
            _issue(env, context, target["zones"][0])
        before = env.fetch(
            "SELECT count(*) AS n FROM shared.audit_entry WHERE actor_id = $1"
            " AND operation = 'live_view_token_issued'",
            user,
        )[0]["n"]
        for _ in range(3):
            _rejected(env, context, target["zones"][0])
        after = env.fetch(
            "SELECT count(*) AS n FROM shared.audit_entry WHERE actor_id = $1"
            " AND operation = 'live_view_token_issued'",
            user,
        )[0]["n"]
        assert before == after == RATE_LIMIT_TOKENS
        assert len(_issued_times(env, user)) == RATE_LIMIT_TOKENS
    finally:
        clock.set(start)


def test_the_limit_is_per_user_not_per_zone_nor_shared(
    env: LiveViewEnvironment, target: dict[str, Any]
) -> None:
    clock = env.clock
    start = clock.now()
    _, first = _new_user(env, target)
    _, second = _new_user(env, target)
    try:
        clock.set(start.replace(microsecond=0) + timedelta(seconds=1))
        for n in range(RATE_LIMIT_TOKENS):
            _issue(env, first, target["zones"][n % 2])
        for zone in target["zones"]:
            assert _rejected(env, first, zone).code is LiveViewRejection.RATE_LIMITED
        _issue(env, second, target["zones"][0])
    finally:
        clock.set(start)
