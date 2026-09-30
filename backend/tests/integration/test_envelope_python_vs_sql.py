"""El sobre canónico en Python es el de la base (LC-NUC-09, TASK-109; PAT-NUC-REN-01).

``ledger.canonical.envelope_canonical`` y ``audit_envelope_canonical`` producen los mismos bytes
que ``ledger.vigia_canonical_envelope`` y ``shared.vigia_canonical_audit_envelope`` (TASK-108):

- para al menos 1 000 sobres generados de cada clase por semilla, con los generadores del oráculo
  compartidos con PR-NUC-47 (acentos, fuera del plano básico, controles, marcas límite), marcas con
  microsegundos (la base las trunca a milisegundos), y cualquier columna nula;
- para filas reales escritas por el disparador y leídas con ``asyncpg`` (UUID de ``asyncpg``,
  marcas de ``timestamptz``), con el ``record_hash`` y el ``entry_hash`` recalculados en Python.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    DatabaseLoop,
    MigratedDatabase,
    audit_values,
    chain_hash,
    genesis_hash,
    insert_audit,
    insert_record,
    migrated_database,
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
from vigia_platform.ledger.canonical import (
    CanonicalFormError,
    audit_envelope_canonical,
    canonical_bytes_sync,
    envelope_canonical,
)

pytestmark = pytest.mark.integration

ENVELOPES = 1_000
"""Sobres generados por clase y por semilla (criterio de aceptación de TASK-109)."""

BOGOTA = timezone(timedelta(hours=-5))

_envelopes = settings(max_examples=max(ENVELOPES, settings.default.max_examples))


@st.composite
def _with_nulls_and_microseconds(
    draw: st.DrawFn, rows: st.SearchStrategy[dict[str, Any]], timestamp: str
) -> dict[str, Any]:
    """Fila generada con microsegundos en la marca y, a veces, columnas nulas cualesquiera."""
    row = dict(draw(rows))
    moment: datetime = row[timestamp]
    if moment.microsecond <= 999_000:
        row[timestamp] = moment + timedelta(microseconds=draw(st.integers(0, 999)))
    for column in draw(st.sets(st.sampled_from(sorted(row)), max_size=4)):
        row[column] = None
    return row


record_rows = _with_nulls_and_microseconds(record_envelope_rows(), "received_at")
audit_rows = _with_nulls_and_microseconds(audit_envelope_rows(), "occurred_at")


# --- Fixtures -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_envelope") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def loop() -> Iterator[DatabaseLoop]:
    runner = DatabaseLoop()
    yield runner
    runner.close()


@pytest.fixture(scope="module")
def app(database: MigratedDatabase, loop: DatabaseLoop) -> Iterator[Any]:
    owner = loop.run(database.connect())
    loop.run(register_record_types(owner))
    loop.run(owner.close())
    connection = loop.run(database.connect("vigia_app"))
    yield connection
    loop.run(connection.close())


def _as_json(row: dict[str, Any]) -> str:
    def encode(value: object) -> object:
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    return json.dumps({key: encode(value) for key, value in row.items()}, ensure_ascii=False)


def _sql(loop: DatabaseLoop, app: Any, function: str, table: str, row: dict[str, Any]) -> bytes:
    envelope: bytes = loop.run(
        app.fetchval(
            f"SELECT {function}(jsonb_populate_record(NULL::{table}, $1::jsonb))",
            _as_json(row),
        )
    )
    return envelope


# --- Sobres generados -----------------------------------------------------------------------------


@_envelopes
@given(row=record_rows)
def test_record_envelope_equals_sql(loop: DatabaseLoop, app: Any, row: dict[str, Any]) -> None:
    expected = _sql(loop, app, "ledger.vigia_canonical_envelope", "ledger.ledger_record", row)
    assert envelope_canonical(row) == expected


@_envelopes
@given(row=audit_rows)
def test_audit_envelope_equals_sql(loop: DatabaseLoop, app: Any, row: dict[str, Any]) -> None:
    expected = _sql(loop, app, "shared.vigia_canonical_audit_envelope", "shared.audit_entry", row)
    assert audit_envelope_canonical(row) == expected


def test_timestamp_offsets_are_normalized_to_utc(loop: DatabaseLoop, app: Any) -> None:
    """Una marca con otra zona horaria da el mismo sobre que en UTC, en Python y en SQL."""
    row = {
        "record_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "plant_id": None,
        "chain_sequence": 7,
        "record_type": "zone_created",
        "schema_version": 1,
        "actor_kind": "user",
        "actor_id": uuid.uuid4(),
        "actor_display_name_snapshot": "Coordinación SST",
        "actor_role_in_use": None,
        "actor_concession_id": None,
        "actor_unit": "U-02",
        "scope_plant_id": None,
        "scope_zone_id": None,
        "scope_node_id": None,
        "correlation_id": uuid.uuid4(),
        "received_at": datetime(2026, 9, 30, 19, 30, 0, 123999, tzinfo=BOGOTA),
        "content_hash": "b" * 64,
    }
    in_utc = dict(row, received_at=row["received_at"].astimezone(UTC))
    envelope = envelope_canonical(row)
    assert b'"received_at":"2026-10-01T00:30:00.123Z"' in envelope
    assert envelope == envelope_canonical(in_utc)
    assert envelope == _sql(
        loop, app, "ledger.vigia_canonical_envelope", "ledger.ledger_record", row
    )


# --- Filas reales escritas por el disparador ------------------------------------------------------


@given(
    plant=st.booleans(),
    names=st.lists(display_names, min_size=1, max_size=3),
    documents=st.lists(contents, min_size=1, max_size=3),
)
def test_persisted_rows_rebuild_the_chain_in_python(
    loop: DatabaseLoop, app: Any, plant: bool, names: list[str], documents: list[dict[str, Any]]
) -> None:
    organization_id = uuid.uuid4()
    plant_id = uuid.uuid4() if plant else None
    values = []
    for index, name in enumerate(names):
        value = record_values(organization_id, plant_id, display_name=name)
        value["content"] = canonical_bytes_sync(documents[index % len(documents)])
        values.append(value)

    async def write() -> tuple[list[Any], list[bytes]]:
        async with app.transaction():
            await set_organization(app, organization_id)
            rows = [await insert_record(app, value) for value in values]
            envelopes = [
                await app.fetchval(
                    "SELECT ledger.vigia_canonical_envelope(r) FROM ledger.ledger_record r"
                    " WHERE r.record_id = $1",
                    row["record_id"],
                )
                for row in rows
            ]
            return rows, envelopes

    rows, sql_envelopes = loop.run(write())
    previous = genesis_hash(organization_id, plant_id)
    for row, sql_envelope in zip(rows, sql_envelopes, strict=True):
        envelope = envelope_canonical(row)
        assert envelope == sql_envelope
        assert row["content_hash"] == hashlib.sha256(row["content"]).hexdigest()
        assert row["previous_hash"] == previous
        assert row["record_hash"] == chain_hash(envelope, previous)
        previous = row["record_hash"]


@given(filters=st.one_of(st.none(), contents), count=st.integers(1, 3))
def test_persisted_audit_rows_rebuild_the_chain_in_python(
    loop: DatabaseLoop, app: Any, filters: dict[str, Any] | None, count: int
) -> None:
    organization_id = uuid.uuid4()

    async def write() -> tuple[list[Any], list[bytes]]:
        async with app.transaction():
            await set_organization(app, organization_id)
            rows = [
                await insert_audit(app, audit_values(organization_id, filters=filters))
                for _ in range(count)
            ]
            envelopes = [
                await app.fetchval(
                    "SELECT shared.vigia_canonical_audit_envelope(a) FROM shared.audit_entry a"
                    " WHERE a.entry_id = $1",
                    row["entry_id"],
                )
                for row in rows
            ]
            return rows, envelopes

    rows, sql_envelopes = loop.run(write())
    previous = genesis_hash(organization_id, None)
    for row, sql_envelope in zip(rows, sql_envelopes, strict=True):
        envelope = audit_envelope_canonical(row)
        assert envelope == sql_envelope
        assert row["entry_hash"] == chain_hash(envelope, previous)
        previous = row["entry_hash"]


# --- Entradas inválidas ---------------------------------------------------------------------------


def _valid_row() -> dict[str, Any]:
    value = record_values(uuid.uuid4(), uuid.uuid4())
    value["received_at"] = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    value["chain_sequence"] = 1
    return value


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("record_id", "01900000-0000-7000-8000-000000000001"),
        ("organization_id", b"\x00" * 16),
        ("actor_display_name_snapshot", 12),
        ("record_type", b"zone_created"),
        ("chain_sequence", True),
        ("chain_sequence", "1"),
        ("chain_sequence", 1.0),
        ("chain_sequence", 2**53),
        ("schema_version", -(2**53)),
        ("received_at", datetime(2026, 9, 29, 12, 0)),  # noqa: DTZ001 - sin zona a propósito
        ("received_at", "2026-09-29T12:00:00.000Z"),
        ("received_at", datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))),
        ("actor_display_name_snapshot", "\ud800"),
    ],
)
def test_invalid_columns_raise_canonical_form_error(column: str, value: object) -> None:
    row = _valid_row()
    envelope_canonical(row)  # la fila base es válida
    row[column] = value
    with pytest.raises(CanonicalFormError):
        envelope_canonical(row)


@pytest.mark.parametrize("column", ["record_id", "received_at", "scope_node_id", "actor_unit"])
def test_missing_column_raises_canonical_form_error(column: str) -> None:
    row = _valid_row()
    del row[column]
    with pytest.raises(CanonicalFormError, match=column):
        envelope_canonical(row)


def test_missing_audit_column_raises_canonical_form_error() -> None:
    entry = audit_values(uuid.uuid4())
    entry["occurred_at"] = datetime(2026, 9, 29, tzinfo=UTC)
    entry["filters_hash"] = None  # lo calcula el disparador
    audit_envelope_canonical(entry)
    del entry["resource_kind"]
    with pytest.raises(CanonicalFormError, match="resource_kind"):
        audit_envelope_canonical(entry)
