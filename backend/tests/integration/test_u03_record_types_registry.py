"""Los 26 tipos de U-03 contra ``ledger.record_type`` en PostgreSQL 16 (BR-NUC-44, 52; LC-NUC-08).

``RecordTypeRegistry.synchronize`` contrasta lo registrado con la tabla global y guarda lo nuevo.
Aquí corre sobre una base migrada con ``alembic upgrade head`` y como **``vigia_app``** (el rol de
la aplicación, sin superusuario):

- registrar los 26 tipos de U-03 y sincronizar deja exactamente esos 26 nombres con
  ``writer_unit = U-03``, cadena de planta y versión 1, con su esquema y sus rutas; junto a los de
  U-02, cada unidad conserva los suyos;
- volver a sincronizar desde otro proceso es idempotente: ninguna fila cambia (ni su ``xmin``);
- retirar un tipo, o cambiar su esquema sin subir ``schema_version``, impide arrancar
  (``RegistryStartupError``) y la tabla no cambia.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any

import pytest
from pydantic import Field, StrictStr, create_model

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import MigratedDatabase, migrated_database
from tests.properties.gob.u03_records import u03_registry
from vigia_platform.ledger.record_types import register_u02_record_types
from vigia_platform.ledger.registry import (
    PersistedRecordType,
    RecordTypeRegistry,
    RegistryStartupError,
)

pytestmark = pytest.mark.integration

_COLUMNS = (
    "record_type, writer_unit, chain_level, schema_version, content_schema::text AS content_schema,"
    " source_key_path, free_text_paths, evidence_paths, label_rule::text AS label_rule,"
    " outbox_events, chain_follows_scope"
)
_LOAD = f"SELECT {_COLUMNS} FROM ledger.record_type"  # noqa: S608 — columnas constantes.
_SNAPSHOT = f"SELECT xmin::text AS version, {_COLUMNS} FROM ledger.record_type"  # noqa: S608
"""Con ``xmin``: si una sincronización reescribiera una fila, cambiaría aunque no su contenido."""
_SAVE = """
INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,
    content_schema, source_key_path, free_text_paths, evidence_paths, label_rule, outbox_events,
    chain_follows_scope)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb, $10, $11)
ON CONFLICT (record_type) DO UPDATE SET
    writer_unit = EXCLUDED.writer_unit, chain_level = EXCLUDED.chain_level,
    schema_version = EXCLUDED.schema_version, content_schema = EXCLUDED.content_schema,
    source_key_path = EXCLUDED.source_key_path, free_text_paths = EXCLUDED.free_text_paths,
    evidence_paths = EXCLUDED.evidence_paths, label_rule = EXCLUDED.label_rule,
    outbox_events = EXCLUDED.outbox_events, chain_follows_scope = EXCLUDED.chain_follows_scope
WHERE ledger.record_type.schema_version < EXCLUDED.schema_version
"""


class PostgresRecordTypeStore:
    """``RecordTypeStore`` sobre ``ledger.record_type`` con una conexión de ``vigia_app``."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def load(self) -> Mapping[str, PersistedRecordType]:
        rows = await self._connection.fetch(_LOAD)
        return {
            row["record_type"]: PersistedRecordType(
                record_type=row["record_type"],
                writer_unit=row["writer_unit"],
                chain_level=row["chain_level"],
                schema_version=row["schema_version"],
                content_schema=json.loads(row["content_schema"]),
                source_key_path=row["source_key_path"],
                free_text_paths=tuple(row["free_text_paths"]),
                evidence_paths=tuple(row["evidence_paths"]),
                label_rule=None if row["label_rule"] is None else json.loads(row["label_rule"]),
                outbox_events=tuple(row["outbox_events"]),
                chain_follows_scope=row["chain_follows_scope"],
            )
            for row in rows
        }

    async def save(self, row: PersistedRecordType) -> None:
        status = await self._connection.execute(
            _SAVE,
            row.record_type,
            row.writer_unit,
            row.chain_level,
            row.schema_version,
            json.dumps(row.content_schema),
            row.source_key_path,
            list(row.free_text_paths),
            list(row.evidence_paths),
            None if row.label_rule is None else json.dumps(row.label_rule),
            list(row.outbox_events),
            row.chain_follows_scope,
        )
        if status != "INSERT 0 1":
            raise ValueError("solo se guarda una versión mayor que la persistida")


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_u03_types") as migrated:
        yield migrated


def _as_app(database: MigratedDatabase, action: Callable[[Any], Awaitable[Any]]) -> Any:
    async def run() -> Any:
        connection = await database.connect("vigia_app")
        try:
            return await action(connection)
        finally:
            await connection.close()

    return asyncio.run(run())


def _synchronize(database: MigratedDatabase, registry: RecordTypeRegistry) -> None:
    _as_app(database, lambda connection: registry.synchronize(PostgresRecordTypeStore(connection)))


def _snapshot(database: MigratedDatabase) -> dict[str, tuple[Any, ...]]:
    async def read(connection: Any) -> dict[str, tuple[Any, ...]]:
        rows = await connection.fetch(_SNAPSHOT)
        return {row["record_type"]: tuple(row.values()) for row in rows}

    result: dict[str, tuple[Any, ...]] = _as_app(database, read)
    return result


def _clear(database: MigratedDatabase) -> None:
    async def clear() -> None:
        connection = await database.connect()
        try:
            await connection.execute("DELETE FROM ledger.record_type")
        finally:
            await connection.close()

    asyncio.run(clear())


@pytest.fixture
def empty(database: MigratedDatabase) -> Iterator[MigratedDatabase]:
    _clear(database)
    yield database
    _clear(database)


def _with_u02(registry: RecordTypeRegistry) -> RecordTypeRegistry:
    register_u02_record_types(registry)
    return registry


def test_synchronizing_the_26_types_persists_exactly_them(empty: MigratedDatabase) -> None:
    registry = u03_registry()
    _synchronize(empty, registry)
    assert registry.sealed
    persisted = _as_app(empty, lambda c: PostgresRecordTypeStore(c).load())
    assert set(persisted) == set(registry.record_types())
    assert len(persisted) == 26
    for compiled in registry.latest():
        row = persisted[compiled.record_type]
        assert row.writer_unit == "U-03"
        assert row.chain_level == "plant"
        assert row.schema_version == 1
        assert row.chain_follows_scope is False
        assert row == compiled.to_persisted()


def test_each_unit_keeps_its_own_types_next_to_u02(empty: MigratedDatabase) -> None:
    registry = _with_u02(u03_registry())
    _synchronize(empty, registry)
    persisted = _as_app(empty, lambda c: PostgresRecordTypeStore(c).load())
    u03 = {name for name, row in persisted.items() if row.writer_unit == "U-03"}
    assert u03 == set(u03_registry().record_types())
    assert {row.writer_unit for row in persisted.values()} == {"U-02", "U-03"}
    assert {row.chain_level for name, row in persisted.items() if name in u03} == {"plant"}


def test_synchronizing_again_is_idempotent(empty: MigratedDatabase) -> None:
    _synchronize(empty, u03_registry())
    before = _snapshot(empty)
    _synchronize(empty, u03_registry())  # otro proceso, el mismo código
    _synchronize(empty, u03_registry())
    assert _snapshot(empty) == before


@pytest.mark.parametrize("retired", ["node_enrolled", "walk_test_result", "ingest_rejected"])
def test_retiring_a_type_prevents_startup_and_changes_nothing(
    empty: MigratedDatabase, retired: str
) -> None:
    _synchronize(empty, u03_registry())
    before = _snapshot(empty)
    registry = RecordTypeRegistry()
    for compiled in u03_registry().latest():
        if compiled.record_type != retired:
            registry.register(compiled.definition)
    with pytest.raises(RegistryStartupError) as failure:
        _synchronize(empty, registry)
    assert any(
        problem.startswith(f"{retired}:") and "BR-NUC-52" in problem
        for problem in failure.value.problems
    )
    assert not registry.sealed
    assert _snapshot(empty) == before


def test_changing_a_schema_without_a_new_version_prevents_startup(empty: MigratedDatabase) -> None:
    _synchronize(empty, u03_registry())
    before = _snapshot(empty)
    original = u03_registry().get("node_revoked").definition
    widened = create_model(
        "NodeRevokedWidened",
        __base__=original.content_model,
        replacement_node_code=(
            StrictStr | None,
            Field(default=None, max_length=32, pattern=r"^[A-Z0-9-]{2,32}$"),
        ),
    )
    registry = RecordTypeRegistry()
    for compiled in u03_registry().latest():
        definition = compiled.definition
        if compiled.record_type == "node_revoked":
            definition = replace(definition, content_model=widened)
        registry.register(definition)
    with pytest.raises(RegistryStartupError, match="cambió sin subir schema_version"):
        _synchronize(empty, registry)
    assert _snapshot(empty) == before
