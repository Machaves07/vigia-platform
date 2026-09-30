"""PR-NUC-47: el sobre canónico en SQL es byte a byte el de RFC 8785 en Python (TASK-108).

Oráculo (PBT-05, PAT-NUC-REN-01, riesgo R11): para cualquier sobre generado,
``ledger.vigia_canonical_envelope`` y ``shared.vigia_canonical_audit_envelope`` en PostgreSQL 16
producen los mismos bytes que ``vigia_contracts.canonical`` (RFC 8785, VIG-14) sobre el mismo
sobre, y el ``record_hash`` (``entry_hash``) que calcula el disparador es el que se recalcula en
Python desde las columnas persistidas y el hash anterior.

Los generadores (``envelope_strategies``, compartidos con TASK-109) cubren lo que pide el patrón:
nombres con acentos, caracteres fuera del plano básico y de control, comillas y barras inversas;
marcas límite; ``plant_id`` nulo, y las claves opcionales de ``actor`` y ``scope`` ausentes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    DatabaseLoop,
    MigratedDatabase,
    audit_envelope,
    audit_values,
    chain_hash,
    genesis_hash,
    insert_audit,
    insert_record,
    migrated_database,
    record_envelope,
    record_values,
    register_record_types,
    set_organization,
)
from tests.properties.envelope_strategies import (
    audit_envelope_rows,
    contents,
    display_names,
    record_envelope_rows,
)

pytestmark = pytest.mark.integration

# --- Fixtures -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_oracle") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def loop() -> Iterator[DatabaseLoop]:
    runner = DatabaseLoop()
    yield runner
    runner.close()


@pytest.fixture(scope="module")
def superuser(database: MigratedDatabase, loop: DatabaseLoop) -> Iterator[Any]:
    connection = loop.run(database.connect())
    loop.run(register_record_types(connection))
    yield connection
    loop.run(connection.close())


@pytest.fixture(scope="module")
def app(database: MigratedDatabase, loop: DatabaseLoop, superuser: Any) -> Iterator[Any]:
    connection = loop.run(database.connect("vigia_app"))
    yield connection
    loop.run(connection.close())


def _as_json(row: dict[str, Any]) -> str:
    def encode(value: object) -> object:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return "\\x" + value.hex()  # entrada de bytea
        return value

    return json.dumps({key: encode(value) for key, value in row.items()}, ensure_ascii=False)


def _sql_record_envelope(loop: DatabaseLoop, connection: Any, row: dict[str, Any]) -> bytes:
    envelope: bytes = loop.run(
        connection.fetchval(
            "SELECT ledger.vigia_canonical_envelope("
            "jsonb_populate_record(NULL::ledger.ledger_record, $1::jsonb))",
            _as_json(row),
        )
    )
    return envelope


def _sql_audit_envelope(loop: DatabaseLoop, connection: Any, row: dict[str, Any]) -> bytes:
    envelope: bytes = loop.run(
        connection.fetchval(
            "SELECT shared.vigia_canonical_audit_envelope("
            "jsonb_populate_record(NULL::shared.audit_entry, $1::jsonb))",
            _as_json(row),
        )
    )
    return envelope


# --- Sobre en SQL frente a Python -----------------------------------------------------------------


@given(row=record_envelope_rows())
def test_sql_record_envelope_equals_rfc8785(
    loop: DatabaseLoop, app: Any, row: dict[str, Any]
) -> None:
    assert _sql_record_envelope(loop, app, row) == record_envelope(row)


@given(row=audit_envelope_rows())
def test_sql_audit_envelope_equals_rfc8785(
    loop: DatabaseLoop, app: Any, row: dict[str, Any]
) -> None:
    assert _sql_audit_envelope(loop, app, row) == audit_envelope(row)


def test_envelope_has_fixed_form_with_null_plant(loop: DatabaseLoop, app: Any) -> None:
    """Forma fija: todas las claves presentes, ``null`` en las ausentes, en orden RFC 8785."""
    row = {
        "record_id": uuid.UUID("01900000-0000-7000-8000-000000000001"),
        "organization_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "plant_id": None,
        "chain_sequence": 1,
        "record_type": "organization_created",
        "schema_version": 1,
        "actor_kind": "operator",
        "actor_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "actor_display_name_snapshot": 'Operación "Vigía"\n\U0001f600',
        "actor_role_in_use": None,
        "actor_concession_id": None,
        "actor_unit": "U-02",
        "scope_plant_id": None,
        "scope_zone_id": None,
        "scope_node_id": None,
        "correlation_id": uuid.UUID("01900000-0000-7000-8000-000000000002"),
        "received_at": datetime(2026, 9, 29, 15, 30, 0, 7000, tzinfo=UTC),
        "content_hash": "a" * 64,
    }
    expected = (
        '{"actor":{"concession_id":null,'
        '"display_name_snapshot":"Operación \\"Vigía\\"\\n\U0001f600",'
        '"id":"22222222-2222-4222-8222-222222222222","kind":"operator","role_in_use":null,'
        '"unit":"U-02"},"chain_sequence":1,"content_hash":"' + "a" * 64 + '",'
        '"correlation_id":"01900000-0000-7000-8000-000000000002",'
        '"organization_id":"11111111-1111-4111-8111-111111111111","plant_id":null,'
        '"received_at":"2026-09-29T15:30:00.007Z",'
        '"record_id":"01900000-0000-7000-8000-000000000001","record_type":"organization_created",'
        '"schema_version":1,"scope":{"node_id":null,"plant_id":null,"zone_id":null}}'
    ).encode()
    assert _sql_record_envelope(loop, app, row) == expected == record_envelope(row)


# --- record_hash del disparador frente al recalculado ---------------------------------------------


def _write_chain(
    loop: DatabaseLoop, app: Any, organization_id: uuid.UUID, values: list[dict[str, Any]]
) -> list[Any]:
    async def write() -> list[Any]:
        async with app.transaction():
            await set_organization(app, organization_id)
            return [await insert_record(app, value) for value in values]

    return loop.run(write())


@given(
    plant=st.booleans(),
    names=st.lists(display_names, min_size=1, max_size=3),
    documents=st.lists(contents, min_size=1, max_size=3),
)
def test_trigger_record_hash_equals_python(
    loop: DatabaseLoop, app: Any, plant: bool, names: list[str], documents: list[dict[str, Any]]
) -> None:
    organization_id = uuid.uuid4()
    plant_id = uuid.uuid4() if plant else None
    values = [
        record_values(
            organization_id,
            plant_id,
            display_name=name,
            content=documents[index % len(documents)],
        )
        for index, name in enumerate(names)
    ]
    rows = _write_chain(loop, app, organization_id, values)

    previous = genesis_hash(organization_id, plant_id)
    for sequence, (value, row) in enumerate(zip(values, rows, strict=True), start=1):
        persisted = dict(row)
        assert persisted["chain_sequence"] == sequence
        assert persisted["content"] == value["content"]
        assert persisted["content_hash"] == hashlib.sha256(value["content"]).hexdigest()
        assert persisted["previous_hash"] == previous
        envelope = record_envelope(persisted)
        assert _sql_record_envelope(loop, app, persisted) == envelope
        assert persisted["record_hash"] == chain_hash(envelope, previous)
        previous = persisted["record_hash"]


@given(
    names=st.lists(display_names, min_size=1, max_size=3),
    filters=st.one_of(st.none(), contents),
)
def test_trigger_entry_hash_equals_python(
    loop: DatabaseLoop, app: Any, names: list[str], filters: dict[str, Any] | None
) -> None:
    organization_id = uuid.uuid4()

    async def write() -> list[Any]:
        async with app.transaction():
            await set_organization(app, organization_id)
            rows = []
            for name in names:
                value = audit_values(organization_id, filters=filters)
                value["actor_display_name_snapshot"] = name
                rows.append(await insert_audit(app, value))
            return rows

    previous = genesis_hash(organization_id, None)
    for sequence, row in enumerate(loop.run(write()), start=1):
        persisted = dict(row)
        assert persisted["chain_sequence"] == sequence
        assert persisted["previous_hash"] == previous
        expected_filters = (
            None
            if persisted["filters"] is None
            else hashlib.sha256(persisted["filters"]).hexdigest()
        )
        assert persisted["filters_hash"] == expected_filters
        envelope = audit_envelope(persisted)
        assert _sql_audit_envelope(loop, app, persisted) == envelope
        assert persisted["entry_hash"] == chain_hash(envelope, previous)
        previous = persisted["entry_hash"]


def test_timestamps_are_stored_with_milliseconds(loop: DatabaseLoop, app: Any) -> None:
    """El disparador guarda la marca truncada a milisegundos: el sobre no pierde precisión."""
    organization_id = uuid.uuid4()
    (row,) = _write_chain(
        loop, app, organization_id, [record_values(organization_id, uuid.uuid4())]
    )
    received_at: datetime = row["received_at"]
    assert received_at.microsecond % 1000 == 0
    database_now: datetime = loop.run(app.fetchval("SELECT clock_timestamp()"))
    assert timedelta(0) <= database_now - received_at < timedelta(minutes=5)
