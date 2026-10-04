"""Aislamiento de ``fleet`` en la base: organización y concesión de proveedor (TASK-203).

Criterio de TASK-203 (BR-NUC-01, BR-NUC-04, PR-NUC-52, adenda A-46): con dos organizaciones y una
concesión de proveedor de **una sola planta**, el proveedor no ve ni escribe filas de otra planta
ni de otra organización en **ninguna** tabla de ``fleet``; y la prueba falla si se quita
``provider_concession_scope`` de una tabla (sonda negativa).

Datos: la proveedora y los clientes A y B de ``tests/identity_db.py`` (dos plantas cada uno) con
filas de cada una de las 17 tablas en cada planta (``tests/fleet_db.py``), la marca por
organización y un intento de alta sin nodo (sin planta) por organización, y una concesión vigente
del instalador sobre la planta 0 de A. Todo con ``vigia_app`` y el ``ScopeContext`` del proveedor
(``actor_kind = provider_user`` y ``concession_id``), en transacciones revertidas.

Las filas sin planta (``revocation_list_dirty`` y el intento de un nodo desconocido) solo las
alcanza una concesión de toda la organización: con la de una planta no se ven ni se escriben.
``open_fleet_alarm`` solo la escriben los disparadores de ``fleet_alarm``: ``vigia_app`` no tiene
``INSERT``, así que su sonda negativa se apoya en la lectura.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.fleet_db import (
    FLEET_TABLES,
    ORGANIZATION_TABLES,
    READ_ONLY_FOR_APP,
    ROW_BUILDERS,
    SINGLETONS,
    FleetScope,
    FleetSeed,
    fleet_alarm,
    seed_fleet,
)
from tests.identity_db import MigratedDatabase, insert_concession, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

PROVIDER = "provider_user"
INSUFFICIENT_PRIVILEGE = "42501"
UNIQUE_VIOLATION = "23505"
BACKEND = Path(__file__).resolve().parents[2]


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gob_0018_isolation", BACKEND / "migrations" / "versions" / "gob_0018_fleet_schema.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()
UPDATABLE = {table: MIGRATION.app_updatable_columns(table) for table in FLEET_TABLES}


def _plant(table: str) -> str:
    return "NULL::uuid" if table in ORGANIZATION_TABLES else "plant_id"


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    seed: FleetSeed
    concession_id: uuid.UUID
    revoked_id: uuid.UUID
    expired_id: uuid.UUID
    organization_concession_id: uuid.UUID

    @property
    def conceded(self) -> FleetScope:
        return self.seed.plant(self.seed.identity.a.organization_id, 0)

    @property
    def other_plant(self) -> FleetScope:
        return self.seed.plant(self.seed.identity.a.organization_id, 1)

    @property
    def other_organization(self) -> FleetScope:
        return self.seed.plant(self.seed.identity.b.organization_id, 0)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_fleet_rls") as (database, identity):

        async def prepare() -> World:
            connection = await database.connect()
            try:
                seed = await seed_fleet(connection, identity)
                a = identity.a
                plant_id = a.plants[0].plant_id
                concessions = [
                    await insert_concession(
                        connection,
                        identity,
                        a.organization_id,
                        scope_level="plant",
                        scope_id=plant_id,
                        status=status,
                        **extra,
                    )
                    for status, extra in (
                        ("active", {}),
                        ("revoked", {}),
                        (
                            "expired",
                            {
                                "granted_offset": -dt.timedelta(days=10),
                                "duration": dt.timedelta(days=2),
                            },
                        ),
                    )
                ]
            finally:
                await connection.close()
            # La concesión de toda la organización que siembra seed_identity.
            return World(database, seed, *concessions, identity.concessions[a.organization_id])

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
    """Revierte la transacción de la prueba."""


async def _visible(
    connection: Any, organization_id: uuid.UUID, concession_id: uuid.UUID, table: str
) -> set[tuple[uuid.UUID, uuid.UUID | None]]:
    """(organización, planta) de las filas de ``table`` que ve el contexto del proveedor."""
    await set_scope(connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id)
    rows = await connection.fetch(
        f"SELECT DISTINCT organization_id, {_plant(table)} AS plant_id"  # noqa: S608
        f" FROM fleet.{table}"
    )
    return {(row["organization_id"], row["plant_id"]) for row in rows}


async def _insert_as_provider(
    connection: Any,
    organization_id: uuid.UUID,
    concession_id: uuid.UUID,
    target: FleetScope,
    table: str,
) -> str:
    """``ok`` o el SQLSTATE del alta de una fila nueva de ``table`` en ``target`` (revertida)."""
    sql, args = ROW_BUILDERS[table](target)
    try:
        async with connection.transaction():
            await set_scope(
                connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id
            )
            await connection.execute(sql, *args)
            raise _Rollback
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)
    except _Rollback:
        return "ok"


def _expected_view(world: World, table: str) -> set[tuple[uuid.UUID, uuid.UUID | None]]:
    """Lo que debe ver la concesión de la planta 0 de A: nada en las tablas sin planta."""
    conceded = world.conceded
    if table in ORGANIZATION_TABLES:
        return set()
    return {(conceded.organization_id, conceded.plant_id)}


async def _read_leaks(connection: Any, world: World, table: str) -> list[str]:
    leaks: list[str] = []
    conceded = world.conceded
    async with connection.transaction():
        seen = await _visible(connection, conceded.organization_id, world.concession_id, table)
    if seen != _expected_view(world, table):
        leaks.append(f"{table}: ve {seen}")
    other = world.other_organization
    async with connection.transaction():
        seen = await _visible(connection, other.organization_id, world.concession_id, table)
    if seen:
        leaks.append(f"{table}: ve la otra organización {seen}")
    return leaks


async def _write_leaks(connection: Any, world: World, table: str) -> list[str]:
    leaks: list[str] = []
    for target in (world.other_plant, world.other_organization):
        result = await _insert_as_provider(
            connection, target.organization_id, world.concession_id, target, table
        )
        if result != INSUFFICIENT_PRIVILEGE:
            leaks.append(f"{table}: escribe en {target.plant_id} ({result})")
    return leaks


# --- Lectura y escritura -----


@pytest.mark.asyncio
@pytest.mark.parametrize("table", FLEET_TABLES)
async def test_provider_sees_only_the_conceded_plant(table: str, app: Any, world: World) -> None:
    assert await _read_leaks(app, world, table) == []
    # Un usuario del cliente (sin concesión) sigue viendo sus dos plantas (y las filas sin planta):
    # la política solo acota al proveedor.
    a = world.conceded
    async with app.transaction():
        await set_scope(app, a.organization_id)
        rows = await app.fetch(
            f"SELECT DISTINCT {_plant(table)} AS plant_id FROM fleet.{table}"  # noqa: S608
        )
    seen = {row["plant_id"] for row in rows}
    if table in ORGANIZATION_TABLES:
        assert seen == {None}
    else:
        assert (
            {a.plant_id, world.other_plant.plant_id}
            <= seen
            <= {
                a.plant_id,
                world.other_plant.plant_id,
                None,
            }
        )


@pytest.mark.asyncio
async def test_rows_without_plant_need_an_organization_concession(app: Any, world: World) -> None:
    """El intento de un nodo desconocido y la marca por organización: invisibles con la concesión
    de una planta, visibles con la de toda la organización."""
    a = world.conceded
    for table, condition in (
        ("enrollment_attempt", "plant_id IS NULL"),
        ("revocation_list_dirty", "true"),
    ):
        for concession_id, expected in (
            (world.concession_id, 0),
            (world.organization_concession_id, 1),
        ):
            async with app.transaction():
                await set_scope(
                    app, a.organization_id, actor_kind=PROVIDER, concession_id=concession_id
                )
                count = await app.fetchval(
                    f"SELECT count(*) FROM fleet.{table} WHERE {condition}"  # noqa: S608
                )
            assert count == expected, (table, concession_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", FLEET_TABLES)
async def test_provider_writes_only_in_the_conceded_plant(
    table: str, app: Any, world: World
) -> None:
    assert await _write_leaks(app, world, table) == []
    conceded = world.conceded
    result = await _insert_as_provider(
        app, conceded.organization_id, world.concession_id, conceded, table
    )
    # En la planta concedida la RLS deja pasar; las de una fila por clave ya tienen la suya (clave
    # duplicada, después de la política). Sin planta, ni en la organización concedida; y la ranura
    # de la alarma abierta no se escribe nunca directamente.
    if table in ORGANIZATION_TABLES or table in READ_ONLY_FOR_APP:
        expected = INSUFFICIENT_PRIVILEGE
    elif table in SINGLETONS:
        expected = UNIQUE_VIOLATION
    else:
        expected = "ok"
    assert result == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("table", [t for t in FLEET_TABLES if UPDATABLE[t]])
async def test_provider_updates_only_rows_of_the_conceded_plant(
    table: str, app: Any, world: World
) -> None:
    column = UPDATABLE[table][0]
    conceded = world.conceded
    try:
        async with app.transaction():
            await set_scope(
                app,
                conceded.organization_id,
                actor_kind=PROVIDER,
                concession_id=world.concession_id,
            )
            status = await app.fetchval(
                f"WITH changed AS (UPDATE fleet.{table} SET {column} = {column}"  # noqa: S608
                f" RETURNING {_plant(table)} AS plant_id)"
                " SELECT array_agg(DISTINCT plant_id) FROM changed"
            )
            raise _Rollback
    except _Rollback:
        pass
    if table in ORGANIZATION_TABLES:
        assert status is None
    else:
        assert set(status) == {conceded.plant_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["revoked", "expired"])
async def test_revoked_or_expired_concession_sees_nothing(
    which: str, app: Any, world: World
) -> None:
    concession_id = world.revoked_id if which == "revoked" else world.expired_id
    conceded = world.conceded
    for table in FLEET_TABLES:
        async with app.transaction():
            assert await _visible(app, conceded.organization_id, concession_id, table) == set()
        result = await _insert_as_provider(
            app, conceded.organization_id, concession_id, conceded, table
        )
        assert result == INSUFFICIENT_PRIVILEGE, table


@pytest.mark.asyncio
async def test_provider_actor_without_concession_sees_nothing(app: Any, world: World) -> None:
    # Fallo cerrado: basta actor_kind de proveedor, aun sin concession_id.
    conceded = world.conceded
    for table in FLEET_TABLES:
        async with app.transaction():
            await set_scope(app, conceded.organization_id, actor_kind=PROVIDER)
            count = await app.fetchval(f"SELECT count(*) FROM fleet.{table}")  # noqa: S608
        assert count == 0, table


@pytest.mark.asyncio
async def test_an_alarm_raised_by_the_provider_takes_the_slot_of_its_plant_only(
    app: Any, world: World
) -> None:
    """Los disparadores de la alarma (``SECURITY DEFINER``) siguen bajo la RLS forzada: el
    proveedor abre una alarma en la planta concedida y su ranura queda en esa planta."""
    conceded = world.conceded
    try:
        async with app.transaction():
            await set_scope(
                app,
                conceded.organization_id,
                actor_kind=PROVIDER,
                concession_id=world.concession_id,
            )
            # Abierta, de una clase que la planta todavía no tiene abierta.
            sql, args = fleet_alarm(conceded, kind="clock_drift", cleared=False)
            await app.execute(sql, *args)
            slots = await app.fetch(
                "SELECT plant_id, alarm_kind FROM fleet.open_fleet_alarm WHERE alarm_id = $1",
                args[0],
            )
            raise _Rollback
    except _Rollback:
        pass
    assert [(row["plant_id"], row["alarm_kind"]) for row in slots] == [
        (conceded.plant_id, "clock_drift")
    ]


# --- Sonda negativa -----


@pytest.mark.asyncio
@pytest.mark.parametrize("table", FLEET_TABLES)
async def test_removing_the_provider_policy_is_detected(
    table: str, superuser: Any, world: World
) -> None:
    """Sin ``provider_concession_scope`` en ``table``, las comprobaciones de arriba fallan.

    Superusuario que quita la política y pasa a ``vigia_app`` dentro de una transacción que se
    revierte siempre: la base de las demás pruebas no cambia.
    """
    try:
        async with superuser.transaction():
            await superuser.execute(f"DROP POLICY provider_concession_scope ON fleet.{table}")
            await superuser.execute("SET LOCAL ROLE vigia_app")
            async with superuser.transaction():  # punto de guardado: las lecturas
                read = await _read_leaks(superuser, world, table)
            write = await _write_leaks(superuser, world, table)
            raise _Rollback
    except _Rollback:
        pass
    assert read, read
    if table not in READ_ONLY_FOR_APP:
        assert write, write
