"""``shared.db`` contra PostgreSQL 16 real (LC-NUC-29; PAT-NUC-SEG-01, RES-01, RES-03).

- Las tres variables de sesión quedan fijadas **solo dentro** de la transacción: al terminar
  (confirmada, revertida o de lectura), ``current_setting(..., true)`` vuelve a vacío en la
  misma conexión del pool (mismo ``pg_backend_pid``).
- ``db.transaction(None)`` lanza ``ContextAbsent`` sin abrir conexión (pool instrumentado).
- Con el pool ``person`` agotado, una transacción ``node`` sigue abriéndose (mamparo).
- Con PostgreSQL pausado, una lectura termina en ``temporarily_unavailable`` dentro de su tiempo
  de espera, y el pool se recupera al reanudarlo.
- ``lock_timeout`` → ``chain_locked_timeout``; ``statement_timeout`` → ``temporarily_unavailable``;
  la lectura es ``READ ONLY``.

Solo datos generados. La prueba de pausa usa un contenedor propio para no congelar el de la
sesión.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from tests.factories import make_context
from tests.integration.conftest import (
    POSTGRES_DATABASE,
    POSTGRES_IMAGE,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    PostgresEndpoint,
)
from vigia_platform.shared.context import ActorKind, ContextAbsent, ScopeContext
from vigia_platform.shared.db import (
    ChainLockedTimeout,
    Database,
    DatabaseSettings,
    PoolClass,
    ProcessKind,
    RouteClass,
    SslMode,
    TemporarilyUnavailable,
    _EnginePool,
    route_class_scope,
)

pytestmark = pytest.mark.integration

READ_SCOPE = text(
    "SELECT pg_backend_pid() AS pid, "
    "current_setting('vigia.organization_id', true) AS organization_id, "
    "current_setting('vigia.actor_kind', true) AS actor_kind, "
    "current_setting('vigia.concession_id', true) AS concession_id"
)


@dataclass
class PoolCounters:
    """Instrumentación del pool: conexiones abiertas y entregas."""

    connects: int = 0
    checkouts: int = 0


def _engine(database: Database, pool_class: PoolClass) -> AsyncEngine:
    return cast(_EnginePool, database._pools[pool_class]).engine


def _instrument(database: Database) -> dict[PoolClass, PoolCounters]:
    counters: dict[PoolClass, PoolCounters] = {}
    for pool_class in database._pools:
        counter = counters[pool_class] = PoolCounters()
        pool = _engine(database, pool_class).sync_engine.pool

        def on_connect(*_: object, c: PoolCounters = counter) -> None:
            c.connects += 1

        def on_checkout(*_: object, c: PoolCounters = counter) -> None:
            c.checkouts += 1

        event.listen(pool, "connect", on_connect)
        event.listen(pool, "checkout", on_checkout)
    return counters


def _settings(endpoint: PostgresEndpoint, **changes: Any) -> DatabaseSettings:
    fields: dict[str, Any] = {
        "url": endpoint.sqlalchemy_url,
        "process": ProcessKind.API,
        "sslmode": SslMode.DISABLE,  # el contenedor local no tiene TLS; en AWS, verify-full
    }
    fields.update(changes)
    return DatabaseSettings(**fields)


@pytest_asyncio.fixture
async def make_database(
    postgres_endpoint: PostgresEndpoint,
) -> AsyncIterator[Callable[..., Database]]:
    created: list[Database] = []

    def factory(**changes: Any) -> Database:
        database = Database.create(_settings(postgres_endpoint, **changes))
        created.append(database)
        return database

    yield factory
    for database in created:
        await database.dispose()


async def _raw_scope(database: Database, pool_class: PoolClass) -> dict[str, Any]:
    """Lee las variables y el pid por la conexión del pool, **sin** contexto ni transacción."""
    async with _engine(database, pool_class).connect() as connection:
        row = (await connection.execute(READ_SCOPE)).mappings().one()
        await connection.rollback()
    return dict(row)


def _expected(context: ScopeContext) -> dict[str, str]:
    return {
        "organization_id": str(context.organization_id),
        "actor_kind": context.actor.kind.value,
        "concession_id": "" if context.concession_id is None else str(context.concession_id),
    }


EMPTY = {"organization_id": "", "actor_kind": "", "concession_id": ""}


# --- SET LOCAL solo dentro de la transacción ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ActorKind.USER, ActorKind.PROVIDER_USER, ActorKind.NODE])
async def test_variables_live_only_inside_the_transaction(
    make_database: Callable[..., Database], kind: ActorKind
) -> None:
    database = make_database(person_pool_size=1)  # una conexión: la misma antes y después
    context = make_context(kind=kind)

    before = await _raw_scope(database, PoolClass.PERSON)
    async with database.transaction(context) as transaction:
        inside = dict((await transaction.execute(READ_SCOPE)).mappings().one())
    after = await _raw_scope(database, PoolClass.PERSON)

    assert inside["pid"] == before["pid"] == after["pid"]
    assert {key: inside[key] for key in EMPTY} == _expected(context)
    assert {key: after[key] for key in EMPTY} == EMPTY
    if kind is ActorKind.PROVIDER_USER:
        assert inside["concession_id"] != ""


@pytest.mark.asyncio
async def test_variables_are_cleared_after_rollback_and_after_read(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(person_pool_size=1)
    context = make_context()

    class CallerError(Exception):
        pass

    with pytest.raises(CallerError):
        async with database.transaction(context) as transaction:
            await transaction.execute(READ_SCOPE)
            raise CallerError
    rolled_back = await _raw_scope(database, PoolClass.PERSON)

    rows = await database.read(context, READ_SCOPE)
    read_inside = dict(rows[0]._mapping)
    after_read = await _raw_scope(database, PoolClass.PERSON)

    assert rolled_back["pid"] == read_inside["pid"] == after_read["pid"]
    assert {key: rolled_back[key] for key in EMPTY} == EMPTY
    assert {key: read_inside[key] for key in EMPTY} == _expected(context)
    assert {key: after_read[key] for key in EMPTY} == EMPTY


@pytest.mark.asyncio
async def test_consecutive_contexts_never_leak_into_each_other(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(person_pool_size=1)
    provider, user = make_context(kind=ActorKind.PROVIDER_USER), make_context()
    async with database.transaction(provider):
        pass
    rows = await database.read(user, READ_SCOPE)
    assert {key: rows[0]._mapping[key] for key in EMPTY} == _expected(user)
    assert rows[0]._mapping["concession_id"] == ""


# --- Sin contexto no hay conexión ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "org", {"organization_id": str(uuid.uuid4())}])
async def test_transaction_without_context_opens_no_connection(
    make_database: Callable[..., Database], value: object
) -> None:
    database = make_database()
    counters = _instrument(database)

    with pytest.raises(ContextAbsent):
        database.transaction(cast(ScopeContext, value))
    with pytest.raises(ContextAbsent):
        await database.read(cast(ScopeContext, value), READ_SCOPE)

    assert all(c.connects == 0 and c.checkouts == 0 for c in counters.values())
    # Contraprueba: la instrumentación sí ve una transacción con contexto.
    async with database.transaction(make_context()):
        pass
    assert counters[PoolClass.PERSON].checkouts == 1


# --- Mamparo entre pools --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_transaction_opens_while_person_pool_is_exhausted(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(pool_timeout_seconds=1.0)  # tamaños por defecto: node 10, person 5
    counters = _instrument(database)
    person_pool = cast(QueuePool, _engine(database, PoolClass.PERSON).sync_engine.pool)
    release = asyncio.Event()
    held = 0

    async def hold_person() -> None:
        nonlocal held
        async with database.transaction(make_context()) as transaction:
            await transaction.execute(text("SELECT 1"))
            held += 1
            await release.wait()

    holders = [asyncio.create_task(hold_person()) for _ in range(5)]
    try:
        while held < 5:
            await asyncio.sleep(0.01)
        assert person_pool.checkedout() == person_pool.size() == 5

        # El pool person agotado: una sexta transacción person espera su tope y falla.
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TemporarilyUnavailable):
            async with database.transaction(make_context()):
                pass
        assert loop.time() - start < 1.0 + 1.0

        # La clase node sigue abriéndose al momento, por su propio pool.
        start = loop.time()
        with route_class_scope(RouteClass.NODE):
            async with database.transaction(make_context(kind=ActorKind.NODE)) as transaction:
                assert (await transaction.execute(text("SELECT 1"))).scalar_one() == 1
            rows = await database.read(make_context(kind=ActorKind.NODE), text("SELECT 2"))
        assert rows[0][0] == 2
        assert loop.time() - start < 1.0
        assert counters[PoolClass.NODE].checkouts == 2
        assert counters[PoolClass.PERSON].checkouts == 5
        assert person_pool.checkedout() == 5
    finally:
        release.set()
        await asyncio.gather(*holders)


# --- Traducción de errores ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_timeout_is_chain_locked_timeout(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(lock_timeout_ms=200)
    lock = text("LOCK TABLE vigia_lock_probe IN ACCESS EXCLUSIVE MODE")
    async with database.transaction(make_context()) as transaction:
        await transaction.execute(text("CREATE TABLE IF NOT EXISTS vigia_lock_probe (id int)"))
    holding, waiting = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with database.transaction(make_context()) as transaction:
            await transaction.execute(lock)
            holding.set()
            await waiting.wait()

    task = asyncio.create_task(holder())
    try:
        await holding.wait()
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(ChainLockedTimeout):
            async with database.transaction(make_context()) as transaction:
                await transaction.execute(lock)
        assert loop.time() - start < 0.2 + 1.0
    finally:
        waiting.set()
        await task
        async with database.transaction(make_context()) as transaction:
            await transaction.execute(text("DROP TABLE vigia_lock_probe"))


@pytest.mark.asyncio
async def test_statement_timeout_is_temporarily_unavailable_without_retry(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(statement_timeout_ms=300)
    counters = _instrument(database)
    with pytest.raises(TemporarilyUnavailable):
        await database.read(make_context(), text("SELECT pg_sleep(2)"))
    assert counters[PoolClass.PERSON].checkouts == 1  # sin reintento


@pytest.mark.asyncio
async def test_read_is_read_only_and_the_mode_does_not_stick(
    make_database: Callable[..., Database],
) -> None:
    database = make_database(person_pool_size=1)
    with pytest.raises(sa_exc.DBAPIError) as caught:
        await database.read(make_context(), text("CREATE TEMP TABLE vigia_ro_probe (id int)"))
    assert getattr(caught.value.orig, "sqlstate", None) == "25006"  # read_only_sql_transaction
    async with database.transaction(make_context()) as transaction:
        await transaction.execute(text("CREATE TEMP TABLE vigia_ro_probe (id int)"))


# --- PostgreSQL pausado ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def paused_postgres() -> Iterator[tuple[PostgresEndpoint, Any]]:
    """PostgreSQL propio para pausarlo sin congelar el de la sesión."""
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        image=POSTGRES_IMAGE,
        username=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=POSTGRES_DATABASE,
        driver=None,
    )
    try:
        container.start()
    except Exception as error:
        pytest.fail(f"Se necesita Docker para la prueba de pausa: {type(error).__name__}: {error}")
    try:
        endpoint = PostgresEndpoint(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(POSTGRES_PORT)),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE,
        )
        yield endpoint, container.get_wrapped_container()
    finally:
        container.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [True, False], ids=["conexion-en-pool", "pool-vacio"])
async def test_paused_postgres_read_ends_temporarily_unavailable_within_timeout(
    paused_postgres: tuple[PostgresEndpoint, Any], warm: bool
) -> None:
    endpoint, container = paused_postgres
    settings = _settings(
        endpoint, connect_timeout_seconds=1.0, statement_timeout_ms=1_000, pool_timeout_seconds=1.0
    )
    database = Database.create(settings)
    counters = _instrument(database)
    context = make_context()
    try:
        if warm:  # con una conexión ya en el pool, la comprobación previa es la que se cuelga
            assert (await database.read(context, text("SELECT 1")))[0][0] == 1
        container.pause()
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(TemporarilyUnavailable) as caught:
                await database.read(context, text("SELECT 1"))
            elapsed = loop.time() - start
            with pytest.raises(TemporarilyUnavailable):
                async with database.transaction(context):
                    pass
            write_elapsed = loop.time() - start - elapsed
        finally:
            container.unpause()

        assert caught.value.retry_after_seconds == 5
        assert elapsed <= settings.read_timeout_seconds + 0.5, (elapsed, settings)
        assert write_elapsed <= settings.attempt_timeout_seconds + 0.5, write_elapsed
        # Tras reanudar, el pool descarta lo roto y responde sin intervención.
        assert (await database.read(context, text("SELECT 3")))[0][0] == 3
        assert counters[PoolClass.PERSON].checkouts >= 1
        # Los intentos abandonados al vencer el tope terminan solos y devuelven su conexión:
        # ninguna queda fuera del pool (regresión: la cancelación a mitad del cierre la perdía).
        while database._abandoned:
            await asyncio.gather(*database._abandoned, return_exceptions=True)
        person_pool = cast(QueuePool, _engine(database, PoolClass.PERSON).sync_engine.pool)
        assert person_pool.checkedout() == 0
    finally:
        await database.dispose()


SAFETY_CAP_SECONDS = 30.0
"""Tope externo de la prueba: si el adaptador se colgara, falla aquí en vez de colgar la suite."""

DRAIN_WHILE_PAUSED_SECONDS = 5.0
"""Tope del drenaje con la base en pausa: sin red, cortar y retirar la conexión es inmediato."""


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["sentencia", "commit"])
async def test_paused_postgres_inside_open_transaction_ends_within_command_timeout(
    paused_postgres: tuple[PostgresEndpoint, Any], where: str
) -> None:
    """PostgreSQL se pausa con la transacción ya abierta: la escritura no se cuelga.

    ``sentencia``: la pausa llega antes de una sentencia → ``TemporarilyUnavailable``.
    ``commit``: la pausa llega justo antes de salir del bloque → el ``COMMIT`` vence y el
    resultado se declara desconocido (``commit_outcome_unknown``). En los dos casos la duración
    queda dentro del tope de comando y, tras reanudar, ninguna conexión queda fuera del pool.
    """
    endpoint, container = paused_postgres
    settings = _settings(
        endpoint, connect_timeout_seconds=1.0, statement_timeout_ms=1_000, pool_timeout_seconds=1.0
    )
    database = Database.create(settings)
    context = make_context()
    loop = asyncio.get_running_loop()
    elapsed = 0.0

    async def write() -> None:
        nonlocal elapsed
        start: float | None = None
        try:
            async with database.transaction(context) as transaction:
                await transaction.execute(text("SELECT 1"))
                container.pause()
                start = loop.time()
                if where == "sentencia":
                    await transaction.execute(text("SELECT 2"))
        finally:
            # Incluye la salida del bloque: ni la limpieza ni el COMMIT pueden colgarse.
            if start is not None:
                elapsed = loop.time() - start

    person_pool = cast(QueuePool, _engine(database, PoolClass.PERSON).sync_engine.pool)
    try:
        with pytest.raises(TemporarilyUnavailable) as caught:
            try:
                await asyncio.wait_for(write(), SAFETY_CAP_SECONDS)
            finally:
                paused_drained = False
                try:
                    # Aún en pausa: la conexión rota se cortó con ``terminate()`` (sin red) y ya
                    # salió del pool. Si ``terminate`` dejara de cortarla, el descarte esperaría a
                    # un servidor que no responde y esto vencería (seguimiento 2 de VIG-26).
                    await asyncio.wait_for(database._drain(), DRAIN_WHILE_PAUSED_SECONDS)
                    paused_drained = person_pool.checkedout() == 0
                finally:
                    container.unpause()
        assert paused_drained, "la conexión rota sigue fuera del pool con PostgreSQL en pausa"
        assert caught.value.commit_outcome_unknown == (where == "commit")
        assert caught.value.retry_after_seconds == 5
        assert elapsed <= settings.command_timeout_seconds + 1.0, (elapsed, settings)

        # Tras reanudar: el adaptador responde y la conexión rota se retiró del pool.
        assert (await database.read(context, text("SELECT 3")))[0][0] == 3
        await asyncio.wait_for(database._drain(), SAFETY_CAP_SECONDS)
        assert person_pool.checkedout() == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dispose_during_outage_is_bounded(
    paused_postgres: tuple[PostgresEndpoint, Any],
) -> None:
    """Un apagado con la base caída y intentos abandonados termina en su tope, sin colgarse.

    Lo único que ``dispose`` cancela es una comprobación previa colgada dentro del pool, que el
    adaptador no puede cortar: SQLAlchemy avisa entonces de una conexión que recoge el
    recolector de basura. En el apagado es inocuo; en operación normal no ocurre (las demás
    pruebas de pausa exigen ``checkedout() == 0``).
    """
    endpoint, container = paused_postgres
    settings = _settings(
        endpoint, connect_timeout_seconds=1.0, statement_timeout_ms=1_000, pool_timeout_seconds=1.0
    )
    database = Database.create(settings)
    context = make_context()
    assert (await database.read(context, text("SELECT 1")))[0][0] == 1
    container.pause()
    try:
        for _ in range(2):
            with pytest.raises(TemporarilyUnavailable):
                await database.read(context, text("SELECT 1"))
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.wait_for(database.dispose(), SAFETY_CAP_SECONDS)
        elapsed = loop.time() - start
        assert elapsed <= settings.attempt_timeout_seconds + 2.0, elapsed
    finally:
        container.unpause()
