"""PR-NUC-47: el sobre canónico en SQL es byte a byte el de RFC 8785 en Python (TASK-108).

Oráculo (PBT-05, PAT-NUC-REN-01, riesgo R11): para cualquier sobre generado,
``ledger.vigia_canonical_envelope`` y ``shared.vigia_canonical_audit_envelope`` en PostgreSQL 16
producen los mismos bytes que ``vigia_contracts.canonical`` (RFC 8785, VIG-14) sobre el mismo
sobre, y el ``record_hash`` (``entry_hash``) que calcula el disparador es el que se recalcula en
Python desde las columnas persistidas y el hash anterior.

Los generadores cubren lo que pide el patrón: nombres con acentos, caracteres fuera del plano
básico y de control (incluidos ``\\u007f``, ``\\u2028`` y ``\\u2029``, que RFC 8785 no escapa),
comillas y barras inversas; marcas límite (año 1 y 9999, época, 29 de febrero, cambio de mes y
de siglo); ``plant_id`` nulo, y las claves opcionales de ``actor`` y ``scope`` ausentes. Dos
caracteres no pueden llegar nunca a la base y no se generan: ``\\u0000`` (PostgreSQL no lo guarda
en ``text``) y los sustitutos sueltos (no son UTF-8 válido); los rechaza la base al insertar, antes
de cualquier hash, así que no hay divergencia posible por ellos.
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

pytestmark = pytest.mark.integration

MAX_SAFE_INTEGER = 2**53 - 1

# --- Generadores ----------------------------------------------------------------------------------

_TRICKY = (
    '"\\/\b\f\n\r\t\x01\x08\x0b\x0e\x1f\x7f\u0080\u009f\u00a0\u00ad\u2028\u2029\ufeff\uffff'
    "áéíóúÁÉÍÓÚñÑüÜçÇ"
    "\U0001f600\U0001f9ba\U00010000\U0010ffff\U0001d11e"
)
_any_character = st.characters(exclude_categories=["Cs"], exclude_characters="\x00")
_characters = st.one_of(st.sampled_from(_TRICKY), _any_character)


def _text(min_size: int = 0, max_size: int = 64) -> st.SearchStrategy[str]:
    return st.text(alphabet=_characters, min_size=min_size, max_size=max_size)


display_names = _text(min_size=1, max_size=120)
"""``display_name_snapshot`` (≤ 120 caracteres), el campo de texto libre del sobre."""

_BOUNDARY_TIMESTAMPS = (
    datetime(1, 1, 1, tzinfo=UTC),
    datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(1970, 1, 1, tzinfo=UTC),
    datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(1999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(2000, 1, 1, tzinfo=UTC),
    datetime(2024, 2, 29, 12, 0, 0, 1000, tzinfo=UTC),
    datetime(2026, 9, 30, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(2026, 10, 1, tzinfo=UTC),
    datetime(2038, 1, 19, 3, 14, 8, tzinfo=UTC),
)


def _to_milliseconds(value: datetime) -> datetime:
    return value.replace(microsecond=value.microsecond - value.microsecond % 1000)


timestamps = st.one_of(
    st.sampled_from(_BOUNDARY_TIMESTAMPS),
    st.datetimes(timezones=st.just(UTC)).map(_to_milliseconds),
)
"""Marcas con milisegundos, que es la precisión con la que el disparador las guarda."""

hex64 = st.binary(min_size=1, max_size=16).map(lambda data: hashlib.sha256(data).hexdigest())
optional_uuid = st.one_of(st.none(), st.uuids())


@st.composite
def record_envelope_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Columnas del sobre de un registro, con texto arbitrario incluso donde la tabla lo acota."""
    return {
        "record_id": draw(st.uuids()),
        "organization_id": draw(st.uuids()),
        "plant_id": draw(optional_uuid),
        "chain_sequence": draw(st.integers(1, MAX_SAFE_INTEGER)),
        "record_type": draw(_text()),
        "schema_version": draw(st.integers(1, 2**31 - 1)),
        "actor_kind": draw(_text()),
        "actor_id": draw(st.uuids()),
        "actor_display_name_snapshot": draw(display_names),
        "actor_role_in_use": draw(st.one_of(st.none(), _text())),
        "actor_concession_id": draw(optional_uuid),
        "actor_unit": draw(_text()),
        "scope_plant_id": draw(optional_uuid),
        "scope_zone_id": draw(optional_uuid),
        "scope_node_id": draw(optional_uuid),
        "correlation_id": draw(st.uuids()),
        "received_at": draw(timestamps),
        "content_hash": draw(hex64),
    }


@st.composite
def audit_envelope_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Columnas del sobre de una entrada de auditoría."""
    resource = draw(st.one_of(st.none(), st.tuples(_text(), st.uuids())))
    return {
        "entry_id": draw(st.uuids()),
        "organization_id": draw(st.uuids()),
        "chain_sequence": draw(st.integers(1, MAX_SAFE_INTEGER)),
        "actor_kind": draw(_text()),
        "actor_id": draw(st.uuids()),
        "actor_display_name_snapshot": draw(display_names),
        "actor_role_in_use": draw(st.one_of(st.none(), _text())),
        "actor_concession_id": draw(optional_uuid),
        "actor_unit": draw(_text()),
        "operation": draw(_text()),
        "scope_plant_id": draw(optional_uuid),
        "scope_zone_id": draw(optional_uuid),
        "resource_kind": None if resource is None else resource[0],
        "resource_id": None if resource is None else resource[1],
        "filters_hash": draw(st.one_of(st.none(), hex64)),
        "result_count": draw(st.one_of(st.none(), st.integers(0, 2**31 - 1))),
        "outcome": draw(_text()),
        "correlation_id": draw(st.uuids()),
        "occurred_at": draw(timestamps),
    }


_json_leaf = st.one_of(
    st.none(), st.booleans(), st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER), _text()
)
contents = st.dictionaries(
    _text(max_size=16),
    st.recursive(
        _json_leaf,
        lambda children: st.one_of(
            st.lists(children, max_size=4), st.dictionaries(_text(max_size=8), children, max_size=4)
        ),
        max_leaves=12,
    ),
    max_size=6,
)
"""Contenido JSON: se persiste como bytes canónicos y el disparador los hashea tal cual."""


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
