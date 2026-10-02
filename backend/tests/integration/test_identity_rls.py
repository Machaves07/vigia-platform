"""Esquema ``identity`` de ``nuc_0004`` contra PostgreSQL 16 real (TASK-107, PAT-NUC-SEG-01).

Una base migrada propia del módulo con la proveedora y dos clientes, A y B, sembrados con filas
en las 18 tablas (``tests/identity_db.py``). Las lecturas y escrituras van como ``vigia_app`` y,
donde importa, también como ``vigia_migrate`` (dueño de las tablas: ``FORCE`` también le aplica).

Criterios de TASK-107:

1. con ``vigia.organization_id`` de A ninguna consulta devuelve filas de B en ninguna tabla
   (``test_organization_a_never_sees_rows_of_b``, ``test_known_ids_of_b_are_not_found``,
   ``test_without_organization_variable_no_rows``);
4. dos asignaciones vigentes de nodo para la misma zona fallan en la base y dos sucesivas sin
   solape se aceptan (``test_one_current_node_per_zone``).

Además: la metapropiedad de catálogo (toda tabla de cliente con ``FORCE`` y política; toda tabla
con planta o zona, con la política de proveedor), privilegios de ``vigia_app``, correo único y
normalizado, una sola proveedora, ``data_region`` inmutable, estados y URL del nodo, y roles y
concesiones solo en el tipo de organización que corresponde.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.identity_db import (
    APPEND_ONLY_TABLES,
    BASE_TIME,
    PRIMARY_KEYS,
    PROVIDER_SCOPED_TABLES,
    TENANT_TABLES,
    IdentitySeed,
    MigratedDatabase,
    insert_plant,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

ROLES = ("vigia_app", "vigia_migrate")

PROVIDER_DENIED_TABLES = frozenset(
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
"""Secretos y credenciales: ninguna fila con un contexto de proveedor (nuc_0009, A-46)."""

CLOSING_COLUMNS = {
    "zone_node_assignment": {"unassigned_at"},
    "role_assignment": {"removed_at", "removed_by"},
    "provider_concession": {"status", "revoked_at", "revoked_by", "revoked_by_side"},
}


@dataclass(frozen=True)
class Identity:
    database: MigratedDatabase
    seed: IdentitySeed
    ids_of_b: dict[str, set[Any]]


@pytest.fixture(scope="module")
def identity(postgres_endpoint: PostgresEndpoint) -> Iterator[Identity]:
    with seeded_identity(postgres_endpoint, "vigia_identity") as (database, seed):

        async def prepare() -> Identity:
            connection = await database.connect()
            try:
                ids_of_b = {
                    table: {
                        row[0]
                        for row in await connection.fetch(
                            f"SELECT {PRIMARY_KEYS[table]} FROM identity.{table}"  # noqa: S608
                            " WHERE organization_id = $1",
                            seed.b.organization_id,
                        )
                    }
                    for table in TENANT_TABLES
                }
            finally:
                await connection.close()
            return Identity(database, seed, ids_of_b)

        yield asyncio.run(prepare())


@pytest_asyncio.fixture
async def app(identity: Identity) -> AsyncIterator[Any]:
    connection = await identity.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def superuser(identity: Identity) -> AsyncIterator[Any]:
    connection = await identity.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


async def _rows(
    connection: Any, table: str, organization_id: Any, **scope: Any
) -> list[asyncpg.Record]:
    async with connection.transaction():
        await set_scope(connection, organization_id, **scope)
        return list(
            await connection.fetch(
                f"SELECT organization_id, {PRIMARY_KEYS[table]} AS id"  # noqa: S608
                f" FROM identity.{table}"
            )
        )


# --- Metapropiedad de catálogo ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_tenant_table_forces_row_security_with_its_policies(superuser: Any) -> None:
    tables = {
        row["relname"]: row
        for row in await superuser.fetch(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
            " pg_get_userbyid(c.relowner) AS owner"
            " FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'identity' AND c.relkind = 'r'"
        )
    }
    assert set(tables) == {*TENANT_TABLES, "data_region"}
    for name in TENANT_TABLES:
        assert tables[name]["relrowsecurity"] and tables[name]["relforcerowsecurity"], name
        assert tables[name]["owner"] == "vigia_migrate", name
    assert not tables["data_region"]["relrowsecurity"]  # global, sin datos de cliente

    policies: dict[str, dict[str, str]] = {}
    for row in await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, roles::text[] AS roles, qual"
        " FROM pg_policies WHERE schemaname = 'identity'"
    ):
        if (row["tablename"], row["policyname"]) == ("user_account", "login_lookup"):
            # nuc_0007 (TASK-124): solo lectura, solo el dueño y solo con la variable que fija
            # identity.login_organization; vigia_app no la tiene.
            assert (row["cmd"], row["roles"], row["permissive"]) == (
                "SELECT",
                ["vigia_migrate"],
                "PERMISSIVE",
            ), dict(row)
            assert "vigia.login_lookup" in row["qual"], dict(row)
            continue
        if row["policyname"] == "concession_lookup":
            # nuc_0008 (TASK-125) y nuc_0009 (TASK-127, la planta): igual, solo dentro de las
            # funciones de búsqueda de las concesiones.
            assert row["tablename"] in {"provider_concession", "organization", "plant"}, dict(row)
            assert (row["cmd"], row["roles"], row["permissive"]) == (
                "SELECT",
                ["vigia_migrate"],
                "PERMISSIVE",
            ), dict(row)
            assert "vigia.concession_lookup" in row["qual"], dict(row)
            continue
        if (row["tablename"], row["policyname"]) == ("invitation", "invitation_lookup"):
            # nuc_0011 (TASK-126): igual, solo dentro de identity.invitation_organization.
            assert (row["cmd"], row["roles"], row["permissive"]) == (
                "SELECT",
                ["vigia_migrate"],
                "PERMISSIVE",
            ), dict(row)
            assert "vigia.invitation_lookup" in row["qual"], dict(row)
            continue
        if (row["tablename"], row["policyname"]) == (
            "live_view_token_issuance",
            "live_view_rate_lookup",
        ):
            # nuc_0012 (TASK-128): igual, solo dentro de identity.live_view_issuances_since.
            assert (row["cmd"], row["roles"], row["permissive"]) == (
                "SELECT",
                ["vigia_migrate"],
                "PERMISSIVE",
            ), dict(row)
            assert "vigia.concession_lookup" in row["qual"], dict(row)
            continue
        assert row["roles"] == ["public"], dict(row)
        policies.setdefault(row["tablename"], {})[row["policyname"]] = (
            f"{row['permissive']} {row['cmd']}"
        )
    with_plant_or_zone = {
        row[0]
        for row in await superuser.fetch(
            "SELECT DISTINCT table_name FROM information_schema.columns"
            " WHERE table_schema = 'identity' AND column_name IN ('plant_id', 'zone_id')"
        )
    }
    assert with_plant_or_zone == set(PROVIDER_SCOPED_TABLES)
    # nuc_0009 (adenda A-46): toda tabla de identity acota además un contexto de proveedor.
    read_only = {
        "provider_concession_read": "RESTRICTIVE SELECT",
        "provider_context_read_only": "RESTRICTIVE INSERT",
        "provider_context_no_update": "RESTRICTIVE UPDATE",
        "provider_context_no_delete": "RESTRICTIVE DELETE",
    }
    for name in TENANT_TABLES:
        expected = {"organization_isolation": "PERMISSIVE ALL"}
        if name in with_plant_or_zone:
            expected["provider_concession_scope"] = "RESTRICTIVE ALL"
        elif name in PROVIDER_DENIED_TABLES:
            expected["provider_context_denied"] = "RESTRICTIVE ALL"
        elif name == "provider_concession":
            expected |= {
                "provider_concession_own": "RESTRICTIVE SELECT",
                "provider_concession_grant": "RESTRICTIVE INSERT",
                "provider_concession_close": "RESTRICTIVE UPDATE",
            }
        else:
            expected |= read_only
        assert policies.get(name) == expected, name


@pytest.mark.asyncio
async def test_vigia_app_never_deletes_and_updates_append_only_tables_only_to_close(
    superuser: Any,
) -> None:
    for table in TENANT_TABLES:
        name = f"identity.{table}"
        for privilege in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert not await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)", name, privilege
            ), (table, privilege)
        assert await superuser.fetchval(
            "SELECT has_table_privilege('vigia_app', $1, 'SELECT, INSERT')", name
        )
    for table in APPEND_ONLY_TABLES:
        updatable = {
            row[0]
            for row in await superuser.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'identity' AND table_name = $1"
                " AND has_column_privilege('vigia_app', 'identity.' || table_name, column_name,"
                " 'UPDATE')",
                table,
            )
        }
        assert updatable == CLOSING_COLUMNS.get(table, set()), table


# --- Criterio 1: A nunca ve a B -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_organization_a_never_sees_rows_of_b(
    identity: Identity, table: str, role: str
) -> None:
    a, b = identity.seed.a.organization_id, identity.seed.b.organization_id
    connection = await identity.database.connect(role)
    try:
        for own, other in ((a, b), (b, a)):
            rows = await _rows(connection, table, own)
            assert rows, f"{table}: la organización debe ver sus propias filas"
            assert {row["organization_id"] for row in rows} == {own}
            other_ids = identity.ids_of_b[table] if other == b else set()
            assert not other_ids & {row["id"] for row in rows}
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_known_ids_of_b_are_not_found(identity: Identity, app: Any, table: str) -> None:
    """Conocer el identificador de una fila de B no basta: la fila no existe para A (BR-NUC-09)."""
    ids = sorted(identity.ids_of_b[table], key=str)
    assert ids
    async with app.transaction():
        await set_scope(app, identity.seed.a.organization_id)
        found = await app.fetch(
            f"SELECT 1 FROM identity.{table} WHERE {PRIMARY_KEYS[table]} = ANY($1)",  # noqa: S608
            ids,
        )
    assert found == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_without_organization_variable_no_rows(
    identity: Identity, table: str, role: str
) -> None:
    connection = await identity.database.connect(role)
    try:
        query = f"SELECT count(*) FROM identity.{table}"  # noqa: S608 - nombre fijo de la lista
        assert await connection.fetchval(query) == 0  # nunca fijada en la sesión
        assert await _rows(connection, table, None) == []  # vacía, como tras el SET LOCAL
        assert await connection.fetchval(query) == 0  # tras la transacción, vacía otra vez
        assert await _rows(connection, table, uuid.uuid4()) == []  # organización inexistente
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["not-a-uuid", " ", "00000000-0000-0000-0000-00000000000g"])
async def test_malformed_organization_variable_fails_closed(app: Any, value: str) -> None:
    with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
        async with app.transaction():
            await set_scope(app, value)
            await app.fetch("SELECT * FROM identity.plant")


@pytest.mark.asyncio
async def test_a_cannot_write_rows_of_b(identity: Identity, app: Any) -> None:
    seed = identity.seed
    a, b = seed.a.organization_id, seed.b.organization_id
    # Insertar con la organización de B, estando en A, viola la política (WITH CHECK).
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="row-level security"):
        async with app.transaction():
            await set_scope(app, a)
            await insert_plant(app, b, seed.b.user_id, seed.operator_id)
    # Actualizar filas de B desde A no alcanza ninguna; sin WHERE, solo cambia lo de A.
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, a)
        update_b = "UPDATE identity.plant SET name = 'x' WHERE organization_id = $1"
        assert await app.execute(update_b, b) == "UPDATE 0"
        assert await app.execute("UPDATE identity.user_account SET display_name = 'x'") == (
            "UPDATE 1"
        )
        assert await app.execute("UPDATE identity.zone SET name = 'Zona A'") == "UPDATE 2"
    finally:
        await transaction.rollback()
    # Sin variable ni siquiera se puede insertar.
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError, match="row-level security"):
        async with app.transaction():
            await set_scope(app, None)
            await insert_plant(app, a, seed.a.user_id, seed.operator_id)


# --- Criterio 4: a lo sumo un nodo vigente por zona (pendiente nº 36) ----------------------------


@pytest.mark.asyncio
async def test_one_current_node_per_zone(identity: Identity, app: Any) -> None:
    seed = identity.seed
    a = seed.a.organization_id
    plant = seed.a.plants[0].plant_id
    zone, node_1, node_2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    t1 = BASE_TIME + dt.timedelta(days=1)
    t2 = t1 + dt.timedelta(hours=5)

    async def assign(
        node: uuid.UUID, start: dt.datetime, end: dt.datetime | None = None
    ) -> uuid.UUID:
        assignment = uuid.uuid4()
        await app.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            assignment,
            a,
            plant,
            zone,
            node,
            start,
            end,
            seed.a.user_id,
        )
        return assignment

    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, a)
        await app.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
            " created_at, created_by) VALUES ($1, $2, $3, 'ZN-GIST', 'Zona de prueba', $4, $5)",
            zone,
            a,
            plant,
            BASE_TIME,
            seed.operator_id,
        )
        for node, code in ((node_1, "ND-GIST-1"), (node_2, "ND-GIST-2")):
            await app.execute(
                "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
                " created_at) VALUES ($1, $2, $3, $4, $5)",
                node,
                a,
                plant,
                code,
                BASE_TIME,
            )
        first = await assign(node_1, t1)

        # Dos vigentes para la misma zona: la base lo rechaza, aunque sea el mismo nodo.
        for node, start in ((node_2, t2), (node_1, t2), (node_2, t1 - dt.timedelta(days=1))):
            with pytest.raises(asyncpg.exceptions.ExclusionViolationError) as caught:
                async with app.transaction():
                    await assign(node, start)
            assert caught.value.constraint_name == "zone_node_assignment_one_node_per_zone"

        # Se cierra la primera en t2 y la siguiente empieza justo en t2: [t1, t2) y [t2, ∞).
        closed = await app.execute(
            "UPDATE identity.zone_node_assignment SET unassigned_at = $2 WHERE assignment_id = $1",
            first,
            t2,
        )
        assert closed == "UPDATE 1"
        await assign(node_2, t2)

        # Un intervalo cerrado que se solapa con la historia también falla.
        with pytest.raises(asyncpg.exceptions.ExclusionViolationError):
            async with app.transaction():
                await assign(node_1, t1 - dt.timedelta(hours=1), t1 + dt.timedelta(minutes=1))
        # Uno anterior, que termina justo cuando empieza la primera, se acepta.
        await assign(node_1, t1 - dt.timedelta(days=2), t1)
        # Un intervalo vacío no chocaría con nada: se rechaza.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with app.transaction():
                await assign(node_1, t1 - dt.timedelta(days=5), t1 - dt.timedelta(days=5))

        # Nodo de otra planta: la asignación solo es válida dentro de la misma planta.
        other_plant_node = seed.a.plants[1].node_id
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            async with app.transaction():
                await assign(
                    other_plant_node, t1 - dt.timedelta(days=10), t1 - dt.timedelta(days=9)
                )
    finally:
        await transaction.rollback()


# --- Restricciones de domain-entities §2 --------------------------------------------------------


@pytest.mark.asyncio
async def test_email_is_unique_across_organizations_and_normalized(
    identity: Identity, app: Any, superuser: Any
) -> None:
    seed = identity.seed
    email = await superuser.fetchval(
        "SELECT email FROM identity.user_account WHERE user_id = $1", seed.a.user_id
    )

    async def insert(address: str) -> None:
        transaction = app.transaction()
        await transaction.start()
        try:
            await set_scope(app, seed.b.organization_id)
            await app.execute(
                "INSERT INTO identity.user_account"
                " (user_id, organization_id, email, display_name, created_at)"
                " VALUES ($1, $2, $3, 'Otra persona sintética', $4)",
                uuid.uuid4(),
                seed.b.organization_id,
                address,
                BASE_TIME,
            )
        finally:
            await transaction.rollback()

    await insert("nuevo-correo@example.test")  # control: un correo libre y normalizado entra
    # Existe en A: B no lo ve, pero tampoco puede usarlo (BR-NUC-05, sin revelar dónde).
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as caught:
        await insert(email)
    assert str(seed.a.organization_id) not in str(caught.value)
    for address in (email.upper(), "Nuevo@example.test", "sin-arroba", "a b@example.test", ""):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await insert(address)


@pytest.mark.asyncio
async def test_exactly_one_provider_organization(identity: Identity, superuser: Any) -> None:
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as caught:
        async with superuser.transaction():
            await superuser.execute(
                "INSERT INTO identity.organization"
                " (organization_id, code, name, kind, created_at, created_by)"
                " VALUES ($1, 'ORG-SECOND-PROVIDER', 'Otra proveedora', 'provider', $2, $3)",
                uuid.uuid4(),
                BASE_TIME,
                identity.seed.operator_id,
            )
    assert caught.value.constraint_name == "organization_single_provider"


@pytest.mark.asyncio
async def test_data_region_is_immutable(identity: Identity, app: Any) -> None:
    seed = identity.seed
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app.transaction():
            await set_scope(app, seed.a.organization_id)
            await app.execute("UPDATE identity.plant SET data_region = 'us-east-1'")
    owner = await identity.database.connect("vigia_migrate")
    try:
        with pytest.raises(asyncpg.exceptions.RestrictViolationError, match="BR-NUC-07"):
            async with owner.transaction():
                await owner.execute(
                    "INSERT INTO identity.data_region VALUES ('eu-west-1', 'Irlanda', 'IE')"
                )
                await set_scope(owner, seed.a.organization_id)
                await owner.execute("UPDATE identity.plant SET data_region = 'eu-west-1'")
    finally:
        await owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "url", "accepted"),
    [
        ("re_enrollment_pending", None, True),
        ("declared", "https://nodo-01.local:8443/", True),
        ("enrolled", "https://192.168.1.20:8443/", True),
        ("revoked", "https://[fe80::1]:8443/", True),
        ("expired", None, False),
        ("superseded", None, False),
        ("declared", "http://nodo-01.local:8443/", False),
        ("declared", "https://nodo-01.local/", False),
        ("declared", "https://nodo-01.local:443/", False),
        ("declared", "https://nodo-01.local:8443/vista", False),
        ("declared", "https://nodo-01.local:8443/?a=1", False),
        ("declared", "https://user@nodo-01.local:8443/", False),
        ("declared", "https://nodo-01.local:8443/\n", False),
        ("declared", "https://" + "a" * 240 + ".b:8443/", False),
    ],
)
async def test_node_status_and_live_view_url(
    identity: Identity, app: Any, status: str, url: str | None, accepted: bool
) -> None:
    seed = identity.seed
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, seed.a.organization_id)
        insert = app.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
            " status, live_view_local_url, created_at) VALUES ($1, $2, $3, 'ND-URL', $4, $5, $6)",
            uuid.uuid4(),
            seed.a.organization_id,
            seed.a.plants[0].plant_id,
            status,
            url,
            BASE_TIME,
        )
        if accepted:
            assert await insert == "INSERT 0 1"
        else:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await insert
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant", "role", "accepted"),
    [
        ("a", "administrator", True),
        ("a", "copasst", True),
        ("a", "provider_installer", False),
        ("a", "platform_operator", False),
        ("provider", "platform_operator", True),
        ("provider", "provider_installer", True),
        ("provider", "coordinator_sst", False),
        ("provider", "administrator", False),
    ],
)
async def test_roles_only_in_their_kind_of_organization(
    identity: Identity, app: Any, tenant: str, role: str, accepted: bool
) -> None:
    seed = identity.seed
    organization_id, user_id = (
        (seed.a.organization_id, seed.a.user_id)
        if tenant == "a"
        else (seed.provider_organization_id, seed.installer_id)
    )
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, organization_id)
        insert = app.execute(
            "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
            " scope_level, scope_id, assigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, 'organization', $2, $5, $6)",
            uuid.uuid4(),
            organization_id,
            user_id,
            role,
            BASE_TIME,
            seed.operator_id,
        )
        if accepted:
            assert await insert == "INSERT 0 1"
        else:
            with pytest.raises(asyncpg.exceptions.CheckViolationError, match="no es asignable"):
                await insert
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_user_of_another_organization_cannot_get_a_role_here(
    identity: Identity, app: Any
) -> None:
    seed = identity.seed
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        async with app.transaction():
            await set_scope(app, seed.a.organization_id)
            await app.execute(
                "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id,"
                " role, scope_level, scope_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, 'administrator', 'organization', $2, $4, $5)",
                uuid.uuid4(),
                seed.a.organization_id,
                seed.b.user_id,
                BASE_TIME,
                seed.operator_id,
            )


@pytest.mark.asyncio
async def test_concession_only_on_a_client_organization(identity: Identity, app: Any) -> None:
    """Sobre la proveedora no hay concesión, ni siquiera a favor de un usuario de un cliente.

    Las organizaciones son distintas y el usuario es de la organización que figura, así que
    solo el disparador (tipo ``client``) puede rechazarla.
    """
    seed = identity.seed
    insert = (
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
        " granted_at, expires_at) VALUES ($1, $2, $3, $4, 'organization', $2,"
        " 'Concesión sintética de prueba', $5::timestamptz, $5::timestamptz + interval '1 day')"
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="organización cliente"):
        async with app.transaction():
            await set_scope(app, seed.provider_organization_id)
            await app.execute(
                insert,
                uuid.uuid4(),
                seed.provider_organization_id,
                seed.a.user_id,
                seed.a.organization_id,
                BASE_TIME,
            )
    # Control: la misma forma sobre un cliente entra, desde el contexto de esa concesión
    # (nuc_0009: una concesión solo nace en el contexto de concesión de su propia fila) y vigente.
    transaction = app.transaction()
    await transaction.start()
    try:
        concession_id = uuid.uuid4()
        await set_scope(
            app, seed.b.organization_id, actor_kind="provider_user", concession_id=concession_id
        )
        await app.execute(
            insert,
            concession_id,
            seed.b.organization_id,
            seed.installer_id,
            seed.provider_organization_id,
            await app.fetchval("SELECT now()"),
        )
    finally:
        await transaction.rollback()
