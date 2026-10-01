"""``OutboxPort.publish`` contra PostgreSQL 16 real como ``vigia_app`` (TASK-111; BR-NUC-75, 83).

Criterios de aceptación:

- con reversión inyectada tras ``publish`` (excepción del llamador, fallo de una sentencia
  posterior, fallo entre el evento y sus entregas o conexión cortada por el servidor), no queda ni
  ``OutboxEvent`` ni ``OutboxDelivery``;
- un evento con dos consumidores suscritos crea exactamente dos entregas ``pending`` (``attempts
  = 0``, vencidas al crearse, de la organización del contexto); sin suscriptores, ninguna;
- una carga con un campo de texto libre o de más de 64 KB se rechaza **antes de insertar**
  (ninguna sentencia enviada). El borde exacto: 65 536 B se aceptan y ``payload::text`` mide
  exactamente eso en la base; 65 537 B se rechazan.

Además: la partición generada coincide con la de Python; la organización y ``correlation_id``
salen del contexto; bajo otra organización el evento no se ve (RLS); el catálogo se sincroniza
en las tablas globales como ``vigia_app`` y una segunda sincronización no cambia nada; sin
catálogo sellado o con un evento no registrado no se publica.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from tests.factories import make_context
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import (
    BULK_EVENT,
    CONSUMER_A,
    CONSUMER_B,
    PROBE_EVENT,
    SOLO_EVENT,
    InjectedFault,
    StatementFaults,
    app_database,
    outbox_counts,
    probe_catalog,
    probe_payload,
    terminate_backend,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ContextAbsent
from vigia_platform.shared.db import Database, TemporarilyUnavailable, TransactionAborted
from vigia_platform.shared.outbox.publish import (
    MAX_PAYLOAD_BYTES,
    NewEvent,
    Outbox,
    OutboxRejected,
    partition_key,
    stored_payload_size,
)
from vigia_platform.shared.outbox.registries import Consumer, OutboxCatalog, Schedule
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 10, 30, 0, 123456, tzinfo=UTC)


async def _noop(*_: Any) -> None:
    return None


@pytest.fixture(scope="module")
def migrated(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "outbox_publish") as database:
        yield database


@pytest_asyncio.fixture
async def database(migrated: MigratedDatabase) -> AsyncIterator[Database]:
    database = app_database(migrated)
    yield database
    await database.dispose()


async def _synchronized(database: Database) -> OutboxCatalog:
    catalog = probe_catalog()
    async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
        await catalog.synchronize(SqlOutboxCatalogStore(transaction), SimulatedClock(NOW))
    return catalog


@pytest_asyncio.fixture
async def outbox(database: Database) -> Outbox:
    return Outbox(await _synchronized(database), SimulatedClock(NOW))


_SELECT_ALL = {
    "outbox_event": text("SELECT * FROM shared.outbox_event ORDER BY created_at, event_id"),
    "outbox_delivery": text(
        "SELECT * FROM shared.outbox_delivery ORDER BY event_id, consumer_name"
    ),
}


async def _rows(database: Database, context: Any, table: str) -> list[dict[str, Any]]:
    """Filas visibles con ``context`` (la seguridad a nivel de fila aplica)."""
    return [dict(row._mapping) for row in await database.read(context, _SELECT_ALL[table])]


# --- dos consumidores, dos entregas -------------------------------------------------------


@pytest.mark.asyncio
async def test_two_subscribed_consumers_get_exactly_two_pending_deliveries(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context()
    plant_id = uuid.uuid4()
    payload = probe_payload()
    async with database.transaction(context) as transaction:
        publication = await outbox.publish(
            transaction,
            NewEvent(
                event_name=PROBE_EVENT, payload=payload, plant_id=plant_id, ledger_sequence=42
            ),
        )
    assert publication.consumers == (CONSUMER_A, CONSUMER_B)

    events = await _rows(database, context, "outbox_event")
    deliveries = await _rows(database, context, "outbox_delivery")
    assert len(events) == 1
    event = events[0]
    created_at = datetime(2026, 9, 29, 10, 30, 0, 123000, tzinfo=UTC)
    # nuc_0010: la base numera cada inserción; sin tramo en curso no hay enlace de traza.
    assert isinstance(event.pop("publish_seq"), int)
    assert (event.pop("trace_id"), event.pop("span_id")) == (None, None)
    assert event == {
        "event_id": publication.event.event_id,
        "organization_id": context.organization_id,
        "plant_id": plant_id,
        "event_name": PROBE_EVENT,
        "partition_key": partition_key(context.organization_id, plant_id),
        "ledger_sequence": 42,
        "payload": payload,
        "correlation_id": context.correlation_id,
        "created_at": created_at,
    }
    assert publication.event.event_id.version == 7
    assert [(d["consumer_name"], d["status"], d["attempts"]) for d in deliveries] == [
        (CONSUMER_A, "pending", 0),
        (CONSUMER_B, "pending", 0),
    ]
    assert all(
        d["event_id"] == event["event_id"]
        and d["organization_id"] == context.organization_id
        and d["next_attempt_at"] == created_at
        and d["delivered_at"] is None
        and d["last_error_code"] is None
        for d in deliveries
    )
    assert await outbox_counts(migrated, context.organization_id) == (1, 2)


@pytest.mark.asyncio
async def test_subscriber_count_decides_the_deliveries(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context()
    async with database.transaction(context) as transaction:
        solo = await outbox.publish(
            transaction, NewEvent(event_name=SOLO_EVENT, payload=probe_payload())
        )
        bulk = await outbox.publish(
            transaction,
            NewEvent(event_name=BULK_EVENT, payload={"zone_ids": [], "pad": ""}),
        )
    assert (solo.consumers, bulk.consumers) == ((), (CONSUMER_A,))
    assert solo.event.partition_key == f"{context.organization_id}:organization"
    assert await outbox_counts(migrated, context.organization_id) == (2, 1)


@pytest.mark.asyncio
async def test_other_organization_does_not_see_the_event(
    database: Database, outbox: Outbox
) -> None:
    context = make_context()
    async with database.transaction(context) as transaction:
        await outbox.publish(transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload()))
    other = make_context()
    assert await _rows(database, other, "outbox_event") == []
    assert await _rows(database, other, "outbox_delivery") == []
    assert len(await _rows(database, context, "outbox_event")) == 1


@pytest.mark.asyncio
async def test_under_concession_the_event_belongs_to_the_client_organization(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context(kind=ActorKind.PROVIDER_USER)
    async with database.transaction(context) as transaction:
        publication = await outbox.publish(
            transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload())
        )
    assert publication.event.organization_id == context.organization_id
    assert await outbox_counts(migrated, context.organization_id) == (1, 2)


# --- reversión inyectada ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_exception_after_publish_leaves_nothing(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context()
    with pytest.raises(InjectedFault):
        async with database.transaction(context) as transaction:
            await outbox.publish(
                transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload())
            )
            raise InjectedFault("el cambio que produjo el evento falló después")
    assert await outbox_counts(migrated, context.organization_id) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_at", [1, 2, 3], ids=["event-insert", "delivery-insert", "next-statement"]
)
async def test_failing_statement_during_or_after_publish_leaves_nothing(
    database: Database, outbox: Outbox, migrated: MigratedDatabase, fail_at: int
) -> None:
    context = make_context()
    with pytest.raises((InjectedFault, TransactionAborted)):
        async with database.transaction(context) as transaction:
            faults = StatementFaults.install(transaction, fail_at=fail_at)
            try:
                await outbox.publish(
                    transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload())
                )
            finally:
                assert faults.statements >= min(fail_at, 2)
            await transaction.execute(text("SELECT 1"))
    assert await outbox_counts(migrated, context.organization_id) == (0, 0)


@pytest.mark.asyncio
async def test_connection_cut_between_event_and_deliveries_leaves_nothing(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context()
    with pytest.raises(TemporarilyUnavailable):
        async with database.transaction(context) as transaction:
            pid = (await transaction.execute(text("SELECT pg_backend_pid()"))).scalar_one()

            async def cut() -> None:
                await terminate_backend(migrated, pid)

            StatementFaults.install(transaction, fail_at=2, before_failure=cut)
            await outbox.publish(
                transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload())
            )
    assert await outbox_counts(migrated, context.organization_id) == (0, 0)
    # El pool se recupera: la siguiente publicación confirma.
    async with database.transaction(context) as transaction:
        await outbox.publish(transaction, NewEvent(event_name=PROBE_EVENT, payload=probe_payload()))
    assert await outbox_counts(migrated, context.organization_id) == (1, 2)


# --- rechazo antes de insertar ------------------------------------------------------------


async def _rejected(
    database: Database, outbox: Outbox, migrated: MigratedDatabase, event: NewEvent
) -> OutboxRejected:
    context = make_context()
    async with database.transaction(context) as transaction:
        faults = StatementFaults.install(transaction)
        with pytest.raises(OutboxRejected) as caught:
            await outbox.publish(transaction, event)
        assert faults.statements == 0, "se envió una sentencia antes de rechazar"
    assert await outbox_counts(migrated, context.organization_id) == (0, 0)
    return caught.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        probe_payload(comment="La persona del turno de noche"),
        probe_payload(code="Juan Pérez"),
        probe_payload(code="walk_test\n"),
        probe_payload(code="walk​test"),
        probe_payload(state="vacía"),
        probe_payload(zone_id="no es un uuid"),
    ],
    ids=["extra-field", "name-in-code", "trailing-newline", "zero-width", "enum", "uuid"],
)
async def test_free_text_payload_is_rejected_before_insert(
    database: Database, outbox: Outbox, migrated: MigratedDatabase, payload: dict[str, Any]
) -> None:
    rejected = await _rejected(
        database, outbox, migrated, NewEvent(event_name=PROBE_EVENT, payload=payload)
    )
    assert rejected.code == "payload_invalid"
    assert "Juan" not in str(rejected) and "persona" not in str(rejected)


def _bulk_payload(size: int) -> dict[str, Any]:
    """Carga de ``bulk_probe`` cuyo ``payload::text`` mide exactamente ``size`` bytes."""
    zone_ids: list[str] = []
    payload: dict[str, Any] = {"zone_ids": zone_ids, "pad": ""}
    while size - stored_payload_size(payload) > 1024:  # cada UUID añade 38 o 40 bytes
        zone_ids.append(str(uuid.UUID(int=len(zone_ids) + 1, version=4)))
    payload["pad"] = "a" * (size - stored_payload_size(payload))
    assert stored_payload_size(payload) == size
    return payload


@pytest.mark.asyncio
async def test_payload_limit_is_measured_as_postgres_stores_it(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    rejected = await _rejected(
        database,
        outbox,
        migrated,
        NewEvent(event_name=BULK_EVENT, payload=_bulk_payload(MAX_PAYLOAD_BYTES + 1)),
    )
    assert rejected.code == "payload_too_large"

    huge = {"zone_ids": [str(uuid.uuid4())] * 4000, "pad": "a" * 1024}
    assert (
        await _rejected(database, outbox, migrated, NewEvent(event_name=BULK_EVENT, payload=huge))
    ).code == "payload_too_large"

    context = make_context()
    async with database.transaction(context) as transaction:
        publication = await outbox.publish(
            transaction, NewEvent(event_name=BULK_EVENT, payload=_bulk_payload(MAX_PAYLOAD_BYTES))
        )
        size = (
            await transaction.execute(
                text(
                    "SELECT octet_length(payload::text) FROM shared.outbox_event"
                    " WHERE event_id = :event_id"
                ),
                {"event_id": publication.event.event_id},
            )
        ).scalar_one()
    assert size == MAX_PAYLOAD_BYTES


@pytest.mark.asyncio
async def test_database_constraint_agrees_with_the_limit(
    migrated: MigratedDatabase, database: Database, outbox: Outbox
) -> None:
    """Sin la validación de Python, la tabla rechaza igualmente 65 537 B (defensa en la base)."""
    connection = await migrated.connect("vigia_app")
    organization_id = uuid.uuid4()
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true)", str(organization_id)
            )
            insert = (
                "INSERT INTO shared.outbox_event (event_id, organization_id, event_name, payload,"
                " correlation_id, created_at) VALUES ($1, $2, $3, $4::jsonb, $5, now())"
            )
            await connection.execute(
                insert,
                uuid.uuid4(),
                organization_id,
                BULK_EVENT,
                json.dumps(_bulk_payload(MAX_PAYLOAD_BYTES)),
                uuid.uuid4(),
            )
            with pytest.raises(Exception, match="outbox_event_payload"):
                async with connection.transaction():
                    await connection.execute(
                        insert,
                        uuid.uuid4(),
                        organization_id,
                        BULK_EVENT,
                        json.dumps(_bulk_payload(MAX_PAYLOAD_BYTES + 1)),
                        uuid.uuid4(),
                    )
            raise InjectedFault("revertir")
    except InjectedFault:
        pass
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "code"),
    [
        (NewEvent(event_name="zone_unknown", payload=probe_payload()), "event_type_unknown"),
        (NewEvent(event_name="ZONE_PROBE", payload=probe_payload()), "event_type_unknown"),
        (
            NewEvent(event_name=PROBE_EVENT, payload=probe_payload(), plant_id="p"),  # type: ignore[arg-type]
            "partition_invalid",
        ),
        (
            NewEvent(event_name=PROBE_EVENT, payload=probe_payload(), ledger_sequence=0),
            "ledger_sequence_invalid",
        ),
        (
            NewEvent(event_name=PROBE_EVENT, payload=probe_payload(), ledger_sequence=2**63),
            "ledger_sequence_invalid",
        ),
        (
            NewEvent(event_name=PROBE_EVENT, payload=probe_payload(), ledger_sequence=True),
            "ledger_sequence_invalid",
        ),
        (NewEvent(event_name=PROBE_EVENT, payload=[1, 2]), "payload_invalid"),  # type: ignore[arg-type]
        (NewEvent(event_name=PROBE_EVENT, payload={"x": float("nan")}), "payload_invalid"),
    ],
    ids=[
        "unknown",
        "case",
        "plant-not-uuid",
        "sequence-zero",
        "sequence-overflow",
        "sequence-bool",
        "not-object",
        "nan",
    ],
)
async def test_invalid_events_are_rejected_before_insert(
    database: Database, outbox: Outbox, migrated: MigratedDatabase, event: NewEvent, code: str
) -> None:
    assert (await _rejected(database, outbox, migrated, event)).code == code


@pytest.mark.asyncio
async def test_ledger_sequence_limits_are_inclusive(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    context = make_context()
    async with database.transaction(context) as transaction:
        for sequence in (1, 2**63 - 1):
            await outbox.publish(
                transaction,
                NewEvent(event_name=SOLO_EVENT, payload=probe_payload(), ledger_sequence=sequence),
            )
    assert await outbox_counts(migrated, context.organization_id) == (2, 0)


@pytest.mark.asyncio
async def test_without_a_sealed_catalog_or_a_transaction_nothing_is_published(
    database: Database, migrated: MigratedDatabase
) -> None:
    unsealed = Outbox(probe_catalog(), SimulatedClock(NOW))
    rejected = await _rejected(
        database, unsealed, migrated, NewEvent(event_name=PROBE_EVENT, payload=probe_payload())
    )
    assert rejected.code == "outbox_not_ready"
    with pytest.raises(ContextAbsent):
        await unsealed.publish(None, NewEvent(event_name=PROBE_EVENT, payload=probe_payload()))  # type: ignore[arg-type]


# --- catálogo en las tablas globales ------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_is_persisted_by_the_application_role(
    database: Database, outbox: Outbox, migrated: MigratedDatabase
) -> None:
    connection = await migrated.connect()
    try:
        events = {
            row["event_name"]: row
            for row in await connection.fetch(
                "SELECT event_name, publisher_unit, payload_schema FROM shared.event_type"
            )
        }
        consumers = {
            row["consumer_name"]: list(row["subscribed_events"])
            for row in await connection.fetch(
                "SELECT consumer_name, subscribed_events FROM shared.consumer"
            )
        }
    finally:
        await connection.close()
    assert {PROBE_EVENT, BULK_EVENT, SOLO_EVENT, "security_alert", "dead_letter_created"} <= set(
        events
    )
    assert json.loads(events[PROBE_EVENT]["payload_schema"])["additionalProperties"] is False
    assert consumers == {
        CONSUMER_A: sorted([PROBE_EVENT, BULK_EVENT]),
        CONSUMER_B: [PROBE_EVENT],
    }
    # Una segunda sincronización (otro arranque) no escribe nada y vuelve a sellar.
    again = probe_catalog()
    async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
        faults = StatementFaults.install(transaction)
        await again.synchronize(SqlOutboxCatalogStore(transaction), SimulatedClock(NOW))
        assert faults.statements == 3  # solo las tres lecturas
    assert again.sealed


def test_resynchronizing_never_touches_circuit_or_lease(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    """Un arranque nuevo actualiza lo declarado, nunca el circuito ni el arrendamiento (TASK-129,
    TASK-130). Base propia: aquí se registra una tarea que el resto del módulo no declara."""

    def catalog(*, external: bool, schedule: Schedule) -> OutboxCatalog:
        result = probe_catalog()
        result.periodic_tasks.register("probe_task", schedule, _noop, unit=ActorUnit.U02)
        result.consumers.register(
            Consumer(
                consumer_name="probe_mailer",
                unit=ActorUnit.U04,
                subscribed_events=(PROBE_EVENT, BULK_EVENT) if external else (PROBE_EVENT,),
                handler=_noop,
                has_external_dependency=external,
            )
        )
        return result

    async def scenario(migrated: MigratedDatabase) -> tuple[dict[str, Any], dict[str, Any]]:
        database = app_database(migrated)
        try:
            for step, (external, schedule) in enumerate(
                [(False, Schedule.daily()), (True, Schedule.every(60))]
            ):
                async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as tx:
                    await catalog(external=external, schedule=schedule).synchronize(
                        SqlOutboxCatalogStore(tx), SimulatedClock(NOW)
                    )
                if step == 0:
                    admin = await migrated.connect()
                    try:
                        await admin.execute(
                            "UPDATE shared.consumer SET circuit_state = 'open',"
                            " circuit_opened_at = now() WHERE consumer_name = $1",
                            "probe_mailer",
                        )
                        await admin.execute(
                            "UPDATE shared.periodic_task SET lease_owner = 'worker-1',"
                            " lease_until = now() WHERE task_name = 'probe_task'"
                        )
                    finally:
                        await admin.close()
        finally:
            await database.dispose()
        admin = await migrated.connect()
        try:
            consumer = dict(
                await admin.fetchrow(
                    "SELECT circuit_state, circuit_opened_at FROM shared.consumer"
                    " WHERE consumer_name = $1",
                    "probe_mailer",
                )
            )
            task = dict(
                await admin.fetchrow(
                    "SELECT schedule, next_run_at, lease_owner FROM shared.periodic_task"
                    " WHERE task_name = 'probe_task'"
                )
            )
            mailer = await admin.fetchval(
                "SELECT has_external_dependency FROM shared.consumer WHERE consumer_name = $1",
                "probe_mailer",
            )
        finally:
            await admin.close()
        assert mailer is True
        return consumer, task

    with migrated_database(postgres_endpoint, "outbox_resync") as migrated:
        consumer, task = asyncio.run(scenario(migrated))
    assert consumer["circuit_state"] == "open" and consumer["circuit_opened_at"] is not None
    assert task["lease_owner"] == "worker-1"
    assert task["schedule"] == "every:60s"
    assert task["next_run_at"] == datetime(2026, 9, 29, 10, 31, tzinfo=UTC)
