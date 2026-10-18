"""PR-LAZ-04 (base de datos): con dos organizaciones, ninguna tabla de U-04 deja ver la otra
(TASK-301; NFR-LAZ-22; PAT-LAZ-SEG-03; BR-NUC-01 a 03).

Paramétrica por esquema: ``SCHEMAS`` lleva hoy ``loop`` y ``notifications`` (TASK-339);
TASK-338 (``accreditation``) solo añade su entrada (nombre, tablas esperadas, siembra y
constructores de filas) y heredan todas las comprobaciones. Sobre una base migrada con los
clientes A y B de ``tests/identity_db.py`` (el par de organizaciones de U-02, dos plantas cada
uno) y filas de cada tabla en cada planta:

1. **Cobertura**: las tablas del esquema en la base (las que haya, no una lista escrita a mano)
   son exactamente las esperadas, y todas tienen ``organization_id``.
2. **Por el camino de la aplicación**: ``shared.db`` como ``vigia_app`` con el ``ScopeContext`` de
   A (``SET LOCAL``) devuelve de cada tabla exactamente las filas de A (las que el superusuario ve
   con ``organization_id = A``), y con el de B las de B. Ninguna fila de la otra.
3. **Sin contexto**: ``Database.read(None, …)`` sobre cada tabla lanza ``ContextAbsent`` sin pedir
   ninguna conexión al pool (sin ejecutar consulta).
4. **Propiedad** (Hypothesis): para una tabla, un número de filas nuevas de A y de B y la
   organización que mira, todos generados, el contexto ve exactamente sus filas.
5. **Mutación por tabla**: quitar la política de organización (``DROP POLICY``: el contexto ya no
   ve las suyas) o desactivar la seguridad a nivel de fila (``DISABLE``: ve las de la otra) de
   **cualquier** tabla hace fallar la comprobación de esa tabla. Cada mutación corre en una
   transacción que se revierte.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import MetaData, Table, Text, cast, event, func, select

from tests.factories import make_context
from tests.identity_db import IdentitySeed, MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.loop_db import LOOP_TABLES, SINGLE_ROW_TABLES, seed_loop
from tests.loop_db import ROW_BUILDERS as LOOP_ROW_BUILDERS
from tests.notifications_db import NOTIFICATIONS_TABLES, seed_notifications
from tests.notifications_db import ROW_BUILDERS as NOTIFICATIONS_ROW_BUILDERS
from tests.notifications_db import SINGLE_ROW_TABLES as NOTIFICATIONS_SINGLE_ROW_TABLES
from tests.outbox_support import app_database
from vigia_platform.loop.adapters.postgres import tables as loop_tables
from vigia_platform.notifications.adapters.postgres import tables as notifications_tables
from vigia_platform.shared.context import ContextAbsent

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class SchemaUnderTest:
    """Lo que un esquema de U-04 aporta a la prueba."""

    name: str
    tables: tuple[str, ...]
    seed: Callable[[Any, IdentitySeed], Awaitable[Any]]
    """Siembra filas de cada tabla en cada planta de A y de B (como superusuario)."""
    scopes: Callable[[Any, uuid.UUID], tuple[Any, ...]]
    """Las plantas sembradas de una organización (para construir filas nuevas)."""
    row_builders: Mapping[str, Callable[[Any], tuple[str, list[Any]]]]
    single_row_tables: frozenset[str]
    """Tablas con una sola fila por organización o planta: la propiedad no les añade filas."""
    metadata: MetaData
    """Las definiciones de SQLAlchemy Core del esquema (``adapters/postgres/tables.py``)."""

    def core(self, table: str) -> Table:
        return self.metadata.tables[f"{self.name}.{table}"]


SCHEMAS: Final = (
    SchemaUnderTest(
        name="loop",
        tables=LOOP_TABLES,
        seed=seed_loop,
        scopes=lambda seeded, organization_id: seeded.scopes[organization_id],
        row_builders=LOOP_ROW_BUILDERS,
        single_row_tables=SINGLE_ROW_TABLES,
        metadata=loop_tables.METADATA,
    ),
    SchemaUnderTest(
        name="notifications",
        tables=NOTIFICATIONS_TABLES,
        seed=seed_notifications,
        scopes=lambda seeded, organization_id: seeded.scopes[organization_id],
        row_builders=NOTIFICATIONS_ROW_BUILDERS,
        single_row_tables=NOTIFICATIONS_SINGLE_ROW_TABLES,
        metadata=notifications_tables.METADATA,
    ),
)
"""Los esquemas de U-04 bajo la prueba. TASK-338 añade aquí el suyo."""

TABLE_CASES: Final = [(schema, table) for schema in SCHEMAS for table in schema.tables]
TABLE_IDS: Final = [f"{schema.name}.{table}" for schema, table in TABLE_CASES]


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    identity: IdentitySeed
    seeded: dict[str, Any]
    """Lo que devolvió la siembra de cada esquema."""

    @property
    def a(self) -> uuid.UUID:
        return self.identity.a.organization_id

    @property
    def b(self) -> uuid.UUID:
        return self.identity.b.organization_id


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_pr_laz_04") as (database, identity):

        async def prepare() -> World:
            connection = await database.connect()
            try:
                seeded = {
                    schema.name: await schema.seed(connection, identity) for schema in SCHEMAS
                }
            finally:
                await connection.close()
            return World(database, identity, seeded)

        yield asyncio.run(prepare())


async def _rows_of(connection: Any, name: str, organization_id: uuid.UUID) -> set[str]:
    """La huella de cada fila de la organización, vista como superusuario (sin RLS)."""
    rows = await connection.fetch(
        f"SELECT md5(to_jsonb(t)::text) AS digest FROM {name} AS t"  # noqa: S608
        " WHERE organization_id = $1",
        organization_id,
    )
    return {str(row["digest"]) for row in rows}


async def _visible(connection: Any, name: str) -> list[tuple[uuid.UUID, str]]:
    rows = await connection.fetch(
        f"SELECT organization_id, md5(to_jsonb(t)::text) AS digest FROM {name} AS t"  # noqa: S608
    )
    return [(row["organization_id"], str(row["digest"])) for row in rows]


def _violations(
    name: str, viewer: uuid.UUID, visible: list[tuple[uuid.UUID, str]], own: set[str]
) -> list[str]:
    """Lo que la organización ``viewer`` ve de más (otra organización) o de menos (las suyas)."""
    violations: list[str] = []
    foreign = {organization for organization, _ in visible if organization != viewer}
    if foreign:
        violations.append(
            f"{name}: el contexto de {viewer} ve filas de {sorted(map(str, foreign))}"
        )
    seen = {digest for organization, digest in visible if organization == viewer}
    if seen != own:
        violations.append(f"{name}: el contexto de {viewer} ve {len(seen)} de sus {len(own)} filas")
    return violations


# --- 1. Cobertura ---------------------------------------------------------------------------------


@pytest.mark.parametrize("schema", SCHEMAS, ids=[schema.name for schema in SCHEMAS])
def test_every_table_of_the_schema_is_covered(schema: SchemaUnderTest, world: World) -> None:
    async def run() -> dict[str, bool]:
        connection = await world.database.connect()
        try:
            rows = await connection.fetch(
                "SELECT c.relname AS name, bool_or(a.attname = 'organization_id') AS tenant"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped"
                " WHERE n.nspname = $1 AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
                " GROUP BY c.relname",
                schema.name,
            )
        finally:
            await connection.close()
        return {str(row["name"]): bool(row["tenant"]) for row in rows}

    found = asyncio.run(run())
    assert set(found) == set(schema.tables)
    assert all(found.values()), found


@pytest.mark.parametrize(("schema", "table"), TABLE_CASES, ids=TABLE_IDS)
def test_both_organizations_have_rows_in_every_table(
    schema: SchemaUnderTest, table: str, world: World
) -> None:
    """La comprobación no pasa por una tabla vacía: A y B tienen filas en cada una."""

    async def run() -> tuple[int, int]:
        connection = await world.database.connect()
        try:
            name = f"{schema.name}.{table}"
            return (
                len(await _rows_of(connection, name, world.a)),
                len(await _rows_of(connection, name, world.b)),
            )
        finally:
            await connection.close()

    a, b = asyncio.run(run())
    assert a > 0 and b > 0


# --- 2 y 3. Por shared.db con ScopeContext, y sin contexto ----------------------------------------


def test_pr_laz_04_each_context_sees_only_its_organization(world: World) -> None:
    database = app_database(world.database)

    async def run() -> list[str]:
        superuser = await world.database.connect()
        violations: list[str] = []
        try:
            for viewer in (world.a, world.b):
                context = make_context(organization_id=viewer)
                for schema, table in TABLE_CASES:
                    name = f"{schema.name}.{table}"
                    row = schema.core(table).alias("t")
                    rows = await database.read(
                        context,
                        select(
                            row.c.organization_id,
                            func.md5(cast(func.to_jsonb(row.table_valued()), Text)).label("digest"),
                        ),
                    )
                    visible = [(row.organization_id, str(row.digest)) for row in rows]
                    violations += _violations(
                        name, viewer, visible, await _rows_of(superuser, name, viewer)
                    )
        finally:
            await superuser.close()
            await database.dispose()
        return violations

    assert asyncio.run(run()) == []


@pytest.mark.parametrize(("schema", "table"), TABLE_CASES, ids=TABLE_IDS)
def test_pr_laz_04_without_context_raises_before_any_query(
    schema: SchemaUnderTest, table: str, world: World
) -> None:
    database = app_database(world.database)
    checkouts: list[int] = []

    def count(*_: object) -> None:
        checkouts.append(1)

    async def run() -> None:
        engines = [pool.engine for pool in database._pools.values()]  # type: ignore[attr-defined]
        for engine in engines:
            event.listen(engine.sync_engine.pool, "checkout", count)
        try:
            with pytest.raises(ContextAbsent):
                await database.read(None, select(schema.core(table)))  # type: ignore[arg-type]
        finally:
            for engine in engines:
                event.remove(engine.sync_engine.pool, "checkout", count)
            await database.dispose()

    asyncio.run(run())
    assert checkouts == []


# --- 4. Propiedad con filas generadas -----


class _Rollback(Exception):
    """Revierte la transacción del ejemplo."""


@given(
    case=st.sampled_from(TABLE_CASES),
    extra_a=st.integers(min_value=0, max_value=3),
    extra_b=st.integers(min_value=0, max_value=3),
    viewer_is_a=st.booleans(),
)
def test_pr_laz_04_generated_rows_never_cross_organizations(
    world: World,
    case: tuple[SchemaUnderTest, str],
    extra_a: int,
    extra_b: int,
    viewer_is_a: bool,
) -> None:
    schema, table = case
    name = f"{schema.name}.{table}"
    seeded = world.seeded[schema.name]

    async def run() -> list[str]:
        connection = await world.database.connect()
        violations: list[str] = []
        try:
            async with connection.transaction():
                if table not in schema.single_row_tables:
                    for organization, extra in ((world.a, extra_a), (world.b, extra_b)):
                        # Los disparadores de las claves de notifications exigen el contexto.
                        await set_scope(connection, organization)
                        scope = schema.scopes(seeded, organization)[0]
                        for _ in range(extra):
                            sql, args = schema.row_builders[table](scope)
                            await connection.execute(sql, *args)
                viewer = world.a if viewer_is_a else world.b
                own = await _rows_of(connection, name, viewer)
                await connection.execute("SET LOCAL ROLE vigia_app")
                await set_scope(connection, viewer)
                violations = _violations(name, viewer, await _visible(connection, name), own)
                raise _Rollback  # solo si todo fue bien: un error se propaga tal cual
        except _Rollback:
            pass
        finally:
            await connection.close()
        return violations

    assert asyncio.run(run()) == []


# --- 5. Mutación por tabla -----

MUTATIONS: Final = {
    "sin la política de organización": "DROP POLICY organization_isolation ON {name}",
    "sin seguridad a nivel de fila": "ALTER TABLE {name} DISABLE ROW LEVEL SECURITY",
}


@pytest.mark.parametrize("mutation", list(MUTATIONS))
@pytest.mark.parametrize(("schema", "table"), TABLE_CASES, ids=TABLE_IDS)
def test_removing_the_policy_of_any_table_is_detected(
    schema: SchemaUnderTest, table: str, mutation: str, world: World
) -> None:
    name = f"{schema.name}.{table}"

    async def run() -> list[str]:
        connection = await world.database.connect()
        violations: list[str] = []
        try:
            async with connection.transaction():
                own = await _rows_of(connection, name, world.a)
                await connection.execute(MUTATIONS[mutation].format(name=name))
                await connection.execute("SET LOCAL ROLE vigia_app")
                await set_scope(connection, world.a)
                violations = _violations(name, world.a, await _visible(connection, name), own)
                raise _Rollback
        except _Rollback:
            pass
        finally:
            await connection.close()
        return violations

    violations = asyncio.run(run())
    assert violations, f"{mutation} en {name} no se detectó"
    assert all(violation.startswith(f"{name}:") for violation in violations)
