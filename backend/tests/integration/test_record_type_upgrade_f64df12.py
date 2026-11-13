"""Actualización real desde ``f64df12``: migrar y arrancar sobre su base (VIG-457, BR-NUC-52).

Repite en PostgreSQL 16 lo que falló en la plataforma de integración (A-107) al pasar de
``f64df12`` (contrato v1.1.0, base en ``laz_0041``) a ``main`` (v1.5.0):

1. base migrada **solo hasta** ``laz_0041``, la última migración de ``f64df12``;
2. ``ledger.record_type`` sembrada con las 58 filas que persistía ``f64df12``
   (``tests/fixtures/record_types/ledger_record_type_f64df12.json``: solo esquemas);
3. un registro **histórico** ``ingest_rejected`` en su versión 1, escrito por el escritor con el
   registro de tipos que tenía ``f64df12`` (las versiones de la tabla; ``synchronize`` las acepta);
4. ``alembic upgrade head`` (``gob_0042`` y las que sigan) y ``synchronize`` con el registro de
   producción (``record_type_registry(registered_units())``, el de ``shared.runtime.core``) como
   ``vigia_app``: **sin** ``RegistryStartupError``. Solo los dos tipos cambiados pasan a la
   versión 2; las demás filas no cambian;
5. el registro histórico se sigue presentando (``LectorExpediente`` y ``record_out`` de la ruta
   ``/records``) con ``schema_version`` 1 y su contenido, que valida con la versión 1 congelada;
6. una escritura nueva de ``ingest_rejected`` sale en la versión 2 (``record_kind`` de v1.5.0).

Solo datos generados: identificadores aleatorios y lugares sintéticos.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.identity_db import MigratedDatabase
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import run_alembic
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import app_database, probe_catalog
from tests.record_type_upgrade_support import UPGRADED_TYPES, f64df12_document, f64df12_rows
from tests.writer_support import (
    InstrumentedStorage,
    Place,
    PlaceSeeder,
    TransactionProbe,
    unit_context,
)
from vigia_platform.ledger.adapters.http.records import record_out
from vigia_platform.ledger.adapters.record_type_store import SqlRecordTypeStore
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt, RecordScope
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.registry import PersistedRecordType, RecordTypeRegistry
from vigia_platform.ledger.schema_rules import normalized_schema
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Database
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.runtime.units import (
    free_text_registry,
    record_type_registry,
    registered_units,
)

pytestmark = pytest.mark.integration

F64DF12_HEAD = "laz_0041"
"""La última migración de ``f64df12``: la base de la plataforma de integración antes de subir."""

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)

_SEED = (
    "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
    " content_schema, source_key_path, free_text_paths, evidence_paths, label_rule,"
    " outbox_events, chain_follows_scope)"
    " VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb, $10, $11)"
)
_TABLE = (
    "SELECT record_type, writer_unit, chain_level, schema_version, content_schema::text AS schema,"
    " source_key_path, free_text_paths, evidence_paths, label_rule::text AS label_rule,"
    " outbox_events, chain_follows_scope FROM ledger.record_type"
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


def _alembic(migrated: MigratedDatabase, revision: str) -> None:
    result = run_alembic(
        migrated.endpoint,
        migrated.database,
        {
            APP_PASSWORD_VARIABLE: migrated.app_password,
            MIGRATE_PASSWORD_VARIABLE: migrated.migrate_password,
        },
        "upgrade",
        revision,
    )
    assert result.returncode == 0, result.stderr


async def _seed_f64df12_table(migrated: MigratedDatabase) -> None:
    connection = await migrated.connect()
    try:
        await connection.executemany(
            _SEED,
            [
                (
                    row["record_type"],
                    row["writer_unit"],
                    row["chain_level"],
                    row["schema_version"],
                    json.dumps(row["content_schema"]),
                    row["source_key_path"],
                    row["free_text_paths"],
                    row["evidence_paths"],
                    None if row["label_rule"] is None else json.dumps(row["label_rule"]),
                    row["outbox_events"],
                    row["chain_follows_scope"],
                )
                for row in f64df12_document()["rows"]
            ],
        )
    finally:
        await connection.close()


async def _table(migrated: MigratedDatabase) -> dict[str, PersistedRecordType]:
    connection = await migrated.connect()
    try:
        rows = await connection.fetch(_TABLE)
    finally:
        await connection.close()
    return {
        row["record_type"]: PersistedRecordType(
            record_type=row["record_type"],
            writer_unit=row["writer_unit"],
            chain_level=row["chain_level"],
            schema_version=row["schema_version"],
            content_schema=json.loads(row["schema"]),
            source_key_path=row["source_key_path"],
            free_text_paths=tuple(row["free_text_paths"]),
            evidence_paths=tuple(row["evidence_paths"]),
            label_rule=None if row["label_rule"] is None else json.loads(row["label_rule"]),
            outbox_events=tuple(row["outbox_events"]),
            chain_follows_scope=row["chain_follows_scope"],
        )
        for row in rows
    }


async def _schema_version(migrated: MigratedDatabase, record_id: uuid.UUID) -> int:
    connection = await migrated.connect()
    try:
        version: int = await connection.fetchval(
            "SELECT schema_version FROM ledger.ledger_record WHERE record_id = $1", record_id
        )
    finally:
        await connection.close()
    return version


def f64df12_registry() -> RecordTypeRegistry:
    """El registro de ``f64df12`` desde el código actual: cada tipo hasta la versión de la tabla
    (la 1 de los dos tipos cambiados, la congelada)."""
    persisted = f64df12_rows()
    registry = RecordTypeRegistry()
    for compiled in record_type_registry(registered_units()).all_versions():
        if compiled.schema_version <= persisted[compiled.record_type].schema_version:
            registry.register(compiled.definition)
    return registry


async def _synchronize(database: Database, registry: RecordTypeRegistry) -> None:
    system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
    async with database.transaction(system) as transaction:
        await registry.synchronize(SqlRecordTypeStore(transaction))


def _writer(database: Database, registry: RecordTypeRegistry) -> EscritorExpediente:
    clock = SimulatedClock(NOW)
    return EscritorExpediente(
        database=database,
        registry=registry,
        free_text=free_text_registry(registered_units()),
        evidence=EvidenceVerifier(InstrumentedStorage(TransactionProbe()), clock),
        outbox=Outbox(probe_catalog(), clock),
        clock=clock,
    )


def _rejection(place: Place, record_kind: str) -> dict[str, Any]:
    return {
        "node_id": str(place.node_id),
        "zone_id": str(place.zone_id),
        "record_kind": record_kind,
        "code": "zone_gate_not_approved",
        "correlation_id": str(uuid.uuid4()),
        "received_at": "2026-11-12T09:30:00.000Z",
    }


async def _write_rejection(
    writer: EscritorExpediente, place: Place, content: dict[str, Any]
) -> uuid.UUID:
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    receipt = await writer.write(
        context,
        "ingest_rejected",
        content,
        scope=RecordScope(plant_id=place.plant_id),
        occurred_at=NOW,
    )
    assert isinstance(receipt, Receipt), receipt
    return receipt.record_id


@dataclass(frozen=True)
class Upgraded:
    migrated: MigratedDatabase
    place: Place
    historical_id: uuid.UUID
    historical: dict[str, Any]
    before_upgrade: dict[str, PersistedRecordType]
    after_upgrade: dict[str, PersistedRecordType]
    registry: RecordTypeRegistry


@pytest.fixture(scope="module")
def upgraded(postgres_endpoint: PostgresEndpoint) -> Iterator[Upgraded]:
    name = f"vigia_f64_upgrade_{secrets.token_hex(4)}"
    asyncio.run(_admin(postgres_endpoint, f"CREATE DATABASE {name}"))
    migrated = MigratedDatabase(
        postgres_endpoint, name, secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    )
    loop = DatabaseLoop()
    seeder: PlaceSeeder | None = None
    database: Database | None = None
    try:
        # (1) y (2): la base de f64df12 con su tabla de tipos.
        _alembic(migrated, F64DF12_HEAD)
        loop.run(_seed_f64df12_table(migrated))
        before = loop.run(_table(migrated))
        seeder = PlaceSeeder(migrated)
        place = Place.new()
        seeder.seed(place)
        # (3): el registro histórico, con el registro de tipos de f64df12.
        database = app_database(migrated)
        old = f64df12_registry()
        loop.run(_synchronize(database, old))
        historical = _rejection(place, "finding")
        historical_id = loop.run(_write_rejection(_writer(database, old), place, historical))
        loop.run(database.dispose())
        database = None
        # (4): migrar y arrancar con el código actual.
        _alembic(migrated, "head")
        database = app_database(migrated)
        registry = record_type_registry(registered_units())
        loop.run(_synchronize(database, registry))  # sin RegistryStartupError
        after = loop.run(_table(migrated))
        loop.run(database.dispose())
        database = None
        yield Upgraded(migrated, place, historical_id, historical, before, after, registry)
    finally:
        if database is not None:
            loop.run(database.dispose())
        if seeder is not None:
            seeder.close()
        loop.close()
        asyncio.run(_admin(postgres_endpoint, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def test_the_seeded_table_is_the_one_of_f64df12(upgraded: Upgraded) -> None:
    fixture = f64df12_rows()
    assert set(upgraded.before_upgrade) == set(fixture)
    for name, row in fixture.items():
        seeded = upgraded.before_upgrade[name]
        assert seeded.schema_version == row.schema_version, name
        assert normalized_schema(seeded.content_schema) == normalized_schema(row.content_schema)


def test_the_current_code_starts_on_it_and_only_raises_the_changed_types(
    upgraded: Upgraded,
) -> None:
    assert upgraded.registry.sealed
    before, after = upgraded.before_upgrade, upgraded.after_upgrade
    raised = {
        name: row.schema_version
        for name, row in after.items()
        if row.schema_version != before[name].schema_version
    }
    assert raised == UPGRADED_TYPES
    for name, row in after.items():
        current = upgraded.registry.get(name).to_persisted()
        assert row.schema_version == current.schema_version, name
        assert normalized_schema(row.content_schema) == normalized_schema(current.content_schema)
        if name not in UPGRADED_TYPES:
            assert row == before[name], name


def test_the_historical_v1_record_is_still_presented(upgraded: Upgraded) -> None:
    async def read() -> Any:
        database = app_database(upgraded.migrated)
        try:
            audit = AuditWriter(
                database=database, clock=SimulatedClock(NOW), provider_organization_id=uuid.uuid4()
            )
            reader = LectorExpediente(database=database, audit=audit, clock=SimulatedClock(NOW))
            context = unit_context(upgraded.place.organization_id, ActorUnit.U02)
            return await reader.get(context, upgraded.historical_id)
        finally:
            await database.dispose()

    loop = DatabaseLoop()
    try:
        view = loop.run(read())
    finally:
        loop.close()
    assert view is not None
    presented = record_out(view)
    assert presented.record_type == "ingest_rejected"
    assert presented.schema_version == 1
    assert presented.content == upgraded.historical
    compiled = upgraded.registry.get("ingest_rejected", schema_version=presented.schema_version)
    compiled.validate_json(json.dumps(presented.content))


def test_new_writes_use_version_2(upgraded: Upgraded) -> None:
    async def write() -> uuid.UUID:
        database = app_database(upgraded.migrated)
        try:
            writer = _writer(database, upgraded.registry)
            return await _write_rejection(
                writer, upgraded.place, _rejection(upgraded.place, "episode_photo")
            )
        finally:
            await database.dispose()

    loop = DatabaseLoop()
    try:
        record_id = loop.run(write())
        assert loop.run(_schema_version(upgraded.migrated, record_id)) == 2
        assert loop.run(_schema_version(upgraded.migrated, upgraded.historical_id)) == 1
    finally:
        loop.close()
