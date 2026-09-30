"""``LectorExpediente``: alcance, auditoría de cada lectura y paginación por clave (TASK-114).

Contra PostgreSQL 16 real, como ``vigia_app``, con registros escritos por ``EscritorExpediente``:

- **Alcance** (criterio 1): la seguridad a nivel de fila limita a la organización y el puerto
  filtra además por ``allowed_scopes``. Un actor con asignación sobre la zona Z1 no obtiene
  registros de Z2 aunque sean de su organización, ni por ``list``, ni por ``get``, ni pidiendo
  ``zone_id = Z2`` en los filtros. La propiedad recorre combinaciones generadas de alcances
  (organización, planta, zona, de otra organización, vacíos) contra un oráculo en Python.
- **Una entrada por lectura** (criterio 2, BR-NUC-59, 62): cada ``list`` escribe exactamente una
  ``ledger_read`` con los filtros tal cual y ``result_count``; cada ``get``, exactamente una
  ``ledger_detail_read`` con el identificador pedido (también si no existe o está fuera de
  alcance); ``list_audit``, una ``audit_read``. En la misma transacción: si la entrada no se
  escribe, la lectura falla. Una consulta inválida no consulta ni audita.
- **Paginación por clave** (criterio 3, PAT-NUC-ESC-07): recorrer las páginas mientras se
  insertan registros nuevos entre una y otra devuelve cada registro original exactamente una vez
  y en orden; la propiedad genera tamaños de página e inserciones.

Solo datos generados: los clips son metadatos de bytes aleatorios que nunca se suben.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import InjectedFault
from tests.writer_support import (
    ORDER_TYPE,
    ORGANIZATION_TYPE,
    ZONE_TYPE,
    Fault,
    Place,
    WriterEnvironment,
    clips_of,
    order_document,
    organization_document,
    unit_context,
    writer_environment,
    zone_document,
)
from vigia_platform.ledger.application.reader import (
    MAX_FILTER_VALUES,
    MAX_PAGE_SIZE,
    AuditFilters,
    AuditPageRequest,
    LectorExpediente,
    LedgerFilters,
    LedgerQueryInvalid,
    LedgerRecordView,
    PageRequest,
    RecordCursor,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsent,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)
from vigia_platform.shared.db import TemporarilyUnavailable

pytestmark = pytest.mark.integration

NODE_TYPE = "node_declared"


# --- Entorno ----------------------------------------------------------------------------------


@dataclass
class ReaderEnvironment:
    env: WriterEnvironment
    reader: LectorExpediente

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    @property
    def migrated(self) -> MigratedDatabase:
        return self.env.migrated


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[ReaderEnvironment]:
    with (
        migrated_database(postgres_endpoint, "reader_scope") as migrated,
        writer_environment(migrated) as env,
    ):
        yield ReaderEnvironment(env, LectorExpediente(database=env.database, audit=env.audit))


def scoped_context(
    organization_id: uuid.UUID,
    scopes: Sequence[AllowedScope],
    *,
    role: Role = Role.PLANT_MANAGER,
) -> ScopeContext:
    """Contexto de sesión de una persona con exactamente ``scopes`` como asignaciones."""
    actor = Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        display_name_snapshot="Jefatura de planta sintética",
        unit=ActorUnit.U02,
        role_in_use=role,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.SESSION,
        allowed_scopes=list(scopes),
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest(),
    )


def zone_scope(zone_id: uuid.UUID) -> AllowedScope:
    return AllowedScope(ScopeLevel.ZONE, zone_id, Role.LINE_MANAGER)


def plant_scope(plant_id: uuid.UUID) -> AllowedScope:
    return AllowedScope(ScopeLevel.PLANT, plant_id, Role.PLANT_MANAGER)


def organization_scope(organization_id: uuid.UUID) -> AllowedScope:
    return AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.ADMINISTRATOR)


def write(environment: ReaderEnvironment, context: Any, record_type: str, document: Any) -> Receipt:
    env = environment.env
    for clip in clips_of(document):
        env.storage.put(clip)
    receipt = environment.run(env.writer.write(context, record_type, document))
    assert isinstance(receipt, Receipt), receipt
    return receipt


def write_order(environment: ReaderEnvironment, place: Place) -> uuid.UUID:
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    return write(environment, context, ORDER_TYPE, order_document(place)).record_id


def write_zone_created(environment: ReaderEnvironment, place: Place) -> uuid.UUID:
    context = unit_context(place.organization_id, ActorUnit.U02)
    document = zone_document(place)
    document["zone_id"] = str(place.zone_id)
    return write(environment, context, ZONE_TYPE, document).record_id


# --- Auditoría vista como superusuario ----------------------------------------------------------


@dataclass(frozen=True)
class AuditRow:
    operation: str
    filters: Any
    result_count: int | None
    resource_kind: str | None
    resource_id: uuid.UUID | None
    scope_plant_id: uuid.UUID | None
    scope_zone_id: uuid.UUID | None
    actor_id: uuid.UUID
    correlation_id: uuid.UUID


async def _audit_rows(migrated: MigratedDatabase, organization_id: uuid.UUID) -> list[AuditRow]:
    connection = await migrated.connect()
    try:
        rows = await connection.fetch(
            "SELECT operation, filters_json, result_count, resource_kind, resource_id,"
            " scope_plant_id, scope_zone_id, actor_id, correlation_id FROM shared.audit_entry"
            " WHERE organization_id = $1 ORDER BY chain_sequence",
            organization_id,
        )
    finally:
        await connection.close()
    return [
        AuditRow(
            operation=row["operation"],
            filters=None if row["filters_json"] is None else json.loads(row["filters_json"]),
            result_count=row["result_count"],
            resource_kind=row["resource_kind"],
            resource_id=row["resource_id"],
            scope_plant_id=row["scope_plant_id"],
            scope_zone_id=row["scope_zone_id"],
            actor_id=row["actor_id"],
            correlation_id=row["correlation_id"],
        )
        for row in rows
    ]


def audit_rows(environment: ReaderEnvironment, organization_id: uuid.UUID) -> list[AuditRow]:
    rows: list[AuditRow] = environment.run(_audit_rows(environment.migrated, organization_id))
    return rows


def new_entries(
    environment: ReaderEnvironment, organization_id: uuid.UUID, before: int
) -> list[AuditRow]:
    return audit_rows(environment, organization_id)[before:]


# --- Organización con dos plantas y tres zonas ------------------------------------------------


@dataclass(frozen=True)
class Tenant:
    """Organización A: planta P1 (zonas Z1, Z2), planta P2 (zona Z3); y otra organización B."""

    organization_id: uuid.UUID
    z1: Place
    z2: Place
    z3: Place
    other: Place
    records: dict[uuid.UUID, tuple[uuid.UUID | None, uuid.UUID | None]]
    """Registro de A → (planta, zona) de su alcance."""
    other_record: uuid.UUID

    def visible(self, scopes: Sequence[AllowedScope]) -> set[uuid.UUID]:
        """Oráculo: los registros de A que un contexto de A con ``scopes`` debe ver."""
        if any(
            s.scope_level is ScopeLevel.ORGANIZATION and s.scope_id == self.organization_id
            for s in scopes
        ):
            return set(self.records)
        plants = {s.scope_id for s in scopes if s.scope_level is ScopeLevel.PLANT}
        zones = {s.scope_id for s in scopes if s.scope_level is ScopeLevel.ZONE}
        return {
            record_id
            for record_id, (plant, zone) in self.records.items()
            if plant in plants or zone in zones
        }


@pytest.fixture(scope="module")
def tenant(environment: ReaderEnvironment) -> Tenant:
    organization_id = uuid.uuid4()
    z1 = Place.new(organization_id)
    z2 = Place(organization_id, z1.plant_id, uuid.uuid4(), uuid.uuid4())
    z3 = Place.new(organization_id)
    other = Place.new()
    records: dict[uuid.UUID, tuple[uuid.UUID | None, uuid.UUID | None]] = {}
    for place in (z1, z1, z2, z2, z3):
        records[write_order(environment, place)] = (place.plant_id, place.zone_id)
    # Un registro de planta sin zona (P1) y uno de organización sin planta.
    u02 = unit_context(organization_id, ActorUnit.U02)
    node = write(
        environment,
        u02,
        NODE_TYPE,
        {
            "node_id": str(uuid.uuid4()),
            "plant_id": str(z1.plant_id),
            "code": "N-01",
            "declared_by": str(uuid.uuid4()),
        },
    )
    records[node.record_id] = (z1.plant_id, None)
    genesis = write(environment, u02, ORGANIZATION_TYPE, organization_document(organization_id))
    records[genesis.record_id] = (None, None)
    other_record = write_order(environment, other)
    return Tenant(organization_id, z1, z2, z3, other, records, other_record)


def list_ids(
    environment: ReaderEnvironment,
    context: ScopeContext,
    filters: LedgerFilters | None = None,
) -> list[uuid.UUID]:
    page = environment.run(
        environment.reader.list(context, filters, PageRequest(size=MAX_PAGE_SIZE))
    )
    assert page.next_cursor is None
    return [item.record_id for item in page.items]


# --- Criterio 1: alcance --------------------------------------------------------------------


def test_zone_scope_never_returns_another_zone_of_the_same_organization(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    context = scoped_context(tenant.organization_id, [zone_scope(tenant.z1.zone_id)])
    z1_records = {r for r, (_, zone) in tenant.records.items() if zone == tenant.z1.zone_id}
    z2_records = {r for r, (_, zone) in tenant.records.items() if zone == tenant.z2.zone_id}
    assert len(z1_records) == 2
    assert len(z2_records) == 2

    assert set(list_ids(environment, context)) == z1_records
    # Pedir Z2 explícitamente, o toda la planta, no amplía el alcance.
    assert list_ids(environment, context, LedgerFilters(zone_id=tenant.z2.zone_id)) == []
    assert set(list_ids(environment, context, LedgerFilters(plant_id=tenant.z1.plant_id))) == (
        z1_records
    )
    for record_id in z2_records:
        assert environment.run(environment.reader.get(context, record_id)) is None
    for record_id in z1_records:
        view = environment.run(environment.reader.get(context, record_id))
        assert view is not None
        assert view.scope_zone_id == tenant.z1.zone_id


def test_plant_and_organization_scopes(environment: ReaderEnvironment, tenant: Tenant) -> None:
    p1 = scoped_context(tenant.organization_id, [plant_scope(tenant.z1.plant_id)])
    expected_p1 = {r for r, (plant, _) in tenant.records.items() if plant == tenant.z1.plant_id}
    assert len(expected_p1) == 5  # Z1, Z2 y el nodo de planta
    assert set(list_ids(environment, p1)) == expected_p1

    whole = scoped_context(tenant.organization_id, [organization_scope(tenant.organization_id)])
    assert set(list_ids(environment, whole)) == set(tenant.records)
    assert tenant.other_record not in list_ids(environment, whole)


@pytest.mark.parametrize(
    "case", ["empty", "foreign_organization_scope", "other_organization_context"]
)
def test_scope_fails_closed(environment: ReaderEnvironment, tenant: Tenant, case: str) -> None:
    if case == "empty":
        context = scoped_context(tenant.organization_id, [])
    elif case == "foreign_organization_scope":
        # Un alcance de organización con el identificador de otra no es el de toda la propia.
        context = scoped_context(tenant.organization_id, [organization_scope(uuid.uuid4())])
    else:
        # Contexto de B con alcances de A: la seguridad a nivel de fila oculta todo A.
        context = scoped_context(
            tenant.other.organization_id,
            [
                organization_scope(tenant.organization_id),
                plant_scope(tenant.z1.plant_id),
                zone_scope(tenant.z1.zone_id),
            ],
        )
    assert list_ids(environment, context) == []
    for record_id in tenant.records:
        assert environment.run(environment.reader.get(context, record_id)) is None


_SCOPE_CHOICES = st.sampled_from(
    ["org", "foreign_org", "p1", "p2", "z1", "z2", "z3", "other_zone", "other_plant", "random"]
)


@given(choices=st.lists(_SCOPE_CHOICES, max_size=4))
def test_scope_filter_matches_the_oracle(
    environment: ReaderEnvironment, tenant: Tenant, choices: list[str]
) -> None:
    table = {
        "org": organization_scope(tenant.organization_id),
        "foreign_org": organization_scope(tenant.other.organization_id),
        "p1": plant_scope(tenant.z1.plant_id),
        "p2": plant_scope(tenant.z3.plant_id),
        "z1": zone_scope(tenant.z1.zone_id),
        "z2": zone_scope(tenant.z2.zone_id),
        "z3": zone_scope(tenant.z3.zone_id),
        "other_zone": zone_scope(tenant.other.zone_id),
        "other_plant": plant_scope(tenant.other.plant_id),
    }
    scopes = [table.get(choice) or zone_scope(uuid.uuid4()) for choice in choices]
    context = scoped_context(tenant.organization_id, scopes)
    expected = tenant.visible(scopes)
    assert set(list_ids(environment, context)) == expected
    for record_id in (*tenant.records, tenant.other_record):
        view = environment.run(environment.reader.get(context, record_id))
        assert (view is not None) == (record_id in expected)


# --- Criterio 2: una entrada de auditoría por lectura -----------------------------------------


def test_each_list_writes_exactly_one_ledger_read_with_filters_and_count(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    context = scoped_context(tenant.organization_id, [plant_scope(tenant.z1.plant_id)])
    since = datetime(2020, 1, 1, tzinfo=UTC)
    until = datetime(2100, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    filters = LedgerFilters(
        record_types=(ORDER_TYPE,),
        plant_id=tenant.z1.plant_id,
        zone_id=tenant.z2.zone_id,
        received_from=since,
        received_before=until,
    )
    before = len(audit_rows(environment, tenant.organization_id))
    page = environment.run(environment.reader.list(context, filters, PageRequest(size=1)))
    entries = new_entries(environment, tenant.organization_id, before)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.operation == "ledger_read"
    assert entry.result_count == len(page.items) == 1
    assert entry.filters == {
        "record_types": [ORDER_TYPE],
        "plant_id": str(tenant.z1.plant_id),
        "zone_id": str(tenant.z2.zone_id),
        "received_from": "2020-01-01T00:00:00.000000Z",
        "received_before": "2100-01-01T00:00:00.123456Z",
        "page_size": 1,
    }
    assert (entry.scope_plant_id, entry.scope_zone_id) == (tenant.z1.plant_id, tenant.z2.zone_id)
    assert entry.actor_id == context.actor.id
    assert entry.correlation_id == context.correlation_id
    assert entry.resource_kind is None

    # La página siguiente: otra entrada, con la clave pedida; y una lista vacía también se audita.
    cursor = page.next_cursor
    assert isinstance(cursor, RecordCursor)
    second = environment.run(
        environment.reader.list(context, filters, PageRequest(size=1, after=cursor))
    )
    empty = environment.run(
        environment.reader.list(context, LedgerFilters(zone_id=tenant.z3.zone_id))
    )
    entries = new_entries(environment, tenant.organization_id, before)
    assert [e.operation for e in entries] == ["ledger_read"] * 3
    assert entries[1].result_count == len(second.items) == 1
    assert entries[1].filters["after"] == {
        "received_at": cursor.received_at.astimezone(UTC)
        .replace(tzinfo=None)
        .isoformat(timespec="microseconds")
        + "Z",
        "record_id": str(cursor.record_id),
    }
    assert empty.items == ()
    assert entries[2].result_count == 0
    assert entries[2].filters == {"zone_id": str(tenant.z3.zone_id), "page_size": 50}


def test_each_get_writes_exactly_one_detail_read_with_the_identifier(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    context = scoped_context(tenant.organization_id, [zone_scope(tenant.z1.zone_id)])
    visible = next(r for r, (_, zone) in tenant.records.items() if zone == tenant.z1.zone_id)
    hidden = next(r for r, (_, zone) in tenant.records.items() if zone == tenant.z2.zone_id)
    missing = uuid7()
    before = len(audit_rows(environment, tenant.organization_id))
    for record_id in (visible, hidden, missing, tenant.other_record):
        environment.run(environment.reader.get(context, record_id))
    entries = new_entries(environment, tenant.organization_id, before)
    assert [e.operation for e in entries] == ["ledger_detail_read"] * 4
    assert [(e.resource_kind, e.resource_id) for e in entries] == [
        ("ledger_record", visible),
        ("ledger_record", hidden),
        ("ledger_record", missing),
        ("ledger_record", tenant.other_record),
    ]
    assert [e.result_count for e in entries] == [1, 0, 0, 0]
    assert (entries[0].scope_plant_id, entries[0].scope_zone_id) == (
        tenant.z1.plant_id,
        tenant.z1.zone_id,
    )
    # Fuera de alcance no revela dónde está el registro.
    assert (entries[1].scope_plant_id, entries[1].scope_zone_id) == (None, None)
    assert all(e.filters is None for e in entries)
    # Nada se escribió en la cadena de la otra organización.
    assert all(
        e.resource_id != tenant.other_record
        for e in audit_rows(environment, tenant.other.organization_id)
    )


def test_list_audit_is_scoped_paginated_and_audited(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    organization_id = uuid.uuid4()
    z1 = Place.new(organization_id)
    z2 = Place(organization_id, z1.plant_id, uuid.uuid4(), uuid.uuid4())
    reader_z1 = scoped_context(organization_id, [zone_scope(z1.zone_id)])
    reader_z2 = scoped_context(organization_id, [zone_scope(z2.zone_id)])
    for context, place in ((reader_z1, z1), (reader_z1, z1), (reader_z2, z2)):
        environment.run(environment.reader.list(context, LedgerFilters(zone_id=place.zone_id)))
    admin = scoped_context(organization_id, [organization_scope(organization_id)])
    before = len(audit_rows(environment, organization_id))
    assert before == 3

    zone_page = environment.run(environment.reader.list_audit(reader_z1))
    assert [e.scope_zone_id for e in zone_page.items] == [z1.zone_id, z1.zone_id]
    assert all(e.operation == "ledger_read" for e in zone_page.items)
    assert zone_page.items[0].filters == {"zone_id": str(z1.zone_id), "page_size": 50}

    first = environment.run(
        environment.reader.list_audit(
            admin, AuditFilters(operations=("ledger_read",)), AuditPageRequest(size=2)
        )
    )
    assert len(first.items) == 2
    assert first.next_cursor is not None
    rest = environment.run(
        environment.reader.list_audit(
            admin,
            AuditFilters(operations=("ledger_read",)),
            AuditPageRequest(size=2, after=first.next_cursor),  # type: ignore[arg-type]
        )
    )
    seen = [e.entry_id for e in (*first.items, *rest.items)]
    assert len(seen) == len(set(seen)) == 3
    assert rest.next_cursor is None
    keys = [(e.occurred_at, e.entry_id) for e in (*first.items, *rest.items)]
    assert keys == sorted(keys, reverse=True)

    entries = new_entries(environment, organization_id, before)
    assert [e.operation for e in entries] == ["audit_read"] * 3
    assert [e.result_count for e in entries] == [2, 2, 1]
    assert entries[1].filters == {"operations": ["ledger_read"], "page_size": 2}
    assert entries[0].scope_zone_id is None


def test_the_audit_entry_is_in_the_same_transaction_as_the_read(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    context = scoped_context(tenant.organization_id, [zone_scope(tenant.z1.zone_id)])
    before = len(audit_rows(environment, tenant.organization_id))
    # Sentencias de la transacción de ``list``: registros, evidencias y la entrada de auditoría.
    environment.env.database.next_fault = Fault(statement=3)
    with pytest.raises(InjectedFault):
        environment.run(environment.reader.list(context))
    # ``get``: el registro, sus evidencias y la entrada.
    visible = next(r for r, (_, zone) in tenant.records.items() if zone == tenant.z1.zone_id)
    environment.env.database.next_fault = Fault(statement=3)
    with pytest.raises(InjectedFault):
        environment.run(environment.reader.get(context, visible))
    # El COMMIT que falla tampoco deja entrada ni devuelve la lectura.
    environment.env.database.next_fault = Fault(commit=True)
    with pytest.raises(TemporarilyUnavailable):
        environment.run(environment.reader.get(context, visible))
    assert len(audit_rows(environment, tenant.organization_id)) == before


def test_by_source_returns_identity_only_and_writes_no_entry(
    environment: ReaderEnvironment,
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    document = order_document(place)
    receipt = write(environment, context, ORDER_TYPE, document)
    before = len(audit_rows(environment, place.organization_id))

    match = environment.run(environment.reader.by_source(context, ORDER_TYPE, document["probe_id"]))
    assert match is not None
    assert (match.record_id, match.received_at) == (receipt.record_id, receipt.received_at)
    assert len(match.content_hash) == 64
    assert environment.run(environment.reader.by_source(context, ORDER_TYPE, str(uuid7()))) is None
    other = unit_context(uuid.uuid4(), ActorUnit.U03, kind=ActorKind.NODE)
    assert (
        environment.run(environment.reader.by_source(other, ORDER_TYPE, document["probe_id"]))
        is None
    )
    assert len(audit_rows(environment, place.organization_id)) == before
    for record_type, source_key in (
        ("Order", document["probe_id"]),
        (ORDER_TYPE, ""),
        (ORDER_TYPE, "x" * 65),
        (ORDER_TYPE, 7),
    ):
        with pytest.raises(LedgerQueryInvalid):
            environment.run(environment.reader.by_source(context, record_type, source_key))


# --- Referencias de evidencias, nunca contenido ------------------------------------------------


def test_records_carry_evidence_references_never_evidence_content(
    environment: ReaderEnvironment,
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    document = order_document(place, clips=2)
    receipt = write(environment, context, ORDER_TYPE, document)
    reader = scoped_context(place.organization_id, [zone_scope(place.zone_id)])
    view = environment.run(environment.reader.get(reader, receipt.record_id))
    assert isinstance(view, LedgerRecordView)
    listed = environment.run(environment.reader.list(reader)).items
    assert [item.record_id for item in listed] == [receipt.record_id]
    assert listed[0] == view

    clips = clips_of(document)
    assert [e.clip_id for e in view.evidences] == [uuid.UUID(c["clip_id"]) for c in clips]
    for evidence, clip in zip(view.evidences, clips, strict=True):
        assert evidence.sha256 == clip["sha256"]
        assert evidence.size_bytes == clip["size_bytes"]
        assert evidence.media_kind == "video"
    # Solo referencias: ni clave de almacén ni URL ni bytes del clip.
    fields = set(type(view.evidences[0]).__dataclass_fields__)
    assert not fields & {"storage_key", "url", "content", "data"}
    assert view.document()["probe_id"] == document["probe_id"]
    assert view.content_hash == hashlib.sha256(view.content).hexdigest()
    assert view.actor.unit == "U-03"


# --- Entradas inválidas: ni consulta ni auditoría ---------------------------------------------


_NAIVE = datetime(2026, 9, 29, 10, 0)  # noqa: DTZ001 - entrada hostil sin zona a propósito
_AWARE = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("filters", "page"),
    [
        (None, PageRequest(size=0)),
        (None, PageRequest(size=MAX_PAGE_SIZE + 1)),
        (None, PageRequest(size=True)),
        (None, PageRequest(size="5")),  # type: ignore[arg-type]
        (None, PageRequest(after=RecordCursor(_NAIVE, uuid7()))),
        (None, PageRequest(after=RecordCursor(_AWARE, str(uuid7())))),  # type: ignore[arg-type]
        (None, PageRequest(after=RecordCursor(_AWARE, None))),  # type: ignore[arg-type]
        (LedgerFilters(record_types=(ORDER_TYPE,) * (MAX_FILTER_VALUES + 1)), None),
        (LedgerFilters(record_types=["order_probe"]), None),  # type: ignore[arg-type]
        (LedgerFilters(record_types=("order_probe%",)), None),
        (LedgerFilters(record_types=("Order_probe",)), None),
        (LedgerFilters(record_types=(" order_probe",)), None),
        (LedgerFilters(record_types=("order_probe​",)), None),
        (LedgerFilters(record_types=("",)), None),
        (LedgerFilters(record_types=("a" * 65,)), None),
        (LedgerFilters(zone_id=str(uuid.uuid4())), None),  # type: ignore[arg-type]
        (LedgerFilters(received_from=_NAIVE), None),
        (LedgerFilters(received_from=_AWARE, received_before=_AWARE), None),
        (LedgerFilters(received_from=_AWARE + timedelta(seconds=1), received_before=_AWARE), None),
        ({"zone_id": None}, None),
    ],
)
def test_invalid_queries_neither_read_nor_audit(
    environment: ReaderEnvironment, tenant: Tenant, filters: Any, page: Any
) -> None:
    context = scoped_context(tenant.organization_id, [organization_scope(tenant.organization_id)])
    before = len(audit_rows(environment, tenant.organization_id))
    with pytest.raises(LedgerQueryInvalid):
        environment.run(environment.reader.list(context, filters, page))
    assert len(audit_rows(environment, tenant.organization_id)) == before


def test_query_bounds_are_accepted(environment: ReaderEnvironment, tenant: Tenant) -> None:
    context = scoped_context(tenant.organization_id, [organization_scope(tenant.organization_id)])
    widest = LedgerFilters(
        record_types=tuple(f"t{index:02d}_" + "a" * 60 for index in range(MAX_FILTER_VALUES)),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        received_from=_AWARE,
        received_before=_AWARE + timedelta(milliseconds=1),
    )
    before = len(audit_rows(environment, tenant.organization_id))
    for filters, size in ((widest, MAX_PAGE_SIZE), (LedgerFilters(), 1)):
        page = environment.run(environment.reader.list(context, filters, PageRequest(size=size)))
        assert len(page.items) <= size
    entries = new_entries(environment, tenant.organization_id, before)
    assert len(entries) == 2
    assert len(entries[0].filters["record_types"]) == MAX_FILTER_VALUES


def test_invalid_get_and_audit_queries_and_missing_context(
    environment: ReaderEnvironment, tenant: Tenant
) -> None:
    context = scoped_context(tenant.organization_id, [organization_scope(tenant.organization_id)])
    before = len(audit_rows(environment, tenant.organization_id))
    for record_id in (None, str(uuid7()), 7):
        with pytest.raises(LedgerQueryInvalid):
            environment.run(environment.reader.get(context, record_id))
    for filters in (
        AuditFilters(operations=("ledger read",)),
        AuditFilters(actor_id="x"),  # type: ignore[arg-type]
        AuditFilters(occurred_before=_NAIVE),
    ):
        with pytest.raises(LedgerQueryInvalid):
            environment.run(environment.reader.list_audit(context, filters))
    with pytest.raises(LedgerQueryInvalid):
        environment.run(environment.reader.list_audit(context, None, AuditPageRequest(size=201)))
    assert len(audit_rows(environment, tenant.organization_id)) == before
    for call in (
        environment.reader.list(None),  # type: ignore[arg-type]
        environment.reader.get(None, uuid7()),  # type: ignore[arg-type]
        environment.reader.by_source(None, ORDER_TYPE, "k"),  # type: ignore[arg-type]
        environment.reader.list_audit(None),  # type: ignore[arg-type]
    ):
        with pytest.raises(ContextAbsent):
            environment.run(call)


# --- Criterio 3: paginación por clave con inserciones entre páginas ---------------------------


def walk_with_inserts(
    environment: ReaderEnvironment,
    place: Place,
    context: ScopeContext,
    size: int,
    inserts: Sequence[int],
) -> tuple[list[list[LedgerRecordView]], set[uuid.UUID]]:
    """Recorre todas las páginas; antes de la página ``i + 1`` inserta ``inserts[i]`` registros."""
    pages: list[list[LedgerRecordView]] = []
    inserted: set[uuid.UUID] = set()
    cursor: RecordCursor | None = None
    while True:
        page = environment.run(
            environment.reader.list(context, None, PageRequest(size=size, after=cursor))
        )
        pages.append(list(page.items))
        if page.next_cursor is None:
            return pages, inserted
        assert isinstance(page.next_cursor, RecordCursor)
        cursor = page.next_cursor
        assert cursor == page.items[-1].cursor
        for _ in range(inserts[len(pages) - 1] if len(pages) - 1 < len(inserts) else 0):
            inserted.add(write_zone_created(environment, place))


def assert_walk(
    pages: list[list[LedgerRecordView]],
    original: list[uuid.UUID],
    inserted: set[uuid.UUID],
    size: int,
) -> None:
    """Cada original una vez y en orden; lo nuevo solo empata con la clave de la página previa."""
    walked = [item for page in pages for item in page]
    ids = [item.record_id for item in walked]
    assert len(ids) == len(set(ids)), "una página repitió registros"
    assert [record_id for record_id in ids if record_id not in inserted] == original
    keys = [(item.received_at, item.record_id) for item in walked]
    assert keys == sorted(keys, reverse=True)
    for previous, page in itertools.pairwise(pages):
        for item in page:
            if item.record_id in inserted:
                # Solo un registro nuevo del mismo milisegundo que la clave puede ordenarse detrás.
                assert item.received_at == previous[-1].received_at
    assert all(len(page) <= size for page in pages)


def test_page_two_neither_repeats_nor_omits_with_inserts_between_pages(
    environment: ReaderEnvironment,
) -> None:
    place = Place.new()
    original = [write_zone_created(environment, place) for _ in range(7)]
    context = scoped_context(place.organization_id, [zone_scope(place.zone_id)])
    snapshot = list_ids(environment, context)
    assert set(snapshot) == set(original)
    pages, inserted = walk_with_inserts(environment, place, context, 3, [4, 2])
    assert len(inserted) == 6
    assert_walk(pages, snapshot, inserted, 3)
    assert [item.record_id for item in pages[1] if item.record_id not in inserted] == snapshot[3:6]
    assert set(list_ids(environment, context)) == set(original) | inserted


@given(
    records=st.integers(min_value=0, max_value=8),
    size=st.integers(min_value=1, max_value=4),
    inserts=st.lists(st.integers(min_value=0, max_value=3), max_size=4),
)
def test_keyset_walk_property(
    environment: ReaderEnvironment, records: int, size: int, inserts: list[int]
) -> None:
    place = Place.new()
    for _ in range(records):
        write_zone_created(environment, place)
    context = scoped_context(place.organization_id, [organization_scope(place.organization_id)])
    snapshot = list_ids(environment, context)
    assert len(snapshot) == records
    pages, inserted = walk_with_inserts(environment, place, context, size, inserts)
    assert_walk(pages, snapshot, inserted, size)
    expected_pages = max(1, -(-records // size))
    assert len(pages) >= expected_pages
