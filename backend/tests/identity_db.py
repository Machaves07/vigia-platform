"""Base migrada y datos generados del esquema ``identity`` para las pruebas (TASK-107).

- ``migrated_database``: base vacía nueva en el PostgreSQL de la sesión con
  ``alembic upgrade head`` aplicado como proceso aparte (igual que ``make migrate``), con
  contraseñas de rol generadas; se borra al salir.
- ``seed_identity``: la organización proveedora y dos clientes, A y B, con al menos una fila en
  cada una de las 18 tablas (dos plantas por cliente, cada una con zona, nodo, asignación y
  emisión de token), escritas como superusuario en una transacción.

Solo datos generados (NFR-CTR-43): nombres, correos y claves sintéticos.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import run_alembic
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)

TENANT_TABLES = (
    "organization",
    "user_account",
    "plant",
    "zone",
    "node_identity",
    "zone_node_assignment",
    "role_assignment",
    "password_credential",
    "totp_credential",
    "recovery_code",
    "session",
    "invitation",
    "auth_throttle",
    "provider_concession",
    "signing_key",
    "key_set_publication",
    "live_view_token_issuance",
    "privacy_notice_acceptance",
)
"""Las 18 tablas de ``identity`` con datos de cliente (domain-entities §2 y §6)."""

PRIMARY_KEYS = {
    "organization": "organization_id",
    "user_account": "user_id",
    "plant": "plant_id",
    "zone": "zone_id",
    "node_identity": "node_id",
    "zone_node_assignment": "assignment_id",
    "role_assignment": "assignment_id",
    "password_credential": "user_id",
    "totp_credential": "user_id",
    "recovery_code": "recovery_code_id",
    "session": "session_id_hash",
    "invitation": "invitation_id",
    "auth_throttle": "subject_key",
    "provider_concession": "concession_id",
    "signing_key": "key_id",
    "key_set_publication": "publication_id",
    "live_view_token_issuance": "jti",
    "privacy_notice_acceptance": "acceptance_id",
}
"""Columna que identifica la fila (en ``auth_throttle``, la parte que es única en las pruebas)."""

PROVIDER_SCOPED_TABLES = (
    "plant",
    "zone",
    "node_identity",
    "zone_node_assignment",
    "live_view_token_issuance",
)
"""Tablas con ``plant_id`` o ``zone_id``: llevan la política de proveedor (PR-NUC-52)."""

APPEND_ONLY_TABLES = (
    "zone_node_assignment",
    "role_assignment",
    "provider_concession",
    "key_set_publication",
    "live_view_token_issuance",
    "privacy_notice_acceptance",
)
"""Tablas ⛓ del esquema (``migrations/append_only.py``)."""

BASE_TIME = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC)
"""Instante fijo de las altas sintéticas: todo lo sembrado es anterior a ``now()``."""


@dataclass(frozen=True)
class MigratedDatabase:
    endpoint: PostgresEndpoint
    database: str
    app_password: str = field(repr=False)
    migrate_password: str = field(repr=False)

    def as_role(self, role: str | None = None) -> PostgresEndpoint:
        """Punto de conexión como ``vigia_app``, ``vigia_migrate`` o (``None``) superusuario."""
        password = {
            None: self.endpoint.password,
            "vigia_app": self.app_password,
            "vigia_migrate": self.migrate_password,
        }[role]
        return PostgresEndpoint(
            self.endpoint.host,
            self.endpoint.port,
            role or self.endpoint.user,
            password,
            self.database,
        )

    async def connect(self, role: str | None = None) -> Any:
        endpoint = self.as_role(role)
        return await asyncpg.connect(
            host=endpoint.host,
            port=endpoint.port,
            user=endpoint.user,
            password=endpoint.password,
            database=endpoint.database,
        )


async def _admin(endpoint: PostgresEndpoint, statement: str) -> None:
    connection = await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=endpoint.database,
    )
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


@contextlib.contextmanager
def migrated_database(endpoint: PostgresEndpoint, prefix: str) -> Iterator[MigratedDatabase]:
    """Base nueva con ``alembic upgrade head``; se borra al salir."""
    database = f"{prefix}_{secrets.token_hex(4)}"
    asyncio.run(_admin(endpoint, f"CREATE DATABASE {database}"))
    app_password, migrate_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    try:
        upgrade = run_alembic(
            endpoint,
            database,
            {APP_PASSWORD_VARIABLE: app_password, MIGRATE_PASSWORD_VARIABLE: migrate_password},
            "upgrade",
            "head",
        )
        assert upgrade.returncode == 0, upgrade.stderr
        yield MigratedDatabase(endpoint, database, app_password, migrate_password)
    finally:
        asyncio.run(_admin(endpoint, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))


async def set_scope(
    connection: Any,
    organization_id: uuid.UUID | str | None,
    *,
    actor_kind: str = "user",
    concession_id: uuid.UUID | str | None = None,
) -> None:
    """Las tres variables de ``ScopeContext`` con ``set_config(..., true)``: exige transacción."""
    await connection.execute(
        "SELECT set_config('vigia.organization_id', $1, true),"
        " set_config('vigia.actor_kind', $2, true),"
        " set_config('vigia.concession_id', $3, true)",
        "" if organization_id is None else str(organization_id),
        actor_kind,
        "" if concession_id is None else str(concession_id),
    )


def _email(label: str) -> str:
    return f"{label}-{secrets.token_hex(4)}@example.test"


def _code(label: str) -> str:
    return f"{label}-{secrets.token_hex(3).upper()}"


def _public_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _hash() -> str:
    return secrets.token_hex(32)


@dataclass(frozen=True)
class PlantRows:
    """Una planta con su zona, su nodo, la asignación vigente y una emisión de token."""

    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    assignment_id: uuid.UUID
    jti: uuid.UUID

    def scoped_ids(self) -> dict[str, set[uuid.UUID]]:
        return {
            "plant": {self.plant_id},
            "zone": {self.zone_id},
            "node_identity": {self.node_id},
            "zone_node_assignment": {self.assignment_id},
            "live_view_token_issuance": {self.jti},
        }


@dataclass(frozen=True)
class Tenant:
    organization_id: uuid.UUID
    user_id: uuid.UUID
    plants: tuple[PlantRows, ...]

    def scoped_ids(self, plant_id: uuid.UUID | None = None) -> dict[str, set[uuid.UUID]]:
        """Filas de las tablas con planta, de toda la organización o de una planta."""
        result: dict[str, set[uuid.UUID]] = {table: set() for table in PROVIDER_SCOPED_TABLES}
        for plant in self.plants:
            if plant_id is None or plant.plant_id == plant_id:
                for table, ids in plant.scoped_ids().items():
                    result[table] |= ids
        return result


@dataclass(frozen=True)
class IdentitySeed:
    provider_organization_id: uuid.UUID
    operator_id: uuid.UUID
    installer_id: uuid.UUID
    a: Tenant
    b: Tenant
    concessions: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    """Concesión vigente de toda la organización, sembrada por cliente."""


async def _insert_organization(
    connection: Any, organization_id: uuid.UUID, kind: str, created_by: uuid.UUID
) -> None:
    await connection.execute(
        "INSERT INTO identity.organization"
        " (organization_id, code, name, kind, created_at, created_by)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        organization_id,
        _code("ORG"),
        f"Organización sintética {kind}",
        kind,
        BASE_TIME,
        created_by,
    )


async def _insert_user(connection: Any, organization_id: uuid.UUID, label: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO identity.user_account"
        " (user_id, organization_id, email, display_name, status, created_at)"
        " VALUES ($1, $2, $3, $4, 'active', $5)",
        user_id,
        organization_id,
        _email(label),
        f"Usuario sintético {label}",
        BASE_TIME,
    )
    return user_id


async def _insert_role(
    connection: Any,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    assigned_by: uuid.UUID,
) -> None:
    await connection.execute(
        "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
        " scope_level, scope_id, assigned_at, assigned_by)"
        " VALUES ($1, $2, $3, $4, 'organization', $2, $5, $6)",
        uuid.uuid4(),
        organization_id,
        user_id,
        role,
        BASE_TIME,
        assigned_by,
    )


async def _insert_keys(connection: Any, organization_id: uuid.UUID, rotated_by: uuid.UUID) -> None:
    key_id = f"key-set-{secrets.token_hex(6)}"
    await connection.execute(
        "INSERT INTO identity.signing_key (key_id, organization_id, purpose, algorithm,"
        " public_key, private_key_ref, valid_from, status, created_at, rotated_by)"
        " VALUES ($1, $2, 'key_set', 'Ed25519', $3, $4, $5, 'active', $5, $6)",
        key_id,
        organization_id,
        _public_key(),
        f"vigia/test/signing/{key_id}",
        BASE_TIME,
        rotated_by,
    )
    await connection.execute(
        "INSERT INTO identity.key_set_publication"
        " (publication_id, organization_id, issued_at, keys, signed_by_key_id, envelope)"
        " VALUES ($1, $2, $3, '[]', $4, '{}')",
        uuid.uuid4(),
        organization_id,
        BASE_TIME,
        key_id,
    )


async def _insert_privacy_notice(
    connection: Any, organization_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    await connection.execute(
        "INSERT INTO identity.privacy_notice_acceptance"
        " (acceptance_id, organization_id, user_id, notice_version, accepted_at, correlation_id)"
        " VALUES ($1, $2, $3, '2026-09', $4, $5)",
        uuid.uuid4(),
        organization_id,
        user_id,
        BASE_TIME,
        uuid.uuid4(),
    )


async def insert_plant(
    connection: Any, organization_id: uuid.UUID, user_id: uuid.UUID, created_by: uuid.UUID
) -> PlantRows:
    """Planta con zona, nodo, asignación vigente y una emisión de token de vista en vivo."""
    rows = PlantRows(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    await connection.execute(
        "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
        " data_region, timezone, created_at, created_by)"
        " VALUES ($1, $2, $3, 'Planta sintética', 'CO', 'us-east-1', 'America/Bogota', $4, $5)",
        rows.plant_id,
        organization_id,
        _code("PL"),
        BASE_TIME,
        created_by,
    )
    await connection.execute(
        "INSERT INTO identity.zone"
        " (zone_id, organization_id, plant_id, code, name, created_at, created_by)"
        " VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
        rows.zone_id,
        organization_id,
        rows.plant_id,
        _code("ZN"),
        BASE_TIME,
        created_by,
    )
    await connection.execute(
        "INSERT INTO identity.node_identity"
        " (node_id, organization_id, plant_id, code, status, created_at)"
        " VALUES ($1, $2, $3, $4, 'enrolled', $5)",
        rows.node_id,
        organization_id,
        rows.plant_id,
        _code("ND"),
        BASE_TIME,
    )
    await connection.execute(
        "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
        " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        rows.assignment_id,
        organization_id,
        rows.plant_id,
        rows.zone_id,
        rows.node_id,
        BASE_TIME,
        user_id,
    )
    await connection.execute(
        "INSERT INTO identity.live_view_token_issuance (jti, organization_id, plant_id, zone_id,"
        " node_id, user_id, role_in_use, issued_at, expires_at, correlation_id)"
        " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', $7::timestamptz,"
        " $7::timestamptz + interval '600 seconds', $8)",
        rows.jti,
        organization_id,
        rows.plant_id,
        rows.zone_id,
        rows.node_id,
        user_id,
        BASE_TIME,
        uuid.uuid4(),
    )
    return rows


async def insert_concession(
    connection: Any,
    seed: IdentitySeed,
    organization_id: uuid.UUID,
    *,
    scope_level: str = "organization",
    scope_id: uuid.UUID | None = None,
    granted_offset: dt.timedelta = -dt.timedelta(hours=1),
    duration: dt.timedelta = dt.timedelta(days=7),
    status: str = "active",
) -> uuid.UUID:
    """Concesión del instalador del proveedor; ``granted_offset`` es relativo a ``now()``."""
    concession_id = uuid.uuid4()
    revoked = status == "revoked"
    await connection.execute(
        "INSERT INTO identity.provider_concession (concession_id, organization_id,"
        " provider_user_id, provider_organization_id, scope_level, scope_id, reason, granted_at,"
        " expires_at, status, revoked_at, revoked_by, revoked_by_side)"
        " SELECT $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::text, $6::uuid,"
        " 'Mantenimiento sintético del nodo', now() + $7::interval,"
        " now() + $7::interval + $8::interval, $9::text,"
        " CASE WHEN $10::boolean THEN now() + $7::interval END,"
        " CASE WHEN $10::boolean THEN $11::uuid END,"
        " CASE WHEN $10::boolean THEN 'client' END",
        concession_id,
        organization_id,
        seed.installer_id,
        seed.provider_organization_id,
        scope_level,
        organization_id if scope_id is None else scope_id,
        granted_offset,
        duration,
        status,
        revoked,
        seed.operator_id,
    )
    return concession_id


async def _insert_client(
    connection: Any, operator_id: uuid.UUID, provider_organization_id: uuid.UUID
) -> Tenant:
    organization_id = uuid.uuid4()
    await _insert_organization(connection, organization_id, "client", operator_id)
    user_id = await _insert_user(connection, organization_id, "admin")
    await _insert_role(connection, organization_id, user_id, "administrator", operator_id)
    plants = (
        await insert_plant(connection, organization_id, user_id, operator_id),
        await insert_plant(connection, organization_id, user_id, operator_id),
    )
    await connection.execute(
        "INSERT INTO identity.password_credential"
        " (user_id, organization_id, password_hash, algorithm_version, updated_at)"
        " VALUES ($1, $2, $3, 'argon2id-v1', $4)",
        user_id,
        organization_id,
        "$argon2id$v=19$m=65536,t=3,p=4$" + _hash(),
        BASE_TIME,
    )
    await connection.execute(
        "INSERT INTO identity.totp_credential"
        " (user_id, organization_id, secret_encrypted, data_key_wrapped, enrolled_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        user_id,
        organization_id,
        os.urandom(48),
        os.urandom(48),
        BASE_TIME,
    )
    await connection.execute(
        "INSERT INTO identity.recovery_code"
        " (recovery_code_id, user_id, organization_id, code_hash, generated_at)"
        " VALUES ($1, $2, $3, $4, $5)",
        uuid.uuid4(),
        user_id,
        organization_id,
        _hash(),
        BASE_TIME,
    )
    await connection.execute(
        "INSERT INTO identity.session (session_id_hash, user_id, organization_id, created_at,"
        " last_seen_at, idle_expires_at, absolute_expires_at, origin_hash)"
        " VALUES ($1, $2, $3, $4::timestamptz, $4::timestamptz,"
        " $4::timestamptz + interval '30 minutes', $4::timestamptz + interval '12 hours', $5)",
        _hash(),
        user_id,
        organization_id,
        BASE_TIME,
        _hash(),
    )
    await connection.execute(
        "INSERT INTO identity.invitation (invitation_id, organization_id, user_id, token_hash,"
        " issued_at, expires_at, invited_by)"
        " VALUES ($1, $2, $3, $4, $5::timestamptz, $5::timestamptz + interval '72 hours', $6)",
        uuid.uuid4(),
        organization_id,
        user_id,
        _hash(),
        BASE_TIME,
        operator_id,
    )
    await connection.execute(
        "INSERT INTO identity.auth_throttle (organization_id, subject_kind, subject_key,"
        " window_started_at, next_allowed_at) VALUES ($1, 'account', $2, $3, $3)",
        organization_id,
        str(user_id),
        BASE_TIME,
    )
    await _insert_keys(connection, organization_id, operator_id)
    await _insert_privacy_notice(connection, organization_id, user_id)
    return Tenant(organization_id, user_id, plants)


async def seed_identity(connection: Any) -> IdentitySeed:
    """Proveedora y dos clientes con filas en las 18 tablas; ``connection`` es superusuario."""
    provider_organization_id = uuid.uuid4()
    async with connection.transaction():
        # La proveedora y su primer operador van juntos: created_by es diferible.
        operator_id = uuid.uuid4()
        await _insert_organization(connection, provider_organization_id, "provider", operator_id)
        await connection.execute(
            "INSERT INTO identity.user_account"
            " (user_id, organization_id, email, display_name, status, created_at)"
            " VALUES ($1, $2, $3, 'Operador sintético', 'active', $4)",
            operator_id,
            provider_organization_id,
            _email("operator"),
            BASE_TIME,
        )
        installer_id = await _insert_user(connection, provider_organization_id, "installer")
        await _insert_role(
            connection, provider_organization_id, operator_id, "platform_operator", operator_id
        )
        await _insert_role(
            connection, provider_organization_id, installer_id, "provider_installer", operator_id
        )
        a = await _insert_client(connection, operator_id, provider_organization_id)
        b = await _insert_client(connection, operator_id, provider_organization_id)
        seed = IdentitySeed(provider_organization_id, operator_id, installer_id, a, b)
        for tenant in (a, b):
            seed.concessions[tenant.organization_id] = await insert_concession(
                connection, seed, tenant.organization_id
            )
    return seed


@contextlib.contextmanager
def seeded_identity(
    endpoint: PostgresEndpoint, prefix: str
) -> Iterator[tuple[MigratedDatabase, IdentitySeed]]:
    """Base migrada con ``seed_identity`` aplicado; se borra al salir."""
    with migrated_database(endpoint, prefix) as database:

        async def seed() -> IdentitySeed:
            connection = await database.connect()
            try:
                return await seed_identity(connection)
            finally:
                await connection.close()

        yield database, asyncio.run(seed())
