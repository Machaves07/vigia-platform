"""A-46 en ``catalog`` y ``fleet``: la segunda política RESTRICTIVE del proveedor (TASK-228).

Como ``vigia_app`` sobre una base migrada con los clientes A y B de ``tests/identity_db.py`` (dos
plantas cada uno) y una fila de cada tabla de ``catalog`` (``tests/catalog_db.py``) y de ``fleet``
(``tests/fleet_db.py``) en cada planta:

- **estructura**: toda tabla de ``catalog`` y ``fleet`` con ``organization_id`` (las que hay hoy y
  las que se añadan) tiene ``provider_concession_scope`` **RESTRICTIVE**, para todas las órdenes,
  con ``identity.rls_provider_scope_allows(organization_id, plant_id)`` en ``USING`` y en
  ``WITH CHECK`` (la marca por organización, sin planta, con ``NULL``); toda tabla con zona tiene
  también planta; y la función exige la concesión **vigente** (activa, sin revocar, ya concedida y
  sin vencer);
- **comportamiento**, con el contexto del proveedor (``actor_kind = provider_user``): con la
  concesión vigente de la planta 0 de A ve exactamente las filas de esa planta en **cada** tabla
  (la prueba no pasa por una consulta vacía); con una concesión vencida (también la que sigue
  ``active`` porque la tarea de vencimiento aún no pasó), revocada o de la otra planta, ni una fila
  de la planta 0; con la concesión de A en el contexto de B, nada;
- **sondas negativas**: la política convertida en PERMISSIVE, o la vigencia quitada (de la función
  y de ``provider_concession_own``, las dos capas que la sostienen), hacen fallar la comprobación
  (dentro de transacciones revertidas); quitarla solo de la función lo detecta la comprobación de
  estructura.

La aplicación (rutas bajo concesión, A-48) está en ``test_route_isolation.py``. Solo datos
generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
import pytest_asyncio

from tests.catalog_db import CATALOG_TABLES, CatalogSeed, seed_catalog
from tests.fleet_db import FLEET_TABLES, ORGANIZATION_TABLES, FleetSeed, seed_fleet
from tests.identity_db import MigratedDatabase, insert_concession, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

PROVIDER: Final = "provider_user"
SCHEMAS: Final = ("catalog", "fleet")
PLANT_TABLES: Final = (
    *(f"catalog.{table}" for table in CATALOG_TABLES),
    *(f"fleet.{table}" for table in FLEET_TABLES if table not in ORGANIZATION_TABLES),
)
"""Las tablas sembradas con planta: la comprobación de comportamiento las recorre todas."""
VIGENCY: Final = (
    "status = 'active'",
    "revoked_at IS NULL",
    "granted_at <= pg_catalog.now()",
    "expires_at > pg_catalog.now()",
)
"""Las condiciones de vigencia de ``identity.rls_provider_scope_allows`` (``nuc_0009``)."""


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    catalog: CatalogSeed
    fleet: FleetSeed
    concessions: dict[str, uuid.UUID]
    """``active`` (planta 0 de A), ``other_plant`` (planta 1 de A), ``revoked``, ``expired`` y
    ``unswept`` (``active`` pero ya vencida: la tarea de vencimiento aún no pasó)."""

    @property
    def a(self) -> uuid.UUID:
        return self.catalog.identity.a.organization_id

    @property
    def b(self) -> uuid.UUID:
        return self.catalog.identity.b.organization_id

    @property
    def conceded(self) -> uuid.UUID:
        return self.catalog.identity.a.plants[0].plant_id

    @property
    def other_plant(self) -> uuid.UUID:
        return self.catalog.identity.a.plants[1].plant_id


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_provider_policy_gob") as (database, identity):

        async def prepare() -> World:
            connection = await database.connect()
            try:
                catalog = await seed_catalog(connection, identity)
                fleet = await seed_fleet(connection, identity)
                a = identity.a
                plants = [plant.plant_id for plant in a.plants]
                past = {"granted_offset": -dt.timedelta(days=10), "duration": dt.timedelta(days=2)}
                specs: dict[str, tuple[uuid.UUID, str, dict[str, Any]]] = {
                    "active": (plants[0], "active", {}),
                    "other_plant": (plants[1], "active", {}),
                    "revoked": (plants[0], "revoked", {}),
                    "expired": (plants[0], "expired", past),
                    "unswept": (plants[0], "active", past),
                }
                concessions = {
                    name: await insert_concession(
                        connection,
                        identity,
                        a.organization_id,
                        scope_level="plant",
                        scope_id=plant,
                        status=status,
                        **extra,
                    )
                    for name, (plant, status, extra) in specs.items()
                }
            finally:
                await connection.close()
            return World(database, catalog, fleet, concessions)

        yield asyncio.run(prepare())


@pytest_asyncio.fixture
async def app(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def superuser(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la sonda."""


# --- Estructura ----------------------------------------------------------------------------------


async def _organization_tables(connection: Any) -> dict[str, set[str]]:
    """``esquema.tabla`` → columnas, de cada tabla (no partición) con ``organization_id``."""
    rows = await connection.fetch(
        "SELECT n.nspname || '.' || c.relname AS name, array_agg(a.attname::text) AS columns"
        " FROM pg_catalog.pg_class AS c"
        " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
        " JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid"
        " WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        " AND a.attnum > 0 AND NOT a.attisdropped GROUP BY 1",
        list(SCHEMAS),
    )
    return {row["name"]: set(row["columns"]) for row in rows if "organization_id" in row["columns"]}


@pytest.mark.asyncio
async def test_every_table_with_organization_has_the_restrictive_provider_policy(
    superuser: Any,
) -> None:
    tables = await _organization_tables(superuser)
    assert set(PLANT_TABLES) <= set(tables)
    failures: list[str] = []
    for name, columns in sorted(tables.items()):
        if "zone_id" in columns and "plant_id" not in columns:
            failures.append(f"{name}: zona sin planta")
        policy = await superuser.fetchrow(
            "SELECT p.polpermissive, p.polcmd::text AS cmd,"
            " pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS qual,"
            " pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS checked"
            " FROM pg_catalog.pg_policy AS p WHERE p.polrelid = $1::regclass"
            " AND p.polname = 'provider_concession_scope'",
            name,
        )
        if policy is None:
            failures.append(f"{name}: sin provider_concession_scope")
            continue
        plant = "plant_id" if "plant_id" in columns else "NULL::uuid"
        expected = f"identity.rls_provider_scope_allows(organization_id, {plant})"
        if policy["polpermissive"]:
            failures.append(f"{name}: la política es PERMISSIVE")
        if policy["cmd"] != "*":
            failures.append(f"{name}: solo para {policy['cmd']}")
        for label in ("qual", "checked"):
            if policy[label] != expected:
                failures.append(f"{name}: {label} = {policy[label]}")
    assert not failures, "\n".join(failures)
    definition = await superuser.fetchval(
        "SELECT pg_catalog.pg_get_functiondef('identity.rls_provider_scope_allows(uuid, uuid)'"
        "::regprocedure)"
    )
    missing = [condition for condition in VIGENCY if condition not in definition]
    assert not missing, missing


# --- Comportamiento ------------------------------------------------------------------------------


async def _visible_plants(
    connection: Any, organization_id: uuid.UUID, concession_id: uuid.UUID, table: str
) -> set[uuid.UUID | None]:
    async with connection.transaction():
        await set_scope(
            connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id
        )
        rows = await connection.fetch(f"SELECT DISTINCT plant_id FROM {table}")  # noqa: S608
    return {row["plant_id"] for row in rows}


async def _leaks(
    connection: Any, world: World, tables: tuple[str, ...] = PLANT_TABLES
) -> list[str]:
    """Lo que ve cada concesión de más (o de menos, con la vigente) en cada tabla con planta."""
    leaks: list[str] = []
    concessions = world.concessions
    for table in tables:
        seen = await _visible_plants(connection, world.a, concessions["active"], table)
        if seen != {world.conceded}:
            leaks.append(f"{table}: la vigente ve {seen}")
        seen = await _visible_plants(connection, world.a, concessions["other_plant"], table)
        if world.conceded in seen or world.other_plant not in seen:
            leaks.append(f"{table}: la de la otra planta ve {seen}")
        for name in ("revoked", "expired", "unswept"):
            seen = await _visible_plants(connection, world.a, concessions[name], table)
            if seen:
                leaks.append(f"{table}: la {name} ve {seen}")
        seen = await _visible_plants(connection, world.b, concessions["active"], table)
        if seen:
            leaks.append(f"{table}: en el contexto de B ve {seen}")
    return leaks


@pytest.mark.asyncio
async def test_only_the_valid_concession_sees_its_plant_in_every_table(
    app: Any, world: World
) -> None:
    assert await _leaks(app, world) == []


# --- Sondas negativas ---------------------------------------------------------------------------


_WITHOUT_VIGENCY: Final = """
CREATE OR REPLACE FUNCTION identity.rls_provider_scope_allows(
    row_organization_id uuid, row_plant_id uuid
)
    RETURNS boolean
    LANGUAGE sql
    STABLE
AS $$
    SELECT NOT identity.rls_provider_context()
        OR EXISTS (
            SELECT
            FROM identity.provider_concession AS concession
            WHERE concession.concession_id
                    = NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid
                AND concession.organization_id = row_organization_id
                AND (
                    concession.scope_level = 'organization'
                    OR (concession.scope_level = 'plant' AND concession.scope_id = row_plant_id)
                )
        )
$$
"""
"""La función de ``nuc_0009`` sin las condiciones de vigencia (sonda negativa)."""


async def _probe_leaks(
    superuser: Any, world: World, change: list[str], tables: tuple[str, ...] = PLANT_TABLES
) -> list[str]:
    """Las fugas con ``change`` aplicado, como ``vigia_app``, en una transacción revertida."""
    leaks: list[str] = []
    try:
        async with superuser.transaction():
            for statement in change:
                await superuser.execute(statement)
            await superuser.execute("SET LOCAL ROLE vigia_app")
            leaks = await _leaks(superuser, world, tables)
            raise _Rollback
    except _Rollback:
        pass
    return leaks


@pytest.mark.asyncio
@pytest.mark.parametrize("table", PLANT_TABLES)
async def test_a_permissive_provider_policy_is_detected(
    table: str, superuser: Any, world: World
) -> None:
    permissive = [
        f"DROP POLICY provider_concession_scope ON {table}",
        f"CREATE POLICY provider_concession_scope ON {table} AS PERMISSIVE FOR ALL TO PUBLIC"
        " USING (identity.rls_provider_scope_allows(organization_id, plant_id))"
        " WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))",
    ]
    leaks = await _probe_leaks(superuser, world, permissive, (table,))
    # PERMISSIVE se suma a ``organization_isolation`` (O lógico): la concesión ve las dos plantas.
    assert any(leak.startswith(f"{table}: la vigente ve") for leak in leaks), leaks


@pytest.mark.asyncio
async def test_a_provider_scope_without_the_vigency_condition_is_detected(
    superuser: Any, world: World
) -> None:
    # La vigencia está en dos capas: la función y ``provider_concession_own`` de
    # ``identity.provider_concession`` (la función lee la concesión bajo esa RLS). Sin las dos, la
    # concesión revocada o vencida ve la planta en cada tabla.
    without_both = [
        _WITHOUT_VIGENCY,
        "DROP POLICY provider_concession_own ON identity.provider_concession",
    ]
    leaks = await _probe_leaks(superuser, world, without_both)
    for table in PLANT_TABLES:
        for name in ("revoked", "expired", "unswept"):
            assert any(leak.startswith(f"{table}: la {name} ve") for leak in leaks), (table, name)
    # Sin la de la función, la otra capa todavía la sostiene en la base, y la comprobación
    # estructural de la definición nombra cada condición que falta.
    assert await _probe_leaks(superuser, world, [_WITHOUT_VIGENCY]) == []
    assert [c for c in VIGENCY if c not in _WITHOUT_VIGENCY] == list(VIGENCY)
