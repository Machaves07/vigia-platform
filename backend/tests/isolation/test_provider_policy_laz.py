"""PAT-LAZ-SEG-03 y A-69 en ``loop``, ``notifications`` y ``accreditation``: las políticas
RESTRICTIVE del proveedor (TASK-301, TASK-339 y TASK-338).

Sobre una base migrada con los clientes A y B de ``tests/identity_db.py`` (dos plantas cada uno),
una fila de cada tabla de ``loop``, ``notifications`` y ``accreditation`` en cada planta
(``tests/loop_db.py``, ``tests/notifications_db.py`` y ``tests/accreditation_db.py``; las nueve
de ``accreditation`` tienen planta) y concesiones de la planta 0 de A vigente, de la planta 1,
revocada, vencida y ``unswept`` (``active`` pero ya vencida: la tarea de vencimiento aún no
pasó), más la de toda la organización de la siembra:

- **estructura**: toda tabla de los tres esquemas con planta tiene ``provider_concession_scope``
  RESTRICTIVE para todas las órdenes con ``identity.rls_provider_scope_allows(organization_id,
  plant_id)`` en ``USING`` y ``WITH CHECK``; las de solo organización (A-69: cinco de ``loop`` y,
  en ``notifications``, ``user_notification_preference``, ``digest_batch``, ``email_message`` y
  su clave) tienen ``provider_context_denied`` RESTRICTIVE con
  ``NOT identity.rls_provider_context()``; ninguna tabla queda sin política de proveedor, y la
  función exige la concesión **vigente**;
- **comportamiento** con el contexto del proveedor (``actor_kind = provider_user``), como
  ``vigia_app`` y, en una base migrada por un **maestro sin SUPERUSER** (como ``vigia_owner`` en
  RDS), como ``vigia_app`` y como el dueño ``vigia_migrate``:
  * tablas con planta: la concesión vigente de la planta 0 ve exactamente esa planta; la de la
    otra planta no la ve; la revocada, la vencida y la ``unswept`` no ven nada; con la concesión
    de A en el contexto de B, nada; y nadie escribe una fila en una planta fuera de su concesión;
  * un aviso de la organización (sin planta) solo lo ve la concesión de toda la organización;
  * las de solo organización: bajo **cualquier** concesión (también la de toda la organización),
    ni una fila visible ni una escritura admitida;
- **sondas negativas** (en transacciones revertidas): la política de proveedor convertida en
  PERMISSIVE, la vigencia quitada (de la función y de ``provider_concession_own``) o la política
  de A-69 convertida en PERMISSIVE hacen fallar la comprobación.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.accreditation_db import ACCREDITATION_TABLES, AccreditationSeed, seed_accreditation
from tests.accreditation_db import ROW_BUILDERS as ACCREDITATION_ROW_BUILDERS
from tests.identity_db import (
    IdentitySeed,
    insert_concession,
    seed_identity,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles_rds_master import (
    MASTER,
    RdsLikeCluster,
    cluster,  # noqa: F401 - fixture del clúster con maestro sin SUPERUSER
)
from tests.integration.test_roles_rds_master import _master_database as master_database
from tests.integration.test_roles_rds_master import _upgrade as upgrade_as_master
from tests.loop_db import LOOP_TABLES, LoopSeed, seed_loop
from tests.loop_db import ORGANIZATION_ONLY_TABLES as LOOP_ORGANIZATION_ONLY
from tests.loop_db import PLANT_TABLES as LOOP_PLANT_TABLES
from tests.loop_db import ROW_BUILDERS as LOOP_ROW_BUILDERS
from tests.loop_db import SINGLE_ROW_TABLES as LOOP_SINGLE_ROW_TABLES
from tests.notifications_db import (
    KEY_TABLES,
    NOTIFICATIONS_TABLES,
    NotificationsSeed,
    notification,
    seed_notifications,
)
from tests.notifications_db import ORGANIZATION_ONLY_TABLES as NOTIFICATIONS_ORGANIZATION_ONLY
from tests.notifications_db import PLANT_TABLES as NOTIFICATIONS_PLANT_TABLES
from tests.notifications_db import ROW_BUILDERS as NOTIFICATIONS_ROW_BUILDERS

pytestmark = pytest.mark.integration

PROVIDER: Final = "provider_user"
INSUFFICIENT_PRIVILEGE: Final = "42501"
UNIQUE_VIOLATION: Final = "23505"
SCHEMAS: Final = ("loop", "notifications", "accreditation")
ALL_TABLES: Final = (
    tuple(f"loop.{table}" for table in LOOP_TABLES)
    + tuple(f"notifications.{table}" for table in NOTIFICATIONS_TABLES)
    + tuple(f"accreditation.{table}" for table in ACCREDITATION_TABLES)
)
"""Las tablas de los tres esquemas, con su esquema."""
ORGANIZATION_ONLY_TABLES: Final = tuple(
    f"loop.{table}" for table in LOOP_ORGANIZATION_ONLY
) + tuple(f"notifications.{table}" for table in NOTIFICATIONS_ORGANIZATION_ONLY)
"""Las de solo organización (A-69): ninguna fila bajo una concesión de proveedor."""
SCOPED_TABLES: Final = (
    tuple(f"loop.{table}" for table in LOOP_PLANT_TABLES if table not in LOOP_ORGANIZATION_ONLY)
    + tuple(f"notifications.{table}" for table in NOTIFICATIONS_PLANT_TABLES)
    + tuple(f"accreditation.{table}" for table in ACCREDITATION_TABLES)
)
"""Las tablas con planta que la concesión acota por planta (las demás no dejan nada)."""
WRITABLE_SCOPED_TABLES: Final = tuple(
    name for name in SCOPED_TABLES if name.split(".")[1] not in KEY_TABLES
)
"""Las que ``vigia_app`` escribe directamente (las claves las llena el disparador de su tabla)."""
SINGLE_ROW_TABLES: Final = frozenset(f"loop.{table}" for table in LOOP_SINGLE_ROW_TABLES)
VIGENCY: Final = (
    "status = 'active'",
    "revoked_at IS NULL",
    "granted_at <= pg_catalog.now()",
    "expires_at > pg_catalog.now()",
)
"""Las condiciones de vigencia de ``identity.rls_provider_scope_allows`` (``nuc_0009``)."""
CONCESSIONS: Final = ("active", "other_plant", "revoked", "expired", "unswept")

Connect = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class World:
    connect: Connect
    """Abre una conexión: sin rol, como superusuario; con rol, como ese rol."""
    loop: LoopSeed
    notifications: NotificationsSeed
    accreditation: AccreditationSeed
    concessions: dict[str, uuid.UUID]
    """Las de ``CONCESSIONS`` (todas de A) y ``organization``, la de toda la organización A."""

    @property
    def identity(self) -> IdentitySeed:
        return self.loop.identity

    @property
    def a(self) -> uuid.UUID:
        return self.identity.a.organization_id

    @property
    def b(self) -> uuid.UUID:
        return self.identity.b.organization_id

    @property
    def conceded(self) -> uuid.UUID:
        return self.identity.a.plants[0].plant_id

    @property
    def other_plant(self) -> uuid.UUID:
        return self.identity.a.plants[1].plant_id

    def row(self, name: str, plant: int) -> tuple[str, list[Any]]:
        """Una fila nueva de ``name`` (con esquema) en la planta ``plant`` de A."""
        schema, table = name.split(".")
        if schema == "loop":
            return LOOP_ROW_BUILDERS[table](self.loop.plant(self.a, plant))
        if schema == "accreditation":
            return ACCREDITATION_ROW_BUILDERS[table](self.accreditation.plant(self.a, plant))
        return NOTIFICATIONS_ROW_BUILDERS[table](self.notifications.plant(self.a, plant))


async def _prepare(connect: Connect, identity: IdentitySeed) -> World:
    connection = await connect()
    try:
        loop = await seed_loop(connection, identity)
        notifications = await seed_notifications(connection, identity)
        accreditation = await seed_accreditation(connection, identity)
        plants = [plant.plant_id for plant in identity.a.plants]
        past: dict[str, Any] = {
            "granted_offset": -dt.timedelta(days=10),
            "duration": dt.timedelta(days=2),
        }
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
                identity.a.organization_id,
                scope_level="plant",
                scope_id=plant,
                status=status,
                **extra,
            )
            for name, (plant, status, extra) in specs.items()
        }
        concessions["organization"] = identity.concessions[identity.a.organization_id]
    finally:
        await connection.close()
    return World(connect, loop, notifications, accreditation, concessions)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_provider_policy_laz") as (database, identity):
        yield asyncio.run(_prepare(database.connect, identity))


@pytest_asyncio.fixture
async def app(world: World) -> AsyncIterator[Any]:
    connection = await world.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def superuser(world: World) -> AsyncIterator[Any]:
    connection = await world.connect()
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la sonda."""


# --- Estructura -----


@pytest.mark.asyncio
async def test_every_u04_table_has_a_restrictive_provider_policy(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT n.nspname || '.' || c.relname AS name, p.polname AS policy, p.polpermissive,"
        " p.polcmd::text AS cmd,"
        " pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS qual,"
        " pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS checked,"
        " EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid = c.oid"
        "  AND a.attname = 'plant_id' AND NOT a.attisdropped) AS has_plant,"
        " EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid = c.oid"
        "  AND a.attname = 'zone_id' AND NOT a.attisdropped) AS has_zone"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_policy p ON p.polrelid = c.oid AND p.polname <> 'organization_isolation'"
        " WHERE n.nspname = ANY ($1::text[]) AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
        list(SCHEMAS),
    )
    policies: dict[str, dict[str, Any]] = {}
    plant: dict[str, bool] = {}
    failures: list[str] = []
    for row in rows:
        plant[row["name"]] = row["has_plant"]
        if row["has_zone"] and not row["has_plant"]:
            failures.append(f"{row['name']}: zona sin planta")
        if row["policy"] is not None:
            policies.setdefault(row["name"], {})[row["policy"]] = row
    assert set(plant) == set(ALL_TABLES)
    expected_scope = "identity.rls_provider_scope_allows(organization_id, plant_id)"
    expected_denied = "(NOT identity.rls_provider_context())"
    for name in sorted(plant):
        named = dict(policies.get(name, {}))
        lookup = named.pop("email_provider_message_lookup", None)
        if name == "notifications.email_provider_message_key":
            # laz_0036 (TASK-325): como identity.invitation_lookup (nuc_0011), PERMISSIVE FOR
            # SELECT solo dentro de notifications.email_provider_message_lookup, aparte de las
            # RESTRICTIVE de proveedor comprobadas abajo.
            if lookup is None:
                failures.append(f"{name}: falta la política email_provider_message_lookup")
            elif not lookup["polpermissive"] or lookup["cmd"] != "r":
                failures.append(
                    f"{name}: email_provider_message_lookup no es PERMISSIVE FOR SELECT"
                )
        elif lookup is not None:
            failures.append(f"{name}: email_provider_message_lookup inesperada aquí")
        wanted: dict[str, str] = {}
        if plant[name]:
            wanted["provider_concession_scope"] = expected_scope
        if name in ORGANIZATION_ONLY_TABLES or not plant[name]:
            wanted["provider_context_denied"] = expected_denied
        if set(named) != set(wanted):
            failures.append(f"{name}: políticas {sorted(named)}, se esperaban {sorted(wanted)}")
            continue
        for policy, expression in wanted.items():
            row = named[policy]
            if row["polpermissive"]:
                failures.append(f"{name}: {policy} es PERMISSIVE")
            if row["cmd"] != "*":
                failures.append(f"{name}: {policy} solo para {row['cmd']}")
            for label in ("qual", "checked"):
                if row[label] != expression:
                    failures.append(f"{name}: {policy} {label} = {row[label]}")
    assert not failures, "\n".join(failures)
    assert set(ORGANIZATION_ONLY_TABLES) >= {name for name, has in plant.items() if not has}
    # A-69 en notifications: las preferencias, los lotes y los correos no dejan nada.
    assert {
        "notifications.user_notification_preference",
        "notifications.digest_batch",
        "notifications.email_message",
    } <= set(ORGANIZATION_ONLY_TABLES)
    definition = await superuser.fetchval(
        "SELECT pg_catalog.pg_get_functiondef('identity.rls_provider_scope_allows(uuid, uuid)'"
        "::regprocedure)"
    )
    assert [condition for condition in VIGENCY if condition not in definition] == []


# --- Comportamiento -----


async def _visible_plants(
    connection: Any, organization_id: uuid.UUID, concession_id: uuid.UUID, name: str
) -> set[uuid.UUID | None]:
    async with connection.transaction():
        await set_scope(
            connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id
        )
        rows = await connection.fetch(f"SELECT DISTINCT plant_id FROM {name}")  # noqa: S608
    return {row["plant_id"] for row in rows}


async def _visible_rows(
    connection: Any, organization_id: uuid.UUID, concession_id: uuid.UUID, name: str
) -> int:
    async with connection.transaction():
        await set_scope(
            connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id
        )
        return int(await connection.fetchval(f"SELECT count(*) FROM {name}"))  # noqa: S608


async def _write(
    connection: Any, organization_id: uuid.UUID, concession_id: uuid.UUID, statement: Any
) -> str:
    """Una escritura bajo la concesión, revertida: ``ok`` o su SQLSTATE. El error sale del
    bloque de la transacción para que la revierta (también si es un punto de guardado)."""
    try:
        async with connection.transaction():
            await set_scope(
                connection, organization_id, actor_kind=PROVIDER, concession_id=concession_id
            )
            await connection.execute(statement[0], *statement[1])
            raise _Rollback
    except _Rollback:
        return "ok"
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)


async def _leaks(
    connection: Any,
    world: World,
    scoped: tuple[str, ...] = SCOPED_TABLES,
    organization_only: tuple[str, ...] = ORGANIZATION_ONLY_TABLES,
) -> list[str]:
    """Lo que ve o escribe cada concesión de más (o ve de menos, la vigente)."""
    leaks: list[str] = []
    concessions = world.concessions
    for name in scoped:
        seen = await _visible_plants(connection, world.a, concessions["active"], name)
        if seen != {world.conceded}:
            leaks.append(f"{name}: la vigente ve {seen}")
        seen = await _visible_plants(connection, world.a, concessions["other_plant"], name)
        if world.conceded in seen or world.other_plant not in seen:
            leaks.append(f"{name}: la de la otra planta ve {seen}")
        for concession in ("revoked", "expired", "unswept"):
            seen = await _visible_plants(connection, world.a, concessions[concession], name)
            if seen:
                leaks.append(f"{name}: la {concession} ve {seen}")
        seen = await _visible_plants(connection, world.b, concessions["active"], name)
        if seen:
            leaks.append(f"{name}: en el contexto de B ve {seen}")
        # Escribir en la planta 1 con la concesión de la planta 0: la política lo rechaza.
        outcome = await _write(connection, world.a, concessions["active"], world.row(name, 1))
        if outcome != INSUFFICIENT_PRIVILEGE:
            leaks.append(f"{name}: la vigente escribe en otra planta ({outcome})")
    for name in organization_only:
        for concession in (*CONCESSIONS, "organization"):
            count = await _visible_rows(connection, world.a, concessions[concession], name)
            if count:
                leaks.append(f"{name}: la {concession} ve {count} filas (A-69)")
            outcome = await _write(connection, world.a, concessions[concession], world.row(name, 0))
            if outcome != INSUFFICIENT_PRIVILEGE:
                leaks.append(f"{name}: la {concession} escribe ({outcome}) (A-69)")
    return leaks


@pytest.mark.asyncio
async def test_only_the_valid_concession_sees_its_plant_and_nothing_of_organization_tables(
    app: Any, world: World
) -> None:
    assert await _leaks(app, world) == []


@pytest.mark.asyncio
async def test_the_valid_concession_writes_in_its_own_plant(app: Any, world: World) -> None:
    """La política no lo bloquea todo: la vigente escribe en su planta (las tablas de una fila
    por planta chocan por clave con la sembrada, después de pasar la política). El aviso reclama
    además su clave de idempotencia bajo la misma concesión."""
    outcomes = {
        name: await _write(app, world.a, world.concessions["active"], world.row(name, 0))
        for name in WRITABLE_SCOPED_TABLES
    }
    assert outcomes == {
        name: UNIQUE_VIOLATION if name in SINGLE_ROW_TABLES else "ok"
        for name in WRITABLE_SCOPED_TABLES
    }


@pytest.mark.asyncio
async def test_without_a_concession_the_client_sees_its_organization_tables(
    app: Any, world: World
) -> None:
    """Las de solo organización no son tablas vacías: el cliente (sin concesión) ve sus filas."""
    for name in ORGANIZATION_ONLY_TABLES:
        async with app.transaction():
            await set_scope(app, world.a)
            assert await app.fetchval(f"SELECT count(*) FROM {name}") > 0, name  # noqa: S608


@pytest.mark.asyncio
async def test_an_organization_notification_is_only_for_the_organization_concession(
    app: Any, world: World
) -> None:
    """Un aviso sin planta (de la organización): lo ven el cliente y la concesión de toda la
    organización; ninguna concesión de planta, ni para leerlo ni para escribir otro."""
    plantless = "notifications.notification WHERE plant_id IS NULL"
    assert await _visible_rows(app, world.a, world.concessions["organization"], plantless) == 1
    for concession in CONCESSIONS:
        assert await _visible_rows(app, world.a, world.concessions[concession], plantless) == 0
    async with app.transaction():
        await set_scope(app, world.a)
        assert await app.fetchval(f"SELECT count(*) FROM {plantless}") == 1  # noqa: S608
    scope = world.notifications.plant(world.a, 0)
    statement = notification(scope, with_plant=False)
    assert await _write(app, world.a, world.concessions["active"], statement) == (
        INSUFFICIENT_PRIVILEGE
    )
    assert await _write(app, world.a, world.concessions["organization"], statement) == "ok"


# --- Maestro sin SUPERUSER (como vigia_owner en RDS) -----


@dataclass(frozen=True)
class _RdsDatabase:
    cluster: RdsLikeCluster
    database: str

    async def connect(self, role: str | None = None) -> Any:
        endpoint = (
            PostgresEndpoint(
                self.cluster.superuser.host,
                self.cluster.superuser.port,
                self.cluster.superuser.user,
                self.cluster.superuser.password,
                self.database,
            )
            if role is None
            else self.cluster.as_role(role, self.database)
        )
        return await asyncpg.connect(
            host=endpoint.host,
            port=endpoint.port,
            user=endpoint.user,
            password=endpoint.password,
            database=endpoint.database,
        )


def test_the_same_isolation_with_a_non_superuser_master(cluster: RdsLikeCluster) -> None:  # noqa: F811
    database = master_database(cluster)
    upgrade = upgrade_as_master(cluster, database)
    assert upgrade.returncode == 0, upgrade.stderr
    assert "Running upgrade gob_0028 -> laz_0029" in upgrade.stderr
    assert "Running upgrade laz_0029 -> laz_0030" in upgrade.stderr
    assert "Running upgrade laz_0030 -> laz_0031" in upgrade.stderr
    rds = _RdsDatabase(cluster, database)

    async def run() -> None:
        superuser = await rds.connect()
        try:
            assert (
                await superuser.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = $1", MASTER)
                is False
            )
            identity = await seed_identity(superuser)
        finally:
            await superuser.close()
        world = await _prepare(rds.connect, identity)
        for role in ("vigia_app", "vigia_migrate"):
            connection = await rds.connect(role)
            try:
                assert await _leaks(connection, world) == [], role
            finally:
                await connection.close()

    asyncio.run(run())


# --- Sondas negativas -----

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
    superuser: Any,
    world: World,
    change: list[str],
    scoped: tuple[str, ...] = SCOPED_TABLES,
    organization_only: tuple[str, ...] = (),
) -> list[str]:
    """Las fugas con ``change`` aplicado, como ``vigia_app``, en una transacción revertida."""
    leaks: list[str] = []
    try:
        async with superuser.transaction():
            for statement in change:
                await superuser.execute(statement)
            await superuser.execute("SET LOCAL ROLE vigia_app")
            leaks = await _leaks(superuser, world, scoped, organization_only)
            raise _Rollback
    except _Rollback:
        pass
    return leaks


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCOPED_TABLES)
async def test_a_permissive_provider_policy_is_detected(
    name: str, superuser: Any, world: World
) -> None:
    permissive = [
        f"DROP POLICY provider_concession_scope ON {name}",
        f"CREATE POLICY provider_concession_scope ON {name} AS PERMISSIVE FOR ALL TO PUBLIC"
        " USING (identity.rls_provider_scope_allows(organization_id, plant_id))"
        " WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))",
    ]
    leaks = await _probe_leaks(superuser, world, permissive, (name,))
    # PERMISSIVE se suma a ``organization_isolation`` (O lógico): la concesión ve las dos plantas.
    assert any(leak.startswith(f"{name}: la vigente ve") for leak in leaks), leaks


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
    for name in SCOPED_TABLES:
        for concession in ("revoked", "expired", "unswept"):
            assert any(leak.startswith(f"{name}: la {concession} ve") for leak in leaks), (
                name,
                concession,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ORGANIZATION_ONLY_TABLES)
async def test_a_permissive_organization_only_policy_is_detected(
    name: str, superuser: Any, world: World
) -> None:
    permissive = [
        f"DROP POLICY provider_context_denied ON {name}",
        f"CREATE POLICY provider_context_denied ON {name} AS PERMISSIVE FOR ALL TO PUBLIC"
        " USING (NOT identity.rls_provider_context())"
        " WITH CHECK (NOT identity.rls_provider_context())",
    ]
    leaks = await _probe_leaks(superuser, world, permissive, (), (name,))
    # PERMISSIVE se suma a ``organization_isolation``: la concesión de toda la organización ve las
    # filas de A (y las escribe, salvo en las tablas de clave, donde vigia_app no tiene INSERT).
    assert any(leak.startswith(f"{name}: la organization ve") for leak in leaks), leaks
    if name.split(".")[1] not in KEY_TABLES:
        assert any(leak.startswith(f"{name}: la organization escribe") for leak in leaks), leaks
