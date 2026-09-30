"""``nuc_0001`` con un usuario maestro sin SUPERUSER, como ``vigia_owner`` en RDS (TASK-106).

En RDS el maestro tiene ``CREATEROLE`` y ``CREATEDB`` y es dueño de la base, pero no
``SUPERUSER`` (``rds_superuser`` no lo es; ``infra/stacks/data.py``). Este módulo levanta un
PostgreSQL 16 propio, sin roles de Vigía, crea ese maestro y ejecuta ``alembic upgrade head``
como él (revisión de VIG-31, bloqueante 1). Cubre también la rama de un rol que ya existe
(menor 1): se ajusta sin SUPERUSER y falla cerrado si el rol tiene atributos que un maestro así
no puede quitar.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.integration.conftest import (
    POSTGRES_DATABASE,
    POSTGRES_IMAGE,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    PostgresEndpoint,
)
from tests.integration.test_roles import run_alembic
from tests.migrations_head import HEAD_VERSION
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)

pytestmark = pytest.mark.integration

MASTER = "vigia_owner"


@dataclass(frozen=True)
class RdsLikeCluster:
    superuser: PostgresEndpoint
    master_password: str = field(repr=False)
    app_password: str = field(repr=False)
    migrate_password: str = field(repr=False)

    def as_role(self, role: str, database: str) -> PostgresEndpoint:
        password = {
            MASTER: self.master_password,
            "vigia_app": self.app_password,
            "vigia_migrate": self.migrate_password,
        }[role]
        return PostgresEndpoint(self.superuser.host, self.superuser.port, role, password, database)

    @property
    def role_environment(self) -> dict[str, str]:
        return {
            APP_PASSWORD_VARIABLE: self.app_password,
            MIGRATE_PASSWORD_VARIABLE: self.migrate_password,
        }


async def _run(endpoint: PostgresEndpoint, *statements: str) -> list[Any]:
    connection = await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=endpoint.database,
    )
    try:
        return [await connection.fetch(statement) for statement in statements]
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def cluster() -> Iterator[RdsLikeCluster]:
    """PostgreSQL 16 propio con un maestro ``vigia_owner`` sin SUPERUSER."""
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
        pytest.fail(f"Se necesita Docker para esta prueba: {type(error).__name__}: {error}")
    try:
        superuser = PostgresEndpoint(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(POSTGRES_PORT)),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE,
        )
        master_password = secrets.token_hex(16)  # hexadecimal: seguro dentro de un literal
        asyncio.run(
            _run(
                superuser,
                f"CREATE ROLE {MASTER} LOGIN CREATEROLE CREATEDB PASSWORD '{master_password}'",
            )
        )
        yield RdsLikeCluster(
            superuser, master_password, secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        )
    finally:
        container.stop()


def _master_database(cluster: RdsLikeCluster) -> str:
    """Base nueva cuyo dueño es el maestro, como la base ``vigia`` que crea RDS."""
    database = f"vigia_rds_{secrets.token_hex(4)}"
    asyncio.run(_run(cluster.superuser, f"CREATE DATABASE {database} OWNER {MASTER}"))
    return database


def _upgrade(cluster: RdsLikeCluster, database: str) -> Any:
    return run_alembic(
        cluster.as_role(MASTER, database), database, cluster.role_environment, "upgrade", "head"
    )


def test_first_deploy_with_a_non_superuser_master(cluster: RdsLikeCluster) -> None:
    master = asyncio.run(
        _run(cluster.superuser, "SELECT rolsuper FROM pg_roles WHERE rolname = 'vigia_owner'")
    )
    assert master[0][0]["rolsuper"] is False
    database = _master_database(cluster)

    upgrade = _upgrade(cluster, database)
    assert upgrade.returncode == 0, upgrade.stderr
    assert "Running upgrade  -> nuc_0001" in upgrade.stderr

    owners, public_create, version = asyncio.run(
        _run(
            cluster.as_role(MASTER, database),
            "SELECT DISTINCT tableowner FROM pg_tables"
            " WHERE schemaname IN ('identity', 'ledger', 'shared')"
            " UNION SELECT pg_get_userbyid(relowner) FROM pg_class"
            " WHERE oid = 'public.alembic_version'::regclass",
            "SELECT has_schema_privilege('vigia_migrate', 'public', 'CREATE')",
            "SELECT shared.vigia_schema_version()",
        )
    )
    assert [row[0] for row in owners] == ["vigia_migrate"]
    assert public_create[0][0] is False  # el CREATE temporal en public se retiró
    assert version[0][0] == HEAD_VERSION

    async def as_app() -> None:
        endpoint = cluster.as_role("vigia_app", database)
        connection = await asyncpg.connect(
            host=endpoint.host,
            port=endpoint.port,
            user=endpoint.user,
            password=endpoint.password,
            database=database,
        )
        try:
            assert await connection.fetchval("SELECT shared.vigia_schema_version()") == (
                HEAD_VERSION
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await connection.execute("CREATE TABLE shared.probe (x integer)")
        finally:
            await connection.close()

    asyncio.run(as_app())

    # Despliegues siguientes: entra vigia_migrate, que ya es dueño de alembic_version.
    later = run_alembic(cluster.as_role("vigia_migrate", database), database, {}, "upgrade", "head")
    assert later.returncode == 0, later.stderr


def test_existing_roles_are_adjusted_without_superuser(cluster: RdsLikeCluster) -> None:
    """Segunda base del mismo clúster: los roles ya existen y el maestro los ajusta."""
    _upgrade(cluster, _master_database(cluster))  # garantiza que los roles existen
    upgrade = _upgrade(cluster, _master_database(cluster))
    assert upgrade.returncode == 0, upgrade.stderr
    assert "Running upgrade  -> nuc_0001" in upgrade.stderr


@pytest.mark.parametrize(
    ("taint", "cleanup", "role"),
    [
        ("ALTER ROLE vigia_app BYPASSRLS", "ALTER ROLE vigia_app NOBYPASSRLS", "vigia_app"),
        ("ALTER ROLE vigia_app SUPERUSER", "ALTER ROLE vigia_app NOSUPERUSER", "vigia_app"),
        (
            "ALTER ROLE vigia_migrate REPLICATION",
            "ALTER ROLE vigia_migrate NOREPLICATION",
            "vigia_migrate",
        ),
        ("GRANT vigia_migrate TO vigia_app", "REVOKE vigia_migrate FROM vigia_app", "vigia_app"),
    ],
)
def test_existing_role_with_forbidden_attributes_fails_closed(
    cluster: RdsLikeCluster, taint: str, cleanup: str, role: str
) -> None:
    _upgrade(cluster, _master_database(cluster))  # garantiza que los roles existen
    asyncio.run(_run(cluster.superuser, taint))
    database = _master_database(cluster)
    try:
        failed = _upgrade(cluster, database)
    finally:
        asyncio.run(_run(cluster.superuser, cleanup))
    assert failed.returncode != 0
    assert f"{role} ya existe con SUPERUSER" in failed.stderr
    leftovers = asyncio.run(
        _run(
            cluster.as_role(MASTER, database),
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('identity', 'ledger', 'shared')",
        )
    )
    assert leftovers == [[]]
