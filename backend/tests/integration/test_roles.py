"""Roles, esquemas, extensiones y tablas globales de ``nuc_0001`` (TASK-106, LC-NUC-12, PR-NUC-18).

Cada módulo crea una base vacía propia en el PostgreSQL 16 de la sesión y ejecuta
``alembic upgrade head`` como proceso aparte, con las variables PG* y las contraseñas de rol en
el entorno, igual que ``make migrate`` y la tarea ``vigia-migrate``. Contraseñas generadas en cada
corrida. Criterios de TASK-106:

1. ``alembic upgrade head`` sobre PostgreSQL 16 vacío crea roles, esquemas, las dos extensiones y
   tablas globales (``test_upgrade_head_creates_roles_schemas_extensions_and_global_tables``);
3. conectado como ``vigia_app``, ``CREATE TABLE`` falla por permisos
   (``test_vigia_app_cannot_create_tables``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    APP_SECRET_VARIABLE,
    MASTER_SECRET_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
    MIGRATE_SECRET_VARIABLE,
)
from vigia_platform.shared.schema_version import SchemaTooOld, ensure_minimum_schema_version

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
ALEMBIC_TIMEOUT_SECONDS = 180
HEAD_SCHEMA_VERSION = len(list((BACKEND / "migrations" / "versions").glob("nuc_[0-9]*_*.py")))
"""Posición del último eslabón (lo que devuelve ``shared.vigia_schema_version()`` tras ``head``)."""
HEAD_REVISION = f"nuc_{HEAD_SCHEMA_VERSION:04d}"
INSUFFICIENT_PRIVILEGE = "42501"
CHECK_VIOLATION = "23514"

GLOBAL_TABLES = {
    ("identity", "data_region"),
    ("ledger", "record_type"),
    ("shared", "event_type"),
    ("shared", "consumer"),
    ("shared", "periodic_task"),
}


@dataclass(frozen=True)
class MigratedDatabase:
    endpoint: PostgresEndpoint
    database: str
    app_password: str = field(repr=False)
    migrate_password: str = field(repr=False)
    upgrade: subprocess.CompletedProcess[str]


def _database_name(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


async def _admin(endpoint: PostgresEndpoint, statement: str, database: str | None = None) -> None:
    connection = await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=database or endpoint.database,
    )
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


def run_alembic(
    endpoint: PostgresEndpoint, database: str, environment: Mapping[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """``python -m alembic <args>`` desde ``backend/`` con las variables PG* y ``environment``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "VIGIA_DB_"))}
    env |= {
        "PGHOST": endpoint.host,
        "PGPORT": str(endpoint.port),
        "PGUSER": endpoint.user,
        "PGPASSWORD": endpoint.password,
        "PGDATABASE": database,
        "PGSSLMODE": "disable",
    }
    env |= environment
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
        check=False,
    )


def _role_passwords() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24)


@pytest.fixture(scope="module")
def migrated(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    """Base vacía nueva con ``alembic upgrade head`` aplicado; se borra al terminar el módulo."""
    database = _database_name("vigia_roles")
    asyncio.run(_admin(postgres_endpoint, f"CREATE DATABASE {database}"))
    app_password, migrate_password = _role_passwords()
    try:
        upgrade = run_alembic(
            postgres_endpoint,
            database,
            {APP_PASSWORD_VARIABLE: app_password, MIGRATE_PASSWORD_VARIABLE: migrate_password},
            "upgrade",
            "head",
        )
        assert upgrade.returncode == 0, upgrade.stderr
        yield MigratedDatabase(postgres_endpoint, database, app_password, migrate_password, upgrade)
    finally:
        asyncio.run(_admin(postgres_endpoint, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))


async def _connect(migrated: MigratedDatabase, role: str | None = None) -> Any:
    endpoint = migrated.endpoint
    password = {
        None: endpoint.password,
        "vigia_app": migrated.app_password,
        "vigia_migrate": migrated.migrate_password,
    }[role]
    return await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=role or endpoint.user,
        password=password,
        database=migrated.database,
    )


@pytest_asyncio.fixture
async def superuser(migrated: MigratedDatabase) -> AsyncIterator[Any]:
    connection = await _connect(migrated)
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(migrated: MigratedDatabase) -> AsyncIterator[Any]:
    connection = await _connect(migrated, "vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


# --- Criterio 1: lo que crea ``alembic upgrade head`` -------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_head_creates_roles_schemas_extensions_and_global_tables(
    migrated: MigratedDatabase, superuser: Any
) -> None:
    assert "Running upgrade  -> nuc_0001" in migrated.upgrade.stderr
    for secret in (migrated.app_password, migrated.migrate_password):
        assert secret not in migrated.upgrade.stdout + migrated.upgrade.stderr

    roles = {
        row["rolname"]: dict(row)
        for row in await superuser.fetch(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,"
            " rolbypassrls, rolinherit FROM pg_roles"
            " WHERE rolname IN ('vigia_app', 'vigia_migrate')"
        )
    }
    denied = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
    for name in ("vigia_app", "vigia_migrate"):
        assert roles[name]["rolcanlogin"] is True, name
        assert not any(roles[name][attribute] for attribute in denied), roles[name]
    assert roles["vigia_app"]["rolinherit"] is False

    extensions = {row[0] for row in await superuser.fetch("SELECT extname FROM pg_extension")}
    assert extensions == {"plpgsql", "pgcrypto", "btree_gist"}
    digest = await superuser.fetchval("SELECT encode(public.digest('vigia', 'sha256'), 'hex')")
    assert digest == hashlib.sha256(b"vigia").hexdigest()
    assert (
        await superuser.fetchval("SELECT count(*) FROM pg_opclass WHERE opcname = 'gist_uuid_ops'")
        >= 1
    )

    schemas = dict(
        await superuser.fetch(
            "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace"
            " WHERE nspname IN ('identity', 'ledger', 'shared')"
        )
    )
    assert schemas == {
        "identity": "vigia_migrate",
        "ledger": "vigia_migrate",
        "shared": "vigia_migrate",
    }

    tables = {
        (row["schemaname"], row["tablename"]): (row["tableowner"], row["rowsecurity"])
        for row in await superuser.fetch(
            "SELECT schemaname, tablename, tableowner, rowsecurity FROM pg_tables"
            " WHERE schemaname IN ('identity', 'ledger', 'shared')"
            # Las particiones no: la política está en la tabla padre (TASK-108).
            " AND NOT (quote_ident(schemaname) || '.' || quote_ident(tablename))::regclass"
            " IN (SELECT inhrelid FROM pg_inherits)"
        )
    }
    assert set(tables) >= GLOBAL_TABLES
    assert {owner for owner, _ in tables.values()} == {"vigia_migrate"}
    # Tablas globales: sin datos de cliente ni seguridad a nivel de fila (domain-entities §6).
    assert {tables[table] for table in GLOBAL_TABLES} == {("vigia_migrate", False)}
    # Las de cliente de las migraciones siguientes (TASK-108 en adelante), todas con ella y
    # forzada: también el dueño (vigia_migrate, los disparadores SECURITY DEFINER) queda sujeto.
    client_tables = [table for table in tables if table not in GLOBAL_TABLES]
    assert all(tables[table][1] for table in client_tables)
    forced = {
        (row["nspname"], row["relname"]): row["relforcerowsecurity"]
        for row in await superuser.fetch(
            "SELECT n.nspname, c.relname, c.relforcerowsecurity FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname IN ('identity', 'ledger', 'shared') AND c.relkind IN ('r', 'p')"
        )
    }
    not_forced = [table for table in client_tables if not forced[table]]
    assert not_forced == [], not_forced
    # vigia_app no alcanza ninguna partición directamente: solo por la tabla padre.
    reachable_partitions = await superuser.fetch(
        "SELECT inhrelid::regclass::text FROM pg_inherits"
        " WHERE has_table_privilege('vigia_app', inhrelid, 'SELECT, INSERT, UPDATE, DELETE')"
    )
    assert reachable_partitions == []

    regions = [tuple(row) for row in await superuser.fetch("SELECT * FROM identity.data_region")]
    assert regions == [("us-east-1", "Este de Estados Unidos (Norte de Virginia)", "US")]
    periodic = {
        row[0]
        for row in await superuser.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'shared' AND table_name = 'periodic_task'"
        )
    }
    assert {"next_run_at", "lease_owner", "lease_until"} <= periodic

    version_num = await superuser.fetchval("SELECT version_num FROM public.alembic_version")
    assert version_num == HEAD_REVISION
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(relowner) FROM pg_class"
            " WHERE oid = 'public.alembic_version'::regclass"
        )
        == "vigia_migrate"
    )


@pytest.mark.asyncio
async def test_passwords_are_stored_as_scram_verifiers(
    migrated: MigratedDatabase, superuser: Any
) -> None:
    stored = dict(
        await superuser.fetch(
            "SELECT rolname, rolpassword FROM pg_authid"
            " WHERE rolname IN ('vigia_app', 'vigia_migrate')"
        )
    )
    for name, secret in (
        ("vigia_app", migrated.app_password),
        ("vigia_migrate", migrated.migrate_password),
    ):
        assert stored[name].startswith("SCRAM-SHA-256$4096:"), name
        assert secret not in stored[name]
    # Ni el verificador ni la contraseña quedan en variables de la sesión que migró.
    assert await superuser.fetchval("SELECT current_setting('vigia.role_verifier', true)") in (
        None,
        "",
    )


@pytest.mark.asyncio
async def test_roles_log_in_with_their_passwords_only(migrated: MigratedDatabase) -> None:
    for role in ("vigia_app", "vigia_migrate"):
        connection = await _connect(migrated, role)
        try:
            assert await connection.fetchval("SELECT current_user") == role
        finally:
            await connection.close()
    endpoint = migrated.endpoint
    with pytest.raises(asyncpg.exceptions.InvalidPasswordError):
        await asyncpg.connect(
            host=endpoint.host,
            port=endpoint.port,
            user="vigia_app",
            password=migrated.migrate_password,
            database=migrated.database,
        )


# --- Criterio 3: vigia_app no crea nada ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE identity.probe (x integer)",
        "CREATE TABLE ledger.probe (x integer)",
        "CREATE TABLE shared.probe (x integer)",
        "CREATE TABLE public.probe (x integer)",
        "CREATE TABLE probe (x integer)",
        "CREATE TEMPORARY TABLE probe (x integer)",
        "CREATE SCHEMA probe",
        "CREATE VIEW shared.probe AS SELECT 1",
        "CREATE FUNCTION public.probe() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "CREATE EXTENSION hstore",
    ],
)
async def test_vigia_app_cannot_create_tables(app: Any, statement: str) -> None:
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as caught:
        await app.execute(statement)
    assert caught.value.sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.asyncio
async def test_vigia_app_belongs_to_no_role_and_cannot_become_vigia_migrate(
    app: Any, superuser: Any
) -> None:
    memberships = await superuser.fetch(
        "SELECT pg_get_userbyid(roleid) FROM pg_auth_members WHERE member = 'vigia_app'::regrole"
    )
    assert memberships == []
    for statement in ("SET ROLE vigia_migrate", "SET SESSION AUTHORIZATION vigia_migrate"):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError) as caught:
            await app.execute(statement)
        assert caught.value.sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_table", sorted(GLOBAL_TABLES))
async def test_vigia_app_never_deletes_truncates_or_alters_global_tables(
    app: Any, schema_table: tuple[str, str]
) -> None:
    name = ".".join(schema_table)
    assert await app.fetchval(f"SELECT count(*) FROM {name}") >= 0  # noqa: S608 - nombre fijo
    for statement in (
        f"DELETE FROM {name}",  # noqa: S608 - nombre fijo de la lista
        f"TRUNCATE {name}",
        f"ALTER TABLE {name} ADD COLUMN probe integer",
        f"DROP TABLE {name}",
    ):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app.execute(statement)


@pytest.mark.asyncio
async def test_vigia_app_reads_but_never_writes_data_regions(app: Any) -> None:
    assert await app.fetchval("SELECT region_code FROM identity.data_region") == "us-east-1"
    for statement in (
        "INSERT INTO identity.data_region VALUES ('eu-west-1', 'Irlanda', 'IE')",
        "UPDATE identity.data_region SET description_es = 'x'",
    ):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app.execute(statement)


@pytest.mark.asyncio
async def test_vigia_app_registers_and_leases_periodic_tasks(app: Any) -> None:
    transaction = app.transaction()
    await transaction.start()
    try:
        await app.execute(
            "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
            " VALUES ('expire_sessions', 'U-02', 'every 5 minutes', now())"
        )
        leased = await app.fetchval(
            "UPDATE shared.periodic_task SET lease_owner = 'worker-1',"
            " lease_until = now() + interval '1 minute'"
            " WHERE task_name = 'expire_sessions' AND (lease_until IS NULL OR lease_until < now())"
            " RETURNING lease_owner"
        )
        assert leased == "worker-1"
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
            await app.execute("UPDATE shared.periodic_task SET lease_until = NULL")
        assert caught.value.sqlstate == CHECK_VIOLATION
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
        " VALUES ('Bad-Name', 'U-02', 'daily', now())",
        "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
        " VALUES ('ok_name', 'U-05', 'daily', now())",
        "INSERT INTO shared.event_type VALUES ('user_invited', 'U-02', '[]', 'Invitación')",
        "INSERT INTO shared.consumer (consumer_name, unit, circuit_state)"
        " VALUES ('outbox', 'U-02', 'broken')",
        "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
        " content_schema) VALUES ('x', 'U-02', 'zone', 1, '{}')",
        "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
        " content_schema) VALUES ('" + "x" * 65 + "', 'U-02', 'plant', 1, '{}')",
    ],
)
async def test_global_tables_reject_values_outside_their_closed_lists(
    app: Any, statement: str
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await app.execute(statement)


# --- vigia_migrate y la versión del esquema -----------------------------------------------------


@pytest.mark.asyncio
async def test_vigia_migrate_owns_the_schemas_and_can_extend_them(
    migrated: MigratedDatabase,
) -> None:
    connection = await _connect(migrated, "vigia_migrate")
    try:
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute("CREATE TABLE shared.probe (x integer)")
            await connection.execute("ALTER TABLE shared.consumer ADD COLUMN probe integer")
            await connection.execute("CREATE SCHEMA catalog")  # U-03 y U-04 crean los suyos
        finally:
            await transaction.rollback()
        # Dueño del esquema, pero sin escalar: ni roles nuevos ni atributos para vigia_app.
        for statement in (
            "ALTER ROLE vigia_app BYPASSRLS",
            "ALTER ROLE vigia_migrate SUPERUSER",
            "CREATE ROLE probe LOGIN",
            "GRANT pg_read_all_data TO vigia_app",
        ):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_schema_version_check_as_vigia_app(migrated: MigratedDatabase) -> None:
    endpoint = migrated.endpoint
    url = PostgresEndpoint(
        endpoint.host, endpoint.port, "vigia_app", migrated.app_password, migrated.database
    ).sqlalchemy_url
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            head = HEAD_SCHEMA_VERSION
            assert await ensure_minimum_schema_version(connection) == head
            assert await ensure_minimum_schema_version(connection, 1) == head
            with pytest.raises(SchemaTooOld) as caught:
                await ensure_minimum_schema_version(connection, head + 1)
            assert (caught.value.found, caught.value.minimum) == (head, head + 1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_schema_version_check_on_an_unmigrated_database(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    engine = create_async_engine(postgres_endpoint.sqlalchemy_url.rsplit("/", 1)[0] + "/postgres")
    try:
        async with engine.connect() as connection:
            with pytest.raises(SchemaTooOld) as caught:
                await ensure_minimum_schema_version(connection)
            assert caught.value.found is None
    finally:
        await engine.dispose()


def test_upgrade_again_is_a_no_op(migrated: MigratedDatabase) -> None:
    again = run_alembic(
        migrated.endpoint,
        migrated.database,
        {
            APP_PASSWORD_VARIABLE: migrated.app_password,
            MIGRATE_PASSWORD_VARIABLE: migrated.migrate_password,
        },
        "upgrade",
        "head",
    )
    assert again.returncode == 0, again.stderr
    assert "Running upgrade" not in again.stderr
    current = run_alembic(migrated.endpoint, migrated.database, {}, "current")
    assert current.returncode == 0, current.stderr
    assert f"{HEAD_REVISION} (head)" in current.stdout


def test_missing_role_password_creates_nothing(postgres_endpoint: PostgresEndpoint) -> None:
    """Fallo cerrado: sin la contraseña de ``vigia_app`` la migración no deja nada a medias."""
    database = _database_name("vigia_nopass")
    asyncio.run(_admin(postgres_endpoint, f"CREATE DATABASE {database}"))
    migrate_password = secrets.token_urlsafe(24)
    try:
        failed = run_alembic(
            postgres_endpoint,
            database,
            {MIGRATE_PASSWORD_VARIABLE: migrate_password},
            "upgrade",
            "head",
        )
        assert failed.returncode != 0
        assert "falta la contraseña de vigia_app" in failed.stderr
        assert migrate_password not in failed.stdout + failed.stderr

        async def leftovers() -> list[str]:
            connection = await asyncpg.connect(
                host=postgres_endpoint.host,
                port=postgres_endpoint.port,
                user=postgres_endpoint.user,
                password=postgres_endpoint.password,
                database=database,
            )
            try:
                rows = await connection.fetch(
                    "SELECT nspname FROM pg_namespace"
                    " WHERE nspname IN ('identity', 'ledger', 'shared')"
                    " UNION ALL SELECT relname FROM pg_class WHERE relname = 'alembic_version'"
                )
                return [row[0] for row in rows]
            finally:
                await connection.close()

        assert asyncio.run(leftovers()) == []
    finally:
        asyncio.run(_admin(postgres_endpoint, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))


def test_aws_mode_reads_the_secrets_the_migrate_task_receives(
    migrated: MigratedDatabase, localstack_endpoint: LocalStackEndpoint
) -> None:
    """Como la tarea ``vigia-migrate`` (``infra/stacks/compute.py``): solo nombres y ARN en el
    entorno; destino, usuario maestro y contraseñas de rol en Secrets Manager (LocalStack).

    Las variables PG* apuntan a propósito a otro usuario y a otra base: si la migración las usara
    en vez de los secretos, fallaría. Las contraseñas de rol son las del resto del módulo (los
    roles son del clúster y ``nuc_0001`` las vuelve a fijar).
    """
    endpoint = migrated.endpoint
    database = _database_name("vigia_aws")
    asyncio.run(_admin(endpoint, f"CREATE DATABASE {database}"))
    client = localstack_endpoint.aws_client("secretsmanager")
    prefix = f"vigia/test-{secrets.token_hex(4)}"
    target = {"engine": "postgres", "host": endpoint.host, "port": endpoint.port}
    documents = {
        "db/migrate": {
            **target,
            "dbname": database,
            "username": "vigia_migrate",
            "password": migrated.migrate_password,
        },
        "db/app": {
            **target,
            "dbname": database,
            "username": "vigia_app",
            "password": migrated.app_password,
        },
        "master": {"username": endpoint.user, "password": endpoint.password},
    }
    arns = {
        name: client.create_secret(Name=f"{prefix}/{name}", SecretString=json.dumps(document))[
            "ARN"
        ]
        for name, document in documents.items()
    }
    aws = {
        "AWS_ENDPOINT_URL": localstack_endpoint.url,
        "AWS_REGION": localstack_endpoint.region,
        "AWS_DEFAULT_REGION": localstack_endpoint.region,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "PGUSER": "nobody",
        "PGPASSWORD": "wrong",
        MIGRATE_SECRET_VARIABLE: f"{prefix}/db/migrate",
    }
    try:
        first = run_alembic(
            endpoint,
            "no_such_database",
            {
                **aws,
                APP_SECRET_VARIABLE: f"{prefix}/db/app",
                MASTER_SECRET_VARIABLE: arns["master"],
            },
            "upgrade",
            "head",
        )
        assert first.returncode == 0, first.stderr
        assert "Running upgrade  -> nuc_0001" in first.stderr
        # Despliegues siguientes: solo db/migrate, se entra como vigia_migrate.
        later = run_alembic(endpoint, "no_such_database", aws, "current")
        assert later.returncode == 0, later.stderr
        assert f"{HEAD_REVISION} (head)" in later.stdout
        for output in (first, later):
            for secret in (migrated.app_password, migrated.migrate_password, endpoint.password):
                assert secret not in output.stdout + output.stderr

        async def app_version() -> int:
            connection = await asyncpg.connect(
                host=endpoint.host,
                port=endpoint.port,
                user="vigia_app",
                password=migrated.app_password,
                database=database,
            )
            try:
                return int(await connection.fetchval("SELECT shared.vigia_schema_version()"))
            finally:
                await connection.close()

        assert asyncio.run(app_version()) == HEAD_SCHEMA_VERSION
    finally:
        for arn in arns.values():
            client.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
        asyncio.run(_admin(endpoint, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))


def test_offline_sql_mode_is_refused(migrated: MigratedDatabase) -> None:
    """``--sql`` volcaría los verificadores de contraseña: no se permite."""
    offline = run_alembic(migrated.endpoint, migrated.database, {}, "upgrade", "head", "--sql")
    assert offline.returncode != 0
    assert "Modo sin conexión (--sql) deshabilitado" in offline.stderr
