"""Base migrada y oráculo en Python del expediente y la auditoría (TASK-108, PR-NUC-47, 13, 18).

Lo comparten ``tests/properties/test_canonical_envelope_oracle.py``,
``tests/properties/test_append_only_ledger.py`` y
``tests/integration/test_chain_trigger_concurrency.py``:

- ``migrated_database``: base vacía nueva en el PostgreSQL 16 de la sesión con
  ``alembic upgrade head`` aplicado como proceso aparte (igual que ``make migrate``), borrada al
  salir.
- ``DatabaseLoop``: un bucle de eventos propio para usar ``asyncpg`` desde propiedades de
  Hypothesis, que son síncronas.
- El **oráculo**: ``record_envelope`` y ``audit_envelope`` construyen el sobre de BR-NUC-46 y
  BR-NUC-60 a partir de las columnas persistidas y lo serializan con ``vigia_contracts.canonical``
  (RFC 8785, VIG-14); ``genesis_hash`` y ``chain_hash`` repiten el cálculo del disparador. Es la
  referencia independiente de ``ledger.vigia_canonical_envelope`` y
  ``shared.vigia_canonical_audit_envelope``.

Solo datos generados. Contraseñas de rol generadas en cada corrida.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from vigia_contracts.canonical import canonicalize

from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import run_alembic
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)

PLANT_RECORD_TYPE = "zone_created"
ORGANIZATION_RECORD_TYPE = "organization_created"
KEYED_RECORD_TYPE = "finding_received"

APPEND_ONLY_TABLES = (
    "ledger.ledger_record",
    "ledger.record_source_key",
    "ledger.record_identity",
    "ledger.evidence",
    "ledger.evidence_identity",
    "ledger.label",
    "shared.audit_entry",
    "shared.audit_entry_identity",
    "shared.outbox_event",
    "shared.dead_letter",
)
"""Tablas ⛓ de TASK-108 (BR-NUC-43): el disparador rechaza ``UPDATE``, ``DELETE`` y ``TRUNCATE``."""

RESTRICT_VIOLATION = "23001"
UNIQUE_VIOLATION = "23505"
SERIALIZATION_FAILURE = "40001"
INSUFFICIENT_PRIVILEGE = "42501"


@dataclass(frozen=True)
class MigratedDatabase:
    """Base con la cadena de migraciones aplicada y las contraseñas de sus dos roles."""

    endpoint: PostgresEndpoint
    database: str
    app_password: str = field(repr=False)
    migrate_password: str = field(repr=False)

    async def connect(self, role: str | None = None) -> Any:
        """Conexión ``asyncpg`` como el superusuario del contenedor o como ``role``."""
        password = {
            None: self.endpoint.password,
            "vigia_app": self.app_password,
            "vigia_migrate": self.migrate_password,
        }[role]
        return await asyncpg.connect(
            host=self.endpoint.host,
            port=self.endpoint.port,
            user=role or self.endpoint.user,
            password=password,
            database=self.database,
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
    """Base vacía nueva con ``alembic upgrade head``; se borra al salir."""
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


class DatabaseLoop:
    """Bucle de eventos propio para llamar a ``asyncpg`` desde propiedades síncronas."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()

    def run[T](self, awaitable: Coroutine[Any, Any, T] | Awaitable[T]) -> T:
        return self._loop.run_until_complete(awaitable)

    def close(self) -> None:
        self._loop.close()


# --- Datos de prueba -----------------------------------------------------------------------------


async def register_record_types(connection: Any) -> None:
    """Tipos de prueba: uno de cadena de planta, uno de organización y uno con clave."""
    await connection.executemany(
        "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
        " content_schema, source_key_path) VALUES ($1, $2, $3, 1, '{}', $4)"
        " ON CONFLICT (record_type) DO NOTHING",
        [
            (PLANT_RECORD_TYPE, "U-02", "plant", None),
            (ORGANIZATION_RECORD_TYPE, "U-02", "organization", None),
            (KEYED_RECORD_TYPE, "U-03", "plant", "/finding_id"),
        ],
    )


async def set_organization(connection: Any, organization_id: uuid.UUID) -> None:
    """El ``SET LOCAL`` de ``shared.db``: solo dentro de la transacción abierta."""
    await connection.execute(
        "SELECT set_config('vigia.organization_id', $1, true)", str(organization_id)
    )


def record_values(
    organization_id: uuid.UUID,
    plant_id: uuid.UUID | None,
    *,
    record_type: str | None = None,
    content: Mapping[str, Any] | None = None,
    source_key: str | None = None,
    display_name: str = "Coordinación SST",
) -> dict[str, Any]:
    """Columnas que la aplicación aporta al insertar un registro (el resto, el disparador)."""
    zone_id = uuid.uuid4() if plant_id is not None else None
    return {
        "record_id": uuid.uuid4(),
        "organization_id": organization_id,
        "plant_id": plant_id,
        "record_type": record_type
        or (PLANT_RECORD_TYPE if plant_id is not None else ORGANIZATION_RECORD_TYPE),
        "schema_version": 1,
        "actor_kind": "user",
        "actor_id": uuid.uuid4(),
        "actor_display_name_snapshot": display_name,
        "actor_role_in_use": "coordinator_sst",
        "actor_concession_id": None,
        "actor_unit": "U-02",
        "scope_plant_id": plant_id,
        "scope_zone_id": zone_id,
        "scope_node_id": None,
        "correlation_id": uuid.uuid4(),
        "source_key": source_key,
        "content": canonicalize(dict(content or {"zone": "Z-01", "n": 1})),
        # Lo que la aplicación ponga aquí se sobrescribe (BR-NUC-46).
        "chain_sequence": 999,
        "content_hash": "0" * 64,
        "previous_hash": "0" * 64,
        "record_hash": "0" * 64,
    }


async def insert_record(connection: Any, values: Mapping[str, Any]) -> Any:
    """``INSERT`` en ``ledger.ledger_record`` y la fila persistida (``RETURNING *``)."""
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    statement = (
        f"INSERT INTO ledger.ledger_record ({', '.join(columns)})"  # noqa: S608 - columnas fijas
        f" VALUES ({placeholders}) RETURNING *"
    )
    return await connection.fetchrow(statement, *values.values())


def audit_values(
    organization_id: uuid.UUID, *, filters: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Columnas que la aplicación aporta al insertar una entrada de auditoría."""
    return {
        "entry_id": uuid.uuid4(),
        "organization_id": organization_id,
        "actor_kind": "user",
        "actor_id": uuid.uuid4(),
        "actor_display_name_snapshot": "Gerencia de planta",
        "actor_role_in_use": "plant_manager",
        "actor_concession_id": None,
        "actor_unit": "U-02",
        "operation": "ledger_read",
        "scope_plant_id": uuid.uuid4(),
        "scope_zone_id": None,
        "resource_kind": None,
        "resource_id": None,
        "filters": None if filters is None else canonicalize(dict(filters)),
        "result_count": 3,
        "outcome": "success",
        "correlation_id": uuid.uuid4(),
        "chain_sequence": 999,
        "previous_hash": "0" * 64,
        "entry_hash": "0" * 64,
    }


async def insert_audit(connection: Any, values: Mapping[str, Any]) -> Any:
    """``INSERT`` en ``shared.audit_entry`` y la fila persistida (``RETURNING *``)."""
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    statement = (
        f"INSERT INTO shared.audit_entry ({', '.join(columns)})"  # noqa: S608 - columnas fijas
        f" VALUES ({placeholders}) RETURNING *"
    )
    return await connection.fetchrow(statement, *values.values())


def evidence_values(
    organization_id: uuid.UUID, plant_id: uuid.UUID, record_id: uuid.UUID, verified_at: datetime
) -> dict[str, Any]:
    """Columnas de una fila de ``ledger.evidence`` (clip sintético ya verificado)."""
    return {
        "evidence_id": uuid.uuid4(),
        "organization_id": organization_id,
        "plant_id": plant_id,
        "zone_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "record_id": record_id,
        "clip_id": uuid.uuid4(),
        "camera_id": uuid.uuid4(),
        "storage_key": "org/x/clip.mp4",
        "sha256": "a" * 64,
        "size_bytes": 1024,
        "content_type": "video/mp4",
        "media_kind": "video",
        "duration_ms": 5000,
        "segment": "full",
        "verified_at": verified_at,
    }


async def insert_evidence(connection: Any, values: Mapping[str, Any]) -> Any:
    """``INSERT`` en ``ledger.evidence`` y la fila persistida (``RETURNING *``)."""
    columns = list(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    statement = (
        f"INSERT INTO ledger.evidence ({', '.join(columns)})"  # noqa: S608 - columnas fijas
        f" VALUES ({placeholders}) RETURNING *"
    )
    return await connection.fetchrow(statement, *values.values())


async def insert_batch(
    connection: Any,
    table: str,
    values: Mapping[str, Any],
    *,
    rows: int,
    overrides: Mapping[str, str] | None = None,
) -> str:
    """``INSERT ... SELECT ... FROM generate_series(1, rows) ON CONFLICT DO NOTHING``.

    Cada fila lleva ``values``, salvo las columnas de ``overrides``, que toman esa expresión SQL
    (por ejemplo ``gen_random_uuid()``). Devuelve la etiqueta de la sentencia (``INSERT 0 n``).
    """
    types = dict(
        await connection.fetch(
            "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid = $1::regclass AND attnum > 0 AND NOT attisdropped",
            table,
        )
    )
    overrides = dict(overrides or {})
    columns = list(values)
    parameters = [values[column] for column in columns if column not in overrides]
    expressions, index = [], 0
    for column in columns:
        if column in overrides:
            expressions.append(overrides[column])
        else:
            index += 1
            expressions.append(f"${index}::{types[column]}")
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)})"  # noqa: S608 - nombres fijos
        f" SELECT {', '.join(expressions)} FROM generate_series(1, {int(rows)})"
        " ON CONFLICT DO NOTHING"
    )
    status: str = await connection.execute(statement, *parameters)
    return status


# --- Oráculo en Python ----------------------------------------------------------------------------


def _uuid(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def canonical_timestamp(value: datetime) -> str:
    """Marca ISO 8601 en UTC con milisegundos (truncados) y sufijo ``Z``."""
    moment = value.astimezone(UTC)
    # Sin strftime("%Y"): en glibc no rellena con ceros los años de menos de cuatro cifras.
    return moment.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _actor(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "concession_id": _uuid(row["actor_concession_id"]),
        "display_name_snapshot": row["actor_display_name_snapshot"],
        "id": _uuid(row["actor_id"]),
        "kind": row["actor_kind"],
        "role_in_use": row["actor_role_in_use"],
        "unit": row["actor_unit"],
    }


def record_envelope(row: Mapping[str, Any]) -> bytes:
    """Sobre de BR-NUC-46 con forma fija, serializado con la canonicalización de U-01."""
    return canonicalize(
        {
            "record_id": _uuid(row["record_id"]),
            "organization_id": _uuid(row["organization_id"]),
            "plant_id": _uuid(row["plant_id"]),
            "chain_sequence": row["chain_sequence"],
            "record_type": row["record_type"],
            "schema_version": row["schema_version"],
            "actor": _actor(row),
            "scope": {
                "plant_id": _uuid(row["scope_plant_id"]),
                "zone_id": _uuid(row["scope_zone_id"]),
                "node_id": _uuid(row["scope_node_id"]),
            },
            "correlation_id": _uuid(row["correlation_id"]),
            "received_at": canonical_timestamp(row["received_at"]),
            "content_hash": row["content_hash"],
        }
    )


def audit_envelope(row: Mapping[str, Any]) -> bytes:
    """Sobre de una entrada de auditoría (BR-NUC-60) con forma fija."""
    resource = (
        None
        if row["resource_kind"] is None and row["resource_id"] is None
        else {"kind": row["resource_kind"], "id": _uuid(row["resource_id"])}
    )
    return canonicalize(
        {
            "entry_id": _uuid(row["entry_id"]),
            "organization_id": _uuid(row["organization_id"]),
            "chain_sequence": row["chain_sequence"],
            "actor": _actor(row),
            "operation": row["operation"],
            "scope": {
                "plant_id": _uuid(row["scope_plant_id"]),
                "zone_id": _uuid(row["scope_zone_id"]),
            },
            "resource_ref": resource,
            "filters_hash": row["filters_hash"],
            "result_count": row["result_count"],
            "outcome": row["outcome"],
            "correlation_id": _uuid(row["correlation_id"]),
            "occurred_at": canonical_timestamp(row["occurred_at"]),
        }
    )


def genesis_hash(organization_id: uuid.UUID, plant_id: uuid.UUID | None) -> str:
    """``SHA-256("vigia:genesis:" + organization_id + ":" + (plant_id | "organization"))``."""
    tail = "organization" if plant_id is None else str(plant_id)
    return hashlib.sha256(f"vigia:genesis:{organization_id}:{tail}".encode()).hexdigest()


def chain_hash(envelope: bytes, previous_hash: str) -> str:
    """``SHA-256(sobre ‖ previous_hash)``, con ``previous_hash`` en hexadecimal UTF-8."""
    return hashlib.sha256(envelope + previous_hash.encode("ascii")).hexdigest()
