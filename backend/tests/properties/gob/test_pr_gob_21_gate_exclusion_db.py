"""PR-GOB-21 verificada por la base: ``gate_state_no_overlap`` (TASK-202, PAT-GOB-REN-01).

PR-GOB-21: para cualquier secuencia generada de aperturas y cierres de intervalos por zona y
compuerta (incluidas transacciones concurrentes e instantes repetidos), la base **rechaza** con
``exclusion_violation`` toda la que produciría dos intervalos solapados de la misma zona y
compuerta; para todo instante ``t`` la consulta por contención (``effective @> t``, la de
``state_at``) devuelve a lo sumo un intervalo, y la consulta por solapamiento de ``[from, to)``
(``effective && tstzrange(from, to)``, la de ``gate_history``) devuelve exactamente los que se
solapan según un oráculo en memoria.

El oráculo es la definición: ``[a, b)`` y ``[c, d)`` se solapan si ``a < d`` y ``c < b`` (una
cota superior nula es infinito). Cada ejemplo usa dos zonas nuevas de la planta 0 del cliente A
y escribe con ``vigia_app`` dentro de su ``ScopeContext``; los instantes salen de una rejilla de
horas pequeña para que se repitan. Las operaciones concurrentes corren en dos conexiones a la
vez (``asyncio.gather``).

``test_concurrent_unbounded_openings_exactly_one_commits`` es la prueba concurrente del criterio:
dos transacciones abren a la vez un intervalo no acotado de la misma zona y compuerta; la
segunda espera a la primera (se comprueba en ``pg_stat_activity``, sin topes de pared) y, al
confirmar la primera, falla con ``exclusion_violation``. Sin la restricción, las dos confirman.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.catalog_db import PlantScope, gate_interval, plant_scopes
from tests.identity_db import BASE_TIME, IdentitySeed, MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

EXCLUSION_VIOLATION = "23P01"
CHECK_VIOLATION = "23514"
DATA_EXCEPTION = "22000"
GATES = ("mounting", "usage")
GRID_HOURS = 10
"""Rejilla de instantes: ``BASE_TIME`` + 0 … 10 horas (pocos valores, para que se repitan)."""

TRANSIENT_SQLSTATES = frozenset({"40P01", "40001"})
"""Lo que ``shared.db`` reintenta: interbloqueo y fallo de serialización."""
TRANSIENT_ATTEMPTS = 3

WAIT_FOR_LOCK_SECONDS = 30.0
"""Tope generoso de la espera por evento (la sesión bloqueada aparece en ``pg_stat_activity``)."""

Key = tuple[int, str]
"""(índice de zona del ejemplo, compuerta)."""
Interval = tuple[int, int | None]
"""[desde, hasta) en horas de la rejilla; ``None`` es no acotado."""


def _at(hour: float) -> dt.datetime:
    return BASE_TIME + dt.timedelta(hours=hour)


def overlaps(a: Interval, b: Interval) -> bool:
    """El oráculo: ``[a0, a1)`` y ``[b0, b1)`` comparten algún instante."""
    a_end = float("inf") if a[1] is None else a[1]
    b_end = float("inf") if b[1] is None else b[1]
    return a[0] < b_end and b[0] < a_end


def contains(interval: Interval, t: float) -> bool:
    return interval[0] <= t and (interval[1] is None or t < interval[1])


@dataclass
class Model:
    intervals: dict[Key, list[Interval]] = field(default_factory=dict)

    def of(self, key: Key) -> list[Interval]:
        return self.intervals.setdefault(key, [])

    def fits(self, key: Key, interval: Interval) -> bool:
        return not any(overlaps(interval, other) for other in self.of(key))

    def open_interval(self, key: Key) -> Interval | None:
        return next((i for i in self.of(key) if i[1] is None), None)


# --- Operaciones -----


@dataclass(frozen=True)
class Open:
    key: Key
    interval: Interval


@dataclass(frozen=True)
class Close:
    """Cierra el intervalo abierto de ``key`` en ``until`` (si lo hay)."""

    key: Key
    until: int


@dataclass(frozen=True)
class Handover:
    """Cierra el abierto en ``at`` y abre ``[at, ∞)`` en la misma transacción (LC-GOB-03)."""

    key: Key
    at: int
    status: str


@dataclass(frozen=True)
class Concurrent:
    first: Open
    second: Open


Operation = Open | Close | Handover | Concurrent

_KEYS = st.tuples(st.integers(0, 1), st.sampled_from(GATES))
_HOURS = st.integers(0, GRID_HOURS)


@st.composite
def _intervals(draw: st.DrawFn) -> Interval:
    start = draw(_HOURS)
    if draw(st.booleans()):
        return start, None
    return start, start + draw(st.integers(1, 4))


_OPENS = st.builds(Open, _KEYS, _intervals())
_OPERATIONS: st.SearchStrategy[Operation] = st.one_of(
    _OPENS,
    _OPENS,
    st.builds(Close, _KEYS, _HOURS),
    st.builds(Handover, _KEYS, _HOURS, st.sampled_from(["approved", "revoked", "pending"])),
    st.builds(Concurrent, _OPENS, _OPENS),
    # La carrera del criterio: dos aperturas no acotadas de la misma zona y compuerta.
    st.builds(
        lambda key, a, b: Concurrent(Open(key, (a, None)), Open(key, (b, None))),
        _KEYS,
        _HOURS,
        _HOURS,
    ),
)
_WINDOWS = st.lists(
    st.tuples(_HOURS, st.integers(1, GRID_HOURS + 4)).map(lambda w: (w[0], w[0] + w[1])),
    min_size=1,
    max_size=3,
)


# --- Base -----


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    seed: IdentitySeed
    scope: PlantScope


@pytest.fixture(scope="module")
def runner() -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as loop:
        yield loop


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_pr_gob_21") as (database, seed):
        yield World(database, seed, plant_scopes(seed.a)[0])


@pytest.fixture(scope="module")
def connections(world: World, runner: asyncio.Runner) -> Iterator[dict[str, Any]]:
    opened = {
        "superuser": runner.run(world.database.connect()),
        "app": runner.run(world.database.connect("vigia_app")),
        "app_2": runner.run(world.database.connect("vigia_app")),
    }
    try:
        yield opened
    finally:
        for connection in opened.values():
            runner.run(connection.close())


async def _new_zones(superuser: Any, scope: PlantScope, count: int) -> list[uuid.UUID]:
    zones = [uuid.uuid4() for _ in range(count)]
    for zone_id in zones:
        await superuser.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            scope.organization_id,
            scope.plant_id,
            f"PR-{zone_id.hex[:12].upper()}",
            BASE_TIME,
            scope.user_id,
        )
    return zones


def _insert(scope: PlantScope, zone_id: uuid.UUID, gate: str, interval: Interval) -> Any:
    return gate_interval(
        scope,
        _at(interval[0]),
        None if interval[1] is None else _at(interval[1]),
        zone_id=zone_id,
        gate=gate,
    )


_CLOSE = (
    "UPDATE catalog.gate_state_history SET effective_until = $4"
    " WHERE zone_id = $1 AND gate = $2 AND effective_from = $3"
)


async def _transaction(connection: Any, scope: PlantScope, *steps: tuple[str, list[Any]]) -> str:
    """Confirma los pasos en una transacción; ``ok`` o el SQLSTATE con el que la base la rechaza.

    Como ``shared.db``, reintenta lo transitorio (``40P01`` y ``40001``): dos inserciones
    concurrentes que chocan en una restricción de exclusión pueden esperarse mutuamente y
    PostgreSQL aborta una por interbloqueo; al reintentarla, choca con la que confirmó y la base la
    rechaza con ``exclusion_violation``.
    """
    for _ in range(TRANSIENT_ATTEMPTS):
        try:
            async with connection.transaction():
                await set_scope(connection, scope.organization_id)
                for sql, args in steps:
                    await connection.execute(sql, *args)
        except asyncpg.PostgresError as error:
            if error.sqlstate in TRANSIENT_SQLSTATES:
                continue
            return str(error.sqlstate)
        return "ok"
    return "transient"


async def _apply(
    operation: Operation,
    model: Model,
    zones: list[uuid.UUID],
    connections: dict[str, Any],
    scope: PlantScope,
) -> None:
    app = connections["app"]
    if isinstance(operation, Open):
        key, interval = operation.key, operation.interval
        expected = "ok" if model.fits(key, interval) else EXCLUSION_VIOLATION
        result = await _transaction(app, scope, _insert(scope, zones[key[0]], key[1], interval))
        assert result == expected, (operation, model.of(key))
        if result == "ok":
            model.of(key).append(interval)
    elif isinstance(operation, Close | Handover):
        key = operation.key
        current = model.open_interval(key)
        if current is None:
            return
        until = operation.until if isinstance(operation, Close) else operation.at
        steps = [(_CLOSE, [zones[key[0]], key[1], _at(current[0]), _at(until)])]
        if isinstance(operation, Handover):
            sql, args = gate_interval(
                scope, _at(until), None, zone_id=zones[key[0]], gate=key[1], status=operation.status
            )
            steps.append((sql, args))
        # Cerrar acorta el intervalo: nunca solapa. Solo falla un cierre no posterior al inicio:
        # igual al inicio, el rango vacío lo rechaza el CHECK; anterior, ya no se puede construir
        # el rango generado (data_exception).
        expected = (
            "ok"
            if until > current[0]
            else CHECK_VIOLATION
            if until == current[0]
            else DATA_EXCEPTION
        )
        result = await _transaction(app, scope, *steps)
        assert result == expected, (operation, model.of(key))
        if result == "ok":
            intervals = model.of(key)
            intervals[intervals.index(current)] = (current[0], until)
            if isinstance(operation, Handover):
                intervals.append((until, None))
    else:
        first, second = operation.first, operation.second
        results = await asyncio.gather(
            _transaction(
                app, scope, _insert(scope, zones[first.key[0]], first.key[1], first.interval)
            ),
            _transaction(
                connections["app_2"],
                scope,
                _insert(scope, zones[second.key[0]], second.key[1], second.interval),
            ),
        )
        fits = [model.fits(first.key, first.interval), model.fits(second.key, second.interval)]
        clash = first.key == second.key and overlaps(first.interval, second.interval)
        for index, fit in enumerate(fits):
            if not fit:
                assert results[index] == EXCLUSION_VIOLATION, (operation, results)
            elif not all(fits):
                # La otra choca con lo ya escrito y se revierte: esta no tiene con qué chocar.
                assert results[index] == "ok", (operation, results)
        if all(fits) and clash:
            # Exactamente una confirma; la otra espera y choca con la que confirmó.
            assert sorted(results) == [EXCLUSION_VIOLATION, "ok"], (operation, results)
        elif all(fits):
            assert results == ["ok", "ok"], (operation, results)
        for opened, result in zip((first, second), results, strict=True):
            if result == "ok":
                model.of(opened.key).append(opened.interval)


async def _check_queries(
    model: Model,
    zones: list[uuid.UUID],
    windows: list[tuple[int, int]],
    connections: dict[str, Any],
    scope: PlantScope,
) -> None:
    app = connections["app"]
    instants = [step / 2 for step in range(-1, 2 * (GRID_HOURS + 6))]
    async with app.transaction():
        await set_scope(app, scope.organization_id)
        rows = await app.fetch(
            "SELECT zone_id, gate, effective_from, effective_until"
            " FROM catalog.gate_state_history WHERE zone_id = ANY ($1::uuid[])",
            zones,
        )
        in_database: dict[Key, set[tuple[dt.datetime, dt.datetime | None]]] = {}
        for row in rows:
            key = (zones.index(row["zone_id"]), row["gate"])
            in_database.setdefault(key, set()).add((row["effective_from"], row["effective_until"]))
        for key in [(z, g) for z in range(len(zones)) for g in GATES]:
            expected = {
                (_at(low), None if high is None else _at(high)) for low, high in model.of(key)
            }
            assert in_database.get(key, set()) == expected, key
        # state_at: por contención, a lo sumo uno, y el del oráculo.
        counts = await app.fetch(
            "SELECT z.zone_id, g.gate, t.instant, count(h.zone_id) AS hits,"
            " min(h.effective_from) AS found"
            " FROM unnest($1::uuid[]) AS z(zone_id)"
            " CROSS JOIN unnest($2::text[]) AS g(gate)"
            " CROSS JOIN unnest($3::timestamptz[]) AS t(instant)"
            " LEFT JOIN catalog.gate_state_history AS h"
            "   ON h.zone_id = z.zone_id AND h.gate = g.gate AND h.effective @> t.instant"
            " GROUP BY z.zone_id, g.gate, t.instant",
            zones,
            list(GATES),
            [_at(t) for t in instants],
        )
        for row in counts:
            key = (zones.index(row["zone_id"]), row["gate"])
            t = (row["instant"] - BASE_TIME) / dt.timedelta(hours=1)
            holding = [i for i in model.of(key) if contains(i, t)]
            assert row["hits"] <= 1, (key, t)
            assert row["hits"] == len(holding), (key, t, model.of(key))
            if holding:
                assert row["found"] == _at(holding[0][0])
        # gate_history: por solapamiento de [from, to), exactamente los del oráculo.
        for low, high in windows:
            for zone_index, zone_id in enumerate(zones):
                found = await app.fetch(
                    "SELECT gate, effective_from FROM catalog.gate_state_history"
                    " WHERE zone_id = $1 AND effective && tstzrange($2, $3, '[)')",
                    zone_id,
                    _at(low),
                    _at(high),
                )
                expected_hits = {
                    (gate, _at(interval[0]))
                    for gate in GATES
                    for interval in model.of((zone_index, gate))
                    if overlaps(interval, (low, high))
                }
                assert {(row["gate"], row["effective_from"]) for row in found} == expected_hits


@given(operations=st.lists(_OPERATIONS, min_size=1, max_size=12), windows=_WINDOWS)
def test_pr_gob_21_the_database_rejects_every_overlap(
    operations: list[Operation],
    windows: list[tuple[int, int]],
    world: World,
    connections: dict[str, Any],
    runner: asyncio.Runner,
) -> None:
    async def run() -> None:
        zones = await _new_zones(connections["superuser"], world.scope, 2)
        model = Model()
        for operation in operations:
            await _apply(operation, model, zones, connections, world.scope)
        await _check_queries(model, zones, windows, connections, world.scope)

    runner.run(run())


# --- Prueba concurrente del criterio -----


async def _wait_until_blocked(superuser: Any, pid: int, task: asyncio.Task[str]) -> None:
    """Espera a que ``pid`` esté esperando un candado, o a que su transacción ya termine."""

    async def blocked() -> None:
        while not task.done():
            waiting = await superuser.fetchval(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1", pid
            )
            if waiting:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(blocked(), WAIT_FOR_LOCK_SECONDS)


@pytest.mark.parametrize(
    ("first_hour", "second_hour"),
    [(0, 0), (0, 3), (3, 0), (5, 5), (1, 2)],
    ids=["mismo-instante", "segundo-despues", "segundo-antes", "repetido", "contiguos"],
)
def test_concurrent_unbounded_openings_exactly_one_commits(
    first_hour: int,
    second_hour: int,
    world: World,
    connections: dict[str, Any],
    runner: asyncio.Runner,
) -> None:
    scope = world.scope

    async def run() -> None:
        (zone_id,) = await _new_zones(connections["superuser"], scope, 1)
        first, second = connections["app"], connections["app_2"]
        transaction = first.transaction()
        await transaction.start()
        try:
            await set_scope(first, scope.organization_id)
            sql, args = _insert(scope, zone_id, "usage", (first_hour, None))
            await first.execute(sql, *args)
            # La segunda transacción abre el suyo mientras la primera sigue sin confirmar.
            racing = asyncio.create_task(
                _transaction(second, scope, _insert(scope, zone_id, "usage", (second_hour, None)))
            )
            await _wait_until_blocked(connections["superuser"], second.get_server_pid(), racing)
        except BaseException:
            await transaction.rollback()
            raise
        await transaction.commit()
        outcome = await racing
        rows = await connections["superuser"].fetchval(
            "SELECT count(*) FROM catalog.gate_state_history WHERE zone_id = $1", zone_id
        )
        assert (outcome, rows) == (EXCLUSION_VIOLATION, 1)

    runner.run(run())


def test_concurrent_unbounded_openings_without_ordering(
    world: World, connections: dict[str, Any], runner: asyncio.Runner
) -> None:
    """Las dos a la vez, sin forzar el orden (20 rondas): siempre exactamente una confirma."""
    scope = world.scope

    async def run() -> None:
        for round_ in range(20):
            (zone_id,) = await _new_zones(connections["superuser"], scope, 1)
            results = await asyncio.gather(
                _transaction(
                    connections["app"],
                    scope,
                    _insert(scope, zone_id, "mounting", (round_ % 3, None)),
                ),
                _transaction(
                    connections["app_2"], scope, _insert(scope, zone_id, "mounting", (1, None))
                ),
            )
            rows = await connections["superuser"].fetchval(
                "SELECT count(*) FROM catalog.gate_state_history WHERE zone_id = $1", zone_id
            )
            assert (sorted(results), rows) == ([EXCLUSION_VIOLATION, "ok"], 1), round_

    runner.run(run())
