"""``expire_enrollment_codes`` y ``expire_walk_test_sessions`` contra PostgreSQL 16 (TASK-227).

Como ``vigia_app``, cada tarea en la transacción de la organización con el contexto de la iteración
periódica de U-03 (``system``), y su propio reloj simulado (el instante de la ejecución; las marcas
de las filas salen del reloj del mundo, nunca de la hora real de la base):

- **Códigos de alta** (nota de cadencias de BL §2.6; DE §3.2): un ``active`` vence en
  ``expires_at`` exacto (1 ms antes, no), queda ``expired`` con su hash y su registro (nada se
  borra) y una segunda pasada no cambia nada; ``used`` y ``superseded`` nunca se tocan.
- **Alta frente al vencimiento** (prueba concurrente): el consumo y el vencimiento del mismo código
  en el mismo instante dejan el código ``used`` **o** ``expired``, nunca las dos cosas ni una
  credencial con el código vencido: en los dos órdenes del candado (el segundo espera la fila y ya
  no la cumple) y con altas reales por la ruta del contrato lanzadas a la vez que la tarea.
- **Sesiones de walk-test** (BL §3.3; NFR-GOB-39): ``in_progress`` y ``reopened`` sin actividad
  durante 7 días (y 1 ms menos, no) pasan a ``incomplete`` conservando matriz, pasos, pases y
  pruebas de oclusión; la métrica de abiertas e incompletas de la organización (NFR-GOB-56). Dos
  workers que la ejecutan a la vez cambian cada sesión una sola vez.
- **Guardas de alcance** (PR-GOB-12): con dos organizaciones cada tarea solo cambia filas de la de
  su contexto, y su sentencia filtra por organización también sin RLS (superusuario): la prueba
  falla si se quita el filtro.

Topes: la espera de un candado se observa en ``pg_stat_activity`` (30 s); la base, 60 s (retro
15). Ninguna prueba decide por tiempo de pared. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_alarm_support import blocked_or_done, system
from tests.fleet_enrollment_support import (
    ENROLLMENT_PATH,
    VERSION,
    EnrollmentWorld,
    NodeSetup,
    enrollment_world,
    new_key,
)
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.walk_test_support import REASON, Mounted, WalkTestWorld, walk_test_world
from vigia_platform.catalog.adapters.postgres import walk_test_repository
from vigia_platform.catalog.application.walk_test_expiry import WalkTestExpirer
from vigia_platform.catalog.domain.walk_test import INACTIVITY_LIMIT, WalkTestSession
from vigia_platform.fleet.adapters.postgres import enrollment_store
from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.fleet.application.enrollment_code_expiry import EnrollmentCodeExpirer
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.db import Database
from vigia_platform.shared.observability.metrics import MetricName

pytestmark = pytest.mark.integration

MS: Final = timedelta(milliseconds=1)
LOCK_TIMEOUT_MS: Final = 60_000
RACES: Final = 6
"""Altas reales lanzadas a la vez que la tarea, cada una con su nodo y su código."""


# --- Entornos -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[EnrollmentWorld]:
    with enrollment_world(postgres_endpoint, "fleet_expiry_codes") as built:
        yield built


@pytest.fixture(scope="module")
def walk(postgres_endpoint: PostgresEndpoint) -> Iterator[WalkTestWorld]:
    with walk_test_world(postgres_endpoint, "fleet_expiry_walk") as built:
        yield built


def _superuser(fetch: Any, statement: Any, parameters: dict[str, Any]) -> list[Any]:
    """Una sentencia ``text()`` de la tarea como superusuario (sin RLS): solo su filtro explícito
    de organización la separa de las filas de otra organización."""
    compiled = statement.compile(dialect=asyncpg_dialect())
    rows: list[Any] = fetch(str(compiled), *(parameters[name] for name in compiled.positiontup))
    return rows


# --- Códigos de alta ------------------------------------------------------------------------------


def _issued(world: EnrollmentWorld, *, published: bool = False) -> tuple[NodeSetup, str, Any]:
    """Un nodo declarado con su código ``active`` recién emitido (y su fila)."""
    setup = world.declared(published=published)
    code = world.code(setup)
    rows = world.fleet.codes(setup.node_id)
    assert [row["status"] for row in rows][-1] == "active"
    return setup, code, rows[-1]


async def _expire_codes(
    database: Database, organization: uuid.UUID, at: datetime
) -> tuple[uuid.UUID, ...]:
    expirer = EnrollmentCodeExpirer(clock=SimulatedClock(at))
    async with database.transaction(system(organization)) as transaction:
        return await expirer.expire(transaction)


def _code_row(world: EnrollmentWorld, code_id: uuid.UUID) -> Any:
    (row,) = world.fetch(
        "SELECT status, code_hash, code_salt, ledger_record_id, expires_at"
        " FROM fleet.enrollment_code WHERE code_id = $1",
        code_id,
    )
    return row


def test_a_code_expires_exactly_at_its_expiry_and_is_kept(world: EnrollmentWorld) -> None:
    setup, _, row = _issued(world)
    database, code_id = world.fleet.database, row["code_id"]
    expires_at: datetime = row["expires_at"]

    assert world.run(_expire_codes(database, setup.organization_id, expires_at - MS)) == ()
    assert _code_row(world, code_id)["status"] == "active"

    expired = world.run(_expire_codes(database, setup.organization_id, expires_at))
    assert expired == (code_id,)
    kept = _code_row(world, code_id)
    assert kept["status"] == "expired"
    # Nada se borra ni se reescribe: el hash, la sal y el registro de la emisión siguen ahí.
    assert (kept["code_hash"], kept["code_salt"], kept["ledger_record_id"]) == (
        row["code_hash"],
        row["code_salt"],
        row["ledger_record_id"],
    )
    # Idempotente: una segunda pasada (o una solapada que llega tarde) no cambia nada.
    assert world.run(_expire_codes(database, setup.organization_id, expires_at + MS)) == ()
    assert _code_row(world, code_id)["status"] == "expired"


def test_used_and_superseded_codes_are_never_expired(world: EnrollmentWorld) -> None:
    setup, _, first = _issued(world)
    world.code(setup)  # el primero pasa a superseded
    second = world.fleet.codes(setup.node_id)[-1]
    store = PostgresEnrollmentStore()

    async def consume() -> bool:
        async with world.fleet.database.transaction(system(setup.organization_id)) as tx:
            return await store.consume(tx, second["code_id"], second["expires_at"] - MS)

    assert world.run(consume())
    far = second["expires_at"] + timedelta(days=30)
    assert world.run(_expire_codes(world.fleet.database, setup.organization_id, far)) == ()
    assert [row["status"] for row in world.fleet.codes(setup.node_id)] == ["superseded", "used"]
    assert first["code_id"] != second["code_id"]


def test_code_expiry_only_changes_the_organization_of_its_context(world: EnrollmentWorld) -> None:
    mine, _, my_code = _issued(world)
    theirs, _, their_code = _issued(world)
    assert mine.organization_id != theirs.organization_id
    late = max(my_code["expires_at"], their_code["expires_at"])

    expired = world.run(_expire_codes(world.fleet.database, mine.organization_id, late))

    assert expired == (my_code["code_id"],)
    assert _code_row(world, their_code["code_id"])["status"] == "active"


def test_the_code_expiry_statement_filters_by_organization_without_rls(
    world: EnrollmentWorld,
) -> None:
    """Como superusuario la RLS no separa nada: con la organización de A, el código vencido de B
    no se toca (la prueba falla si se quita ``organization_id = :organization_id``)."""
    mine, _, my_code = _issued(world)
    theirs, _, their_code = _issued(world)
    late = max(my_code["expires_at"], their_code["expires_at"])

    rows = _superuser(
        world.fetch,
        enrollment_store._EXPIRE_DUE,
        {"organization_id": mine.organization_id, "now": late, "limit": 1000},
    )

    assert {row["code_id"] for row in rows} == {my_code["code_id"]}
    assert _code_row(world, their_code["code_id"])["status"] == "active"
    assert theirs.organization_id != mine.organization_id


@pytest.mark.parametrize("first", ["consume", "expire"])
def test_a_consumption_and_an_expiry_of_the_same_code_leave_it_used_or_expired(
    world: EnrollmentWorld, first: str
) -> None:
    """Los dos órdenes del candado de la fila, forzados: la primera operación toma la fila y la
    segunda espera en ``pg_stat_activity`` hasta que la primera confirma. La alta consume con un
    instante en que el código aún vale (1 ms antes de vencer) y la tarea vence con el instante del
    vencimiento: las dos condiciones se cumplen al leer, solo ``status = 'active'`` decide."""
    setup, code, row = _issued(world)
    fleet = world.fleet
    service = fleet.services.enrollment_codes
    assert service is not None
    store = PostgresEnrollmentStore()
    second_database = app_database(
        fleet.authz.sessions.migrated, worker_pool_size=2, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    scope = world.run(
        fleet.authz.contexts.context_from_node_enrollment(
            PostgresNodeContextStore(fleet.database), setup.node_id
        )
    )
    expires_at: datetime = row["expires_at"]
    expirer = EnrollmentCodeExpirer(clock=SimulatedClock(expires_at))
    admin = fleet.authz.sessions.admin

    async def consume(database: Database, hold: asyncio.Event | None) -> bool:
        async with database.transaction(scope.context) as tx:
            consumed = await store.consume(tx, row["code_id"], expires_at - MS)
            if hold is not None:
                await hold.wait()
        return consumed

    async def expire(database: Database, hold: asyncio.Event | None) -> tuple[uuid.UUID, ...]:
        async with database.transaction(system(setup.organization_id)) as tx:
            expired = await expirer.expire(tx)
            if hold is not None:
                await hold.wait()
        return expired

    async def scenario() -> tuple[bool, tuple[uuid.UUID, ...]]:
        release = asyncio.Event()
        if first == "consume":
            holder: asyncio.Task[Any] = asyncio.create_task(consume(fleet.database, release))
        else:
            holder = asyncio.create_task(expire(fleet.database, release))
        await asyncio.sleep(0)
        # La primera ya tiene la fila cuando la segunda llega (su sentencia terminó).
        async with asyncio.timeout(30):
            while not await _row_locked(admin):
                await asyncio.sleep(0.05)
        if first == "consume":
            waiter: asyncio.Task[Any] = asyncio.create_task(expire(second_database, None))
        else:
            waiter = asyncio.create_task(consume(second_database, None))
        await blocked_or_done(admin, waiter)
        release.set()
        async with asyncio.timeout(60):
            first_result, second_result = await holder, await waiter
        if first == "consume":
            return first_result, second_result
        return second_result, first_result

    try:
        consumed, expired = world.run(scenario())
    finally:
        world.run(second_database.dispose())

    status = _code_row(world, row["code_id"])["status"]
    assert status == ("used" if first == "consume" else "expired")
    assert consumed == (status == "used")
    assert expired == ((row["code_id"],) if status == "expired" else ())
    if status == "expired":
        # El alta que perdió ya no encuentra un código vigente.
        check = world.run(service.verify(scope, code))
        assert not check.valid


async def _row_locked(admin: Any) -> bool:
    """``True`` cuando alguna sesión de esta base tiene una transacción abierta e inactiva (la
    primera operación terminó su sentencia y retiene la fila)."""
    waiting: int = await admin.fetchval(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_database() AND state = 'idle in transaction'"
    )
    return waiting >= 1


async def _enroll_while_expiring(
    world: EnrollmentWorld, payload: Any, organization: uuid.UUID, expires_at: datetime
) -> tuple[httpx.Response, tuple[uuid.UUID, ...]]:
    """El alta por la ruta del contrato y la tarea, lanzadas a la vez."""
    enroll = world.client.post(
        ENROLLMENT_PATH, json=payload, headers={"X-Vigia-Contract-Version": VERSION}
    )
    expire = _expire_codes(world.fleet.database, organization, expires_at)
    response, expired = await asyncio.gather(enroll, expire)
    return response, expired


def test_real_enrollments_raced_against_the_expiry_never_issue_a_credential_for_an_expired_code(
    world: EnrollmentWorld,
) -> None:
    """``RACES`` altas por la ruta del contrato (``EnrollmentService`` real) lanzadas a la vez que
    ``expire_enrollment_codes``. El alta ve el código vigente (el reloj del mundo, recién emitido)
    y la tarea lo ve vencido (su propio reloj, en el vencimiento): cualquiera puede ganar.
    En todas: el código queda ``used`` y hay exactamente una credencial, o queda ``expired``, el
    alta se rechaza y no hay ninguna."""
    outcomes: list[str] = []
    for _ in range(RACES):
        setup, code, row = _issued(world, published=True)
        expires_at: datetime = row["expires_at"]
        payload = world.body(setup, code, client_key=new_key(), server_key=new_key())
        response, expired = world.run(
            _enroll_while_expiring(world, payload, setup.organization_id, expires_at)
        )
        status = _code_row(world, row["code_id"])["status"]
        credentials = world.credentials(setup.node_id)
        assert status in {"used", "expired"}
        if status == "used":
            assert response.status_code == 200, response.text
            assert len(credentials) == 1
            assert expired == ()
        else:
            assert response.status_code != 200, response.text
            assert credentials == []
            assert expired == (row["code_id"],)
        outcomes.append(status)
    assert len(outcomes) == RACES


# --- Sesiones de walk-test ------------------------------------------------------------------------


def _session(walk: WalkTestWorld, mounted: Mounted) -> WalkTestSession:
    """Una sesión abierta con un paso, dos pases y una prueba de oclusión."""
    session = walk.open(mounted)
    walk.start(mounted, session.session_id)
    for row in session.matrix_rows[:2]:
        walk.advance()
        walk.run(
            walk.service.record_pass(mounted.installer, session.session_id, row.row_id, "detected")
        )
    ended = walk.a.g.authz.now()
    walk.execute(
        "INSERT INTO catalog.occlusion_test (test_id, organization_id, plant_id, session_id,"
        " camera_id, started_at, ended_at, deadline, recorded_by)"
        " VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $6::timestamptz,"
        " $6::timestamptz + interval '5 minutes', $7)",
        uuid.uuid4(),
        mounted.site.organization_id,
        mounted.plant,
        session.session_id,
        uuid.uuid4(),
        ended,
        uuid.UUID(str(mounted.installer.actor.id)),
    )
    return session


def _last_activity(walk: WalkTestWorld, session_id: uuid.UUID) -> datetime:
    activity: datetime = walk.session_row(session_id)["last_activity_at"]
    return activity


def _snapshot(walk: WalkTestWorld, session_id: uuid.UUID) -> dict[str, Any]:
    """Lo que la sesión tiene registrado, salvo el estado."""
    (row,) = walk.fetch(
        "SELECT matrix_rows::text AS matrix, started_at, last_activity_at, passes_per_cell,"
        " (SELECT count(*) FROM catalog.walk_test_step s WHERE s.session_id = w.session_id)"
        " AS steps,"
        " (SELECT count(*) FROM catalog.walk_test_pass p WHERE p.session_id = w.session_id)"
        " AS passes,"
        " (SELECT count(*) FROM catalog.occlusion_test o WHERE o.session_id = w.session_id)"
        " AS occlusions"
        " FROM catalog.walk_test_session AS w WHERE session_id = $1",
        session_id,
    )
    return dict(row)


async def _expire_sessions(
    database: Database, organization: uuid.UUID, at: datetime, metrics: Any = None
) -> tuple[uuid.UUID, ...]:
    expirer = WalkTestExpirer(clock=SimulatedClock(at), metrics=metrics)
    async with database.transaction(system(organization)) as transaction:
        return (await expirer.expire(transaction)).expired


def test_a_session_inactive_for_seven_days_becomes_incomplete_and_keeps_everything(
    walk: WalkTestWorld,
) -> None:
    mounted = walk.mounted()
    session = _session(walk, mounted)
    organization = mounted.site.organization_id
    database = walk.a.g.database
    before = _snapshot(walk, session.session_id)
    assert (before["steps"], before["passes"], before["occlusions"]) == (1, 2, 1)
    deadline = _last_activity(walk, session.session_id) + INACTIVITY_LIMIT
    metrics, reader = metrics_with_reader()

    assert walk.run(_expire_sessions(database, organization, deadline - MS, metrics)) == ()
    assert walk.session_row(session.session_id)["status"] == "in_progress"

    expired = walk.run(_expire_sessions(database, organization, deadline, metrics))

    assert expired == (session.session_id,)
    assert walk.session_row(session.session_id)["status"] == "incomplete"
    assert _snapshot(walk, session.session_id) == before  # pasos, pases, oclusiones y matriz
    assert walk.run(_expire_sessions(database, organization, deadline + MS)) == ()
    gauges = {
        name: [value for attributes, value in metric_points(reader, name)
               if attributes == {"organization_id": str(organization)}]
        for name in (MetricName.WALK_TEST_SESSIONS_OPEN, MetricName.WALK_TEST_SESSIONS_INCOMPLETE)
    }  # fmt: skip
    assert gauges[MetricName.WALK_TEST_SESSIONS_OPEN][-1] == 0
    assert gauges[MetricName.WALK_TEST_SESSIONS_INCOMPLETE][-1] == 1


def test_a_reopened_session_also_expires_and_keeps_its_reopening(walk: WalkTestWorld) -> None:
    mounted = walk.mounted()
    session = _session(walk, mounted)
    organization = mounted.site.organization_id
    database = walk.a.g.database
    deadline = _last_activity(walk, session.session_id) + INACTIVITY_LIMIT
    assert walk.run(_expire_sessions(database, organization, deadline)) == (session.session_id,)
    walk.advance()
    walk.run(walk.service.reopen(mounted.installer, session.session_id, REASON))
    reopened = walk.session_row(session.session_id)
    assert reopened["status"] == "reopened"
    again = reopened["last_activity_at"] + INACTIVITY_LIMIT

    assert walk.run(_expire_sessions(database, organization, again - MS)) == ()
    assert walk.run(_expire_sessions(database, organization, again)) == (session.session_id,)
    row = walk.session_row(session.session_id)
    assert row["status"] == "incomplete"
    assert (row["reopened_at"], row["reopen_reason_es"]) == (
        reopened["reopened_at"],
        reopened["reopen_reason_es"],
    )


def test_session_expiry_only_changes_the_organization_of_its_context(walk: WalkTestWorld) -> None:
    mine, theirs = walk.mounted(), walk.mounted()
    assert mine.site.organization_id != theirs.site.organization_id
    my_session, their_session = _session(walk, mine), _session(walk, theirs)
    late = (
        max(
            _last_activity(walk, my_session.session_id),
            _last_activity(walk, their_session.session_id),
        )
        + INACTIVITY_LIMIT
    )

    expired = walk.run(_expire_sessions(walk.a.g.database, mine.site.organization_id, late))

    assert expired == (my_session.session_id,)
    assert walk.session_row(their_session.session_id)["status"] == "in_progress"


def test_the_session_expiry_statement_filters_by_organization_without_rls(
    walk: WalkTestWorld,
) -> None:
    mine, theirs = walk.mounted(), walk.mounted()
    my_session, their_session = _session(walk, mine), _session(walk, theirs)
    late = (
        max(
            _last_activity(walk, my_session.session_id),
            _last_activity(walk, their_session.session_id),
        )
        + INACTIVITY_LIMIT
    )

    rows = _superuser(
        walk.fetch,
        walk_test_repository._EXPIRE_INACTIVE,
        {
            "organization_id": mine.site.organization_id,
            "inactive_since": late - INACTIVITY_LIMIT,
            "limit": 1000,
        },
    )

    assert {row["session_id"] for row in rows} == {my_session.session_id}
    assert walk.session_row(their_session.session_id)["status"] == "in_progress"


def test_two_workers_expiring_at_once_change_each_session_once(walk: WalkTestWorld) -> None:
    """Dos workers (dos pools, dos transacciones) sobre la misma organización: el primero bloquea
    las filas y retiene su transacción; el segundo espera en ``pg_stat_activity`` y, cuando el
    primero confirma, ya no encuentra nada que cambiar. Después, ``RACES`` rondas con las dos
    pasadas lanzadas a la vez (``asyncio.gather``): cada sesión, una sola vez."""
    g = walk.a.g
    second = app_database(
        g.authz.sessions.migrated, worker_pool_size=2, lock_timeout_ms=LOCK_TIMEOUT_MS
    )
    admin = g.authz.sessions.admin

    def stale_sessions() -> tuple[uuid.UUID, list[uuid.UUID], datetime]:
        site = walk.a.g.site(plants=1, zones=3)
        sessions = [_session(walk, walk.mounted(site=site, zone_index=i)) for i in range(3)]
        latest = max(_last_activity(walk, s.session_id) for s in sessions)
        return site.organization_id, sorted(s.session_id for s in sessions), latest

    async def held(
        database: Database, organization: uuid.UUID, at: datetime, hold: asyncio.Event | None
    ) -> tuple[uuid.UUID, ...]:
        expirer = WalkTestExpirer(clock=SimulatedClock(at))
        async with database.transaction(system(organization)) as transaction:
            report = await expirer.expire(transaction)
            if hold is not None:
                await hold.wait()
        return report.expired

    async def ordered(organization: uuid.UUID, at: datetime) -> list[tuple[uuid.UUID, ...]]:
        release = asyncio.Event()
        first = asyncio.create_task(held(g.database, organization, at, release))
        async with asyncio.timeout(30):
            while not await _row_locked(admin):
                await asyncio.sleep(0.05)
        waiter = asyncio.create_task(held(second, organization, at, None))
        await blocked_or_done(admin, waiter)
        release.set()
        async with asyncio.timeout(60):
            return [await first, await waiter]

    async def together(organization: uuid.UUID, at: datetime) -> list[tuple[uuid.UUID, ...]]:
        return list(
            await asyncio.gather(
                held(g.database, organization, at, None), held(second, organization, at, None)
            )
        )

    try:
        organization, sessions, latest = stale_sessions()
        first, late = walk.run(ordered(organization, latest + INACTIVITY_LIMIT))
        assert sorted(first) == sessions
        assert late == ()
        for _ in range(RACES):
            organization, sessions, latest = stale_sessions()
            one, two = walk.run(together(organization, latest + INACTIVITY_LIMIT))
            assert sorted(one + two) == sessions, (one, two)
            assert set(one).isdisjoint(two)
            for session_id in sessions:
                assert walk.session_row(session_id)["status"] == "incomplete"
    finally:
        walk.run(second.dispose())
