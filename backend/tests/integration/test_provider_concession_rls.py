"""Aislamiento del proveedor en toda la base de ``identity`` (TASK-127; adenda A-46, ``nuc_0009``).

Contra PostgreSQL 16 real, como ``vigia_app`` y como ``vigia_migrate`` (dueño: ``FORCE`` también
le aplica), con un contexto de proveedor (``vigia.actor_kind = provider_user`` y
``vigia.concession_id``) sobre el cliente A:

- **Matriz de visibilidad** (``test_provider_sees_only_what_its_concession_reaches``): para cada
  una de las 18 tablas y cada concesión (vigente de organización, vigente de planta, vencida sin
  marcar, vencida marcada, revocada, la de otro cliente y ninguna), las filas visibles son
  exactamente las esperadas: los secretos y credenciales, nunca; la organización, con cualquier
  concesión vigente; usuarios y aceptaciones del aviso, solo con alcance de organización; las
  asignaciones de rol, las de su planta (o de sus zonas); la concesión, solo la propia vigente.
- **Escrituras**: desde un contexto de proveedor no se crean ni amplían concesiones, no se
  escriben usuarios, roles, credenciales ni ajustes de la organización, y solo se revoca la
  propia concesión como ``provider``; un contexto de cliente no crea concesiones.
- **Funciones de búsqueda**: ``concession_terms`` y ``provider_concession_of`` solo responden a
  la proveedora sin concesión; fijar ``vigia.concession_lookup`` no da nada a ``vigia_app``.
- **Reglas entre filas** (revisión de VIG-38): «proveedora» que es cliente, duración por encima
  del ``concession_max_days`` del cliente, cliente suspendido, planta ajena, vencer antes de
  ``expires_at`` y revocar lo ya vencido.
- **Maestro sin SUPERUSER** (como ``vigia_owner`` en RDS): la misma matriz en una base migrada
  por él.
- **Sondas negativas**: con la política de secretos como PERMISSIVE, o sin la condición de
  vigencia, la misma comprobación encuentra la fuga.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.identity_db import (
    PRIMARY_KEYS,
    PROVIDER_SCOPED_TABLES,
    TENANT_TABLES,
    IdentitySeed,
    MigratedDatabase,
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
from tests.integration.test_roles_rds_master import (
    _master_database as master_database,
)
from tests.integration.test_roles_rds_master import _upgrade as upgrade_as_master

pytestmark = pytest.mark.integration

ROLES = ("vigia_app", "vigia_migrate")
INSUFFICIENT_PRIVILEGE = "42501"

SECRET_TABLES = frozenset(
    {
        "password_credential",
        "totp_credential",
        "recovery_code",
        "session",
        "invitation",
        "auth_throttle",
        "signing_key",
        "key_set_publication",
    }
)
ORGANIZATION_ONLY_TABLES = frozenset({"user_account", "privacy_notice_acceptance"})

NONE = "none"
"""Actor del proveedor sin ``vigia.concession_id``."""


@dataclass
class Scenario:
    """El cliente A con concesiones en todos los estados y asignaciones de planta y zona."""

    connect: Any
    seed: IdentitySeed
    concessions: dict[str, uuid.UUID] = field(default_factory=dict)
    plant_roles: set[uuid.UUID] = field(default_factory=set)
    """Asignaciones de rol de A en la planta 0 (de planta y de su zona)."""

    @property
    def a(self) -> uuid.UUID:
        return self.seed.a.organization_id


async def _role(
    connection: Any, seed: IdentitySeed, level: str, scope_id: uuid.UUID, role: str
) -> uuid.UUID:
    assignment_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
        " scope_level, scope_id, assigned_at, assigned_by)"
        " VALUES ($1, $2, $3, $4, $5, $6, now() - interval '1 day', $7)",
        assignment_id,
        seed.a.organization_id,
        seed.a.user_id,
        role,
        level,
        scope_id,
        seed.operator_id,
    )
    return assignment_id


async def prepare(connection: Any, seed: IdentitySeed) -> dict[str, Any]:
    """Concesiones y asignaciones del escenario, como superusuario."""
    a = seed.a
    plant0, plant1 = a.plants
    concessions = {
        "organization": seed.concessions[a.organization_id],
        "plant": await insert_concession(
            connection, seed, a.organization_id, scope_level="plant", scope_id=plant0.plant_id
        ),
        "expired": await insert_concession(
            connection,
            seed,
            a.organization_id,
            granted_offset=-dt.timedelta(days=2),
            duration=dt.timedelta(days=1),
        ),
        "expired_marked": await insert_concession(
            connection,
            seed,
            a.organization_id,
            granted_offset=-dt.timedelta(days=2),
            duration=dt.timedelta(days=1),
            status="expired",
        ),
        "revoked": await insert_concession(connection, seed, a.organization_id, status="revoked"),
        "other_client": seed.concessions[seed.b.organization_id],
    }
    plant_roles = {
        await _role(connection, seed, "plant", plant0.plant_id, "plant_manager"),
        await _role(connection, seed, "zone", plant0.zone_id, "line_manager"),
    }
    await _role(connection, seed, "plant", plant1.plant_id, "plant_manager")
    await _role(connection, seed, "zone", plant1.zone_id, "line_manager")
    return {"concessions": concessions, "plant_roles": plant_roles}


async def _visible(
    connection: Any, table: str, organization_id: uuid.UUID, concession: uuid.UUID | None
) -> set[Any]:
    async with connection.transaction():
        await set_scope(
            connection, organization_id, actor_kind="provider_user", concession_id=concession
        )
        rows = await connection.fetch(
            f"SELECT {PRIMARY_KEYS[table]} AS id FROM identity.{table}"  # noqa: S608
        )
    return {row["id"] for row in rows}


async def _all_ids(connection: Any, table: str, organization_id: uuid.UUID) -> set[Any]:
    """Todas las filas de la organización (el superusuario no tiene RLS: se filtra aquí)."""
    async with connection.transaction():
        await set_scope(connection, organization_id)
        rows = await connection.fetch(
            f"SELECT {PRIMARY_KEYS[table]} AS id FROM identity.{table}"  # noqa: S608
            " WHERE organization_id = $1",
            organization_id,
        )
    return {row["id"] for row in rows}


def _expected(table: str, name: str, everything: set[Any], scenario: Scenario) -> set[Any]:
    """Lo que un contexto de proveedor con la concesión ``name`` puede ver de ``table``."""
    if name not in ("organization", "plant") or table in SECRET_TABLES:
        return set()
    a = scenario.seed.a
    if table == "provider_concession":
        return {scenario.concessions[name]}
    if table == "organization":
        return {a.organization_id}
    if name == "organization":
        return everything
    # Concesión de la planta 0.
    if table in ORGANIZATION_ONLY_TABLES:
        return set()
    if table == "role_assignment":
        return set(scenario.plant_roles)
    assert table in PROVIDER_SCOPED_TABLES, table
    return a.scoped_ids(a.plants[0].plant_id)[table]


async def leaks(connection: Any, superuser: Any, scenario: Scenario) -> list[str]:
    """Cada (tabla, concesión) cuya visibilidad no es exactamente la esperada."""
    found: list[str] = []
    for table in TENANT_TABLES:
        everything = await _all_ids(superuser, table, scenario.a)
        for name in (*scenario.concessions, NONE):
            concession = None if name == NONE else scenario.concessions[name]
            visible = await _visible(connection, table, scenario.a, concession)
            expected = _expected(table, name, everything, scenario)
            if visible != expected:
                found.append(f"{table}/{name}: {len(visible)} visibles, {len(expected)} esperadas")
    return found


# --- Escenario en la base de la sesión ----------------------------------------------------------


@pytest.fixture(scope="module")
def scenario(postgres_endpoint: PostgresEndpoint) -> Iterator[Scenario]:
    with seeded_identity(postgres_endpoint, "vigia_provider_rls") as (database, seed):

        async def build() -> Scenario:
            connection = await database.connect()
            try:
                prepared = await prepare(connection, seed)
            finally:
                await connection.close()
            return Scenario(database.connect, seed, **prepared)

        yield asyncio.run(build())


async def _connections(scenario: Scenario, role: str) -> tuple[Any, Any]:
    return await scenario.connect(role), await scenario.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_provider_sees_only_what_its_concession_reaches(
    scenario: Scenario, role: str
) -> None:
    connection, superuser = await _connections(scenario, role)
    try:
        assert await leaks(connection, superuser, scenario) == []
    finally:
        await connection.close()
        await superuser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_the_scenario_has_rows_to_leak(scenario: Scenario, role: str) -> None:
    """Control: sin contexto de proveedor, A ve sus filas en todas las tablas."""
    connection = await scenario.connect(role)
    try:
        for table in TENANT_TABLES:
            assert await _all_ids(connection, table, scenario.a), table
    finally:
        await connection.close()


async def _fails(connection: Any, sql: str, *arguments: Any) -> str | None:
    """Ejecuta ``sql`` en un punto de guardado y devuelve el SQLSTATE si falla."""
    try:
        async with connection.transaction():
            await connection.execute(sql, *arguments)
    except asyncpg.PostgresError as error:
        state: str = error.sqlstate
        return state
    return None


async def _scoped(connection: Any, organization_id: uuid.UUID, **scope: Any) -> Any:
    transaction = connection.transaction()
    await transaction.start()
    await set_scope(connection, organization_id, **scope)
    return transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("concession", ["organization", "plant"])
async def test_a_provider_context_writes_nothing_but_its_own_revocation(
    scenario: Scenario, role: str, concession: str
) -> None:
    seed = scenario.seed
    a = seed.a
    own = scenario.concessions[concession]
    connection = await scenario.connect(role)
    transaction = await _scoped(
        connection, a.organization_id, actor_kind="provider_user", concession_id=own
    )
    try:
        new_concession = (
            "INSERT INTO identity.provider_concession (concession_id, organization_id,"
            " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
            " granted_at, expires_at) VALUES ($1, $2, $3, $4, 'organization', $2,"
            " 'Ampliación sintética del alcance', now(), now() + interval '1 day')"
        )
        # Ni crear otra concesión (ampliar a toda la organización) ni repetir la propia.
        for concession_id in (uuid.uuid4(), own):
            assert await _fails(
                connection,
                new_concession,
                concession_id,
                a.organization_id,
                seed.installer_id,
                seed.provider_organization_id,
            ) in (INSUFFICIENT_PRIVILEGE, "23505")
        writes = [
            (
                "INSERT INTO identity.user_account (user_id, organization_id, email,"
                " display_name, created_at) VALUES ($1, $2, $3, 'Intruso sintético', now())",
                (uuid.uuid4(), a.organization_id, f"x-{secrets.token_hex(4)}@example.test"),
            ),
            (
                "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id,"
                " role, scope_level, scope_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, 'administrator', 'organization', $2, now(), $3)",
                (uuid.uuid4(), a.organization_id, a.user_id),
            ),
            (
                "INSERT INTO identity.password_credential (user_id, organization_id,"
                " password_hash, algorithm_version, updated_at)"
                " VALUES ($1, $2, 'x', 'argon2id-v1', now())",
                (uuid.uuid4(), a.organization_id),
            ),
        ]
        for sql, arguments in writes:
            assert await _fails(connection, sql, *arguments) == INSUFFICIENT_PRIVILEGE, sql
        # Ajustes de la organización (el tope de las concesiones) y credenciales: cero filas.
        for sql in (
            "UPDATE identity.organization SET concession_max_days = 90",
            "UPDATE identity.user_account SET display_name = 'Reescrito'",
            "UPDATE identity.session SET status = 'revoked', end_reason = 'logout',"
            " ended_at = now()",
            "UPDATE identity.password_credential SET password_hash = 'x'",
        ):
            assert await connection.execute(sql) == "UPDATE 0", sql
        # Revocar la propia como cliente: no; como proveedor: sí.
        revoke = (
            "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = now(),"
            " revoked_by = $2, revoked_by_side = $3 WHERE concession_id = $1"
        )
        assert (
            await _fails(connection, revoke, own, seed.installer_id, "client")
            == INSUFFICIENT_PRIVILEGE
        )
        # Sin WHERE no hace falta leer la fila: solo cuenta el WITH CHECK de la política de
        # UPDATE (la de SELECT ya no la respalda).
        assert (
            await _fails(
                connection,
                "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = now(),"
                " revoked_by = $1, revoked_by_side = 'client'",
                seed.installer_id,
            )
            == INSUFFICIENT_PRIVILEGE
        )
        assert await connection.execute(revoke, own, seed.installer_id, "provider") == "UPDATE 1"
        # Y después ya no ve nada más del cliente.
        assert not await connection.fetch("SELECT 1 FROM identity.user_account")
        assert not await connection.fetch("SELECT 1 FROM identity.organization")
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_only_the_grant_context_of_that_row_creates_a_concession(
    scenario: Scenario, role: str
) -> None:
    seed = scenario.seed
    a = seed.a
    insert = (
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
        " granted_at, expires_at) VALUES ($1, $2, $3, $4, 'organization', $2,"
        " 'Concesión sintética de prueba', now(), now() + $5::interval)"
    )
    arguments = (a.organization_id, seed.installer_id, seed.provider_organization_id)
    connection = await scenario.connect(role)
    try:
        # Un contexto de cliente o del sistema no crea concesiones.
        for actor_kind in ("user", "system"):
            transaction = await _scoped(connection, a.organization_id, actor_kind=actor_kind)
            try:
                assert (
                    await _fails(connection, insert, uuid.uuid4(), *arguments, dt.timedelta(days=1))
                    == INSUFFICIENT_PRIVILEGE
                )
            finally:
                await transaction.rollback()
        # El contexto de concesión de esa misma fila sí, vigente; ya vencida, no.
        concession_id = uuid.uuid4()
        transaction = await _scoped(
            connection, a.organization_id, actor_kind="provider_user", concession_id=concession_id
        )
        try:
            assert (
                await _fails(connection, insert, concession_id, *arguments, -dt.timedelta(hours=1))
                is not None
            )
            assert (
                await connection.execute(insert, concession_id, *arguments, dt.timedelta(days=1))
                == "INSERT 0 1"
            )
            visible = await connection.fetch(
                "SELECT concession_id FROM identity.provider_concession"
            )
            assert [row["concession_id"] for row in visible] == [concession_id]
        finally:
            await transaction.rollback()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_lookup_variable_gives_nothing_to_vigia_app(scenario: Scenario) -> None:
    seed = scenario.seed
    connection = await scenario.connect("vigia_app")
    transaction = await _scoped(
        connection,
        seed.a.organization_id,
        actor_kind="provider_user",
        concession_id=scenario.concessions["organization"],
    )
    try:
        await connection.execute("SELECT set_config('vigia.concession_lookup', 'on', true)")
        for table in SECRET_TABLES:
            rows = await connection.fetch(f"SELECT 1 FROM identity.{table}")  # noqa: S608
            assert not rows, table
        expired = scenario.concessions["expired"]
        await set_scope(
            connection, seed.a.organization_id, actor_kind="provider_user", concession_id=expired
        )
        await connection.execute("SELECT set_config('vigia.concession_lookup', 'on', true)")
        assert not await connection.fetch("SELECT 1 FROM identity.organization")
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_lookup_functions_answer_only_the_provider_without_concession(
    scenario: Scenario,
) -> None:
    seed = scenario.seed
    a = seed.a
    connection = await scenario.connect("vigia_app")
    try:
        cases = [
            ({"organization_id": seed.provider_organization_id}, True),
            (
                {
                    "organization_id": seed.provider_organization_id,
                    "actor_kind": "provider_user",
                    "concession_id": scenario.concessions["organization"],
                },
                False,
            ),
            ({"organization_id": a.organization_id}, False),
            ({"organization_id": seed.b.organization_id}, False),
        ]
        for scope, answers in cases:
            transaction = await _scoped(connection, **scope)
            try:
                terms = await connection.fetch(
                    "SELECT * FROM identity.concession_terms($1)", a.organization_id
                )
                found = await connection.fetch(
                    "SELECT * FROM identity.provider_concession_of($1)",
                    scenario.concessions["organization"],
                )
                assert bool(terms) is answers and bool(found) is answers, scope
                if answers:
                    assert dict(terms[0]) == {
                        "concession_max_days": 90,
                        "concession_default_days": 7,
                    }
                    assert "reason" not in dict(found[0])
                # Nunca términos de la proveedora ni de una organización inexistente.
                for other in (seed.provider_organization_id, uuid.uuid4()):
                    assert not await connection.fetch(
                        "SELECT * FROM identity.concession_terms($1)", other
                    )
            finally:
                await transaction.rollback()
    finally:
        await connection.close()


# --- Reglas entre filas (revisión de VIG-38) -----------------------------------------------------


@pytest.mark.asyncio
async def test_row_rules_of_a_concession(scenario: Scenario) -> None:
    seed = scenario.seed
    a, b = seed.a, seed.b
    superuser = await scenario.connect()
    insert = (
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
        " granted_at, expires_at) VALUES ($1, $2, $3, $4, $5, $6,"
        " 'Concesión sintética de prueba', now(), now() + $7::interval)"
    )

    async def constraint(*arguments: Any) -> str | None:
        try:
            async with superuser.transaction():
                await superuser.execute(insert, uuid.uuid4(), *arguments)
        except asyncpg.exceptions.CheckViolationError as error:
            name: str | None = error.constraint_name
            return name
        return None

    try:
        org, day = "organization", dt.timedelta(days=1)
        provider, installer = seed.provider_organization_id, seed.installer_id
        assert (
            await constraint(a.organization_id, installer, provider, org, a.organization_id, day)
            is None
        )
        # La «proveedora» es un cliente (el usuario es de B).
        assert (
            await constraint(
                a.organization_id, b.user_id, b.organization_id, org, a.organization_id, day
            )
            == "provider_concession_provider_kind"
        )
        # Planta de otro cliente.
        assert (
            await constraint(
                a.organization_id, installer, provider, "plant", b.plants[0].plant_id, day
            )
            == "provider_concession_scope_plant"
        )
        # Por encima del tope del cliente (90 en la siembra): 90 días entra, uno más no.
        await superuser.execute(
            "UPDATE identity.organization SET concession_default_days = 1, concession_max_days = 5"
            " WHERE organization_id = $1",
            a.organization_id,
        )
        try:
            assert (
                await constraint(
                    a.organization_id, installer, provider, org, a.organization_id, 5 * day
                )
                is None
            )
            assert (
                await constraint(
                    a.organization_id,
                    installer,
                    provider,
                    org,
                    a.organization_id,
                    5 * day + dt.timedelta(microseconds=1),
                )
                == "provider_concession_max_days"
            )
        finally:
            await superuser.execute(
                "UPDATE identity.organization SET concession_max_days = 90,"
                " concession_default_days = 7 WHERE organization_id = $1",
                a.organization_id,
            )
        # Cliente suspendido (en una transacción que se revierte).
        transaction = superuser.transaction()
        await transaction.start()
        try:
            await superuser.execute(
                "UPDATE identity.organization SET status = 'suspended' WHERE organization_id = $1",
                a.organization_id,
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError) as raised:
                await superuser.execute(
                    insert,
                    uuid.uuid4(),
                    a.organization_id,
                    installer,
                    provider,
                    org,
                    a.organization_id,
                    day,
                )
            assert raised.value.constraint_name == "provider_concession_client_active"
        finally:
            await transaction.rollback()
        # Cierres: no vence antes de expires_at; no se revoca lo vencido.
        in_force = scenario.concessions["organization"]
        expired = scenario.concessions["expired"]
        for sql, arguments, name in (
            (
                "UPDATE identity.provider_concession SET status = 'expired'"
                " WHERE concession_id = $1",
                (in_force,),
                "provider_concession_expired_after_expiry",
            ),
            (
                "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = now(),"
                " revoked_by = $2, revoked_by_side = 'client' WHERE concession_id = $1",
                (expired, a.user_id),
                "provider_concession_revoked_before_expiry",
            ),
        ):
            with pytest.raises(asyncpg.exceptions.CheckViolationError) as raised:
                async with superuser.transaction():
                    await superuser.execute(sql, *arguments)
            assert raised.value.constraint_name == name
        # Lo vencido sí vence (y se revierte para no tocar el escenario).
        transaction = superuser.transaction()
        await transaction.start()
        try:
            assert (
                await superuser.execute(
                    "UPDATE identity.provider_concession SET status = 'expired'"
                    " WHERE concession_id = $1",
                    expired,
                )
                == "UPDATE 1"
            )
        finally:
            await transaction.rollback()
    finally:
        await superuser.close()


# --- Maestro sin SUPERUSER (como vigia_owner en RDS) ---------------------------------------------


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
    rds = _RdsDatabase(cluster, database)

    async def run() -> None:
        superuser = await rds.connect()
        try:
            master_is_superuser = await superuser.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = $1", MASTER
            )
            assert master_is_superuser is False
            seed = await seed_identity(superuser)
            prepared = await prepare(superuser, seed)
            scenario = Scenario(rds.connect, seed, **prepared)
            for role in ROLES:
                connection = await rds.connect(role)
                try:
                    assert await leaks(connection, superuser, scenario) == [], role
                finally:
                    await connection.close()
        finally:
            await superuser.close()

    asyncio.run(run())


# --- Sondas negativas ----------------------------------------------------------------------------


@pytest.fixture
def probe(postgres_endpoint: PostgresEndpoint) -> Iterator[tuple[MigratedDatabase, Scenario]]:
    """Base propia para romper las políticas a propósito."""
    with seeded_identity(postgres_endpoint, "vigia_provider_rls_probe") as (database, seed):

        async def build() -> Scenario:
            connection = await database.connect()
            try:
                prepared = await prepare(connection, seed)
            finally:
                await connection.close()
            return Scenario(database.connect, seed, **prepared)

        yield database, asyncio.run(build())


BROKEN = {
    "secretos como PERMISSIVE": (
        "DROP POLICY provider_context_denied ON identity.password_credential;"
        " CREATE POLICY provider_context_denied ON identity.password_credential"
        " AS PERMISSIVE FOR ALL TO PUBLIC USING (NOT identity.rls_provider_context())"
    ),
    "lectura como PERMISSIVE": (
        "DROP POLICY provider_concession_read ON identity.user_account;"
        " CREATE POLICY provider_concession_read ON identity.user_account"
        " AS PERMISSIVE FOR SELECT TO PUBLIC USING (NOT identity.rls_provider_context()"
        " OR identity.rls_concession_reaches(organization_id, NULL, false))"
    ),
    "la propia concesión sin vigencia": (
        "DROP POLICY provider_concession_own ON identity.provider_concession;"
        " CREATE POLICY provider_concession_own ON identity.provider_concession"
        " AS RESTRICTIVE FOR SELECT TO PUBLIC USING (NOT identity.rls_provider_context()"
        " OR concession_id = NULLIF(current_setting('vigia.concession_id', true), '')::uuid)"
    ),
    # La vigencia se comprueba dos veces (la función y la política de la concesión, que la
    # subconsulta de la función también atraviesa): la sonda quita las dos.
    "sin la condición de vigencia": (
        "DROP POLICY provider_concession_own ON identity.provider_concession;"
        " CREATE POLICY provider_concession_own ON identity.provider_concession"
        " AS RESTRICTIVE FOR SELECT TO PUBLIC USING (NOT identity.rls_provider_context()"
        " OR concession_id = NULLIF(current_setting('vigia.concession_id', true), '')::uuid);"
        " CREATE OR REPLACE FUNCTION identity.rls_concession_reaches("
        " row_organization_id uuid, row_plant_id uuid, any_scope boolean) RETURNS boolean"
        " LANGUAGE sql STABLE AS $$ SELECT EXISTS (SELECT FROM identity.provider_concession AS c"
        " WHERE c.concession_id = NULLIF(current_setting('vigia.concession_id', true), '')::uuid"
        " AND c.organization_id = row_organization_id"
        " AND (any_scope OR c.scope_level = 'organization'"
        " OR (c.scope_level = 'plant' AND c.scope_id = row_plant_id))) $$"
    ),
    "la concesión ajena visible": (
        "DROP POLICY provider_concession_own ON identity.provider_concession;"
        " CREATE POLICY provider_concession_own ON identity.provider_concession"
        " AS RESTRICTIVE FOR SELECT TO PUBLIC USING (true)"
    ),
}


@pytest.mark.parametrize("breakage", sorted(BROKEN))
def test_negative_probes_find_the_leak(
    probe: tuple[MigratedDatabase, Scenario], breakage: str
) -> None:
    """Sonda negativa (A-46, punto 2): rota la política, ``leaks`` encuentra la fuga."""
    database, scenario = probe

    async def run() -> list[str]:
        superuser = await database.connect()
        connection = await database.connect("vigia_app")
        try:
            assert await leaks(connection, superuser, scenario) == []
            await superuser.execute(BROKEN[breakage])
            return await leaks(connection, superuser, scenario)
        finally:
            await connection.close()
            await superuser.close()

    assert asyncio.run(run()), breakage
