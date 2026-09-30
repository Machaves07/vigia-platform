"""PR-NUC-32 (atomicidad completa) y la transacción corta del escritor (TASK-113; BR-NUC-50).

Contra PostgreSQL 16 real, como ``vigia_app``:

- **PR-NUC-32**: para toda escritura generada (registro con 0 a 3 evidencias y un evento con dos
  consumidores, o una clasificación que proyecta su etiqueta y publica un evento) y todo punto de
  fallo inyectado (cualquier sentencia de la transacción, el ``COMMIT`` o una carga de evento
  inválida), no queda **nada**: ni registro, ni clave de idempotencia, ni evidencia, ni etiqueta,
  ni evento, ni entrega, y la cabeza de la cadena no avanza. La misma escritura, repetida sin
  fallo, se acepta con la secuencia siguiente (sin huecos) y deja exactamente una etiqueta por
  registro fuente (BR-NUC-67).
- **Transacción corta** (PAT-NUC-RES-08): un almacén instrumentado cuenta toda llamada hecha con
  una transacción de escritura abierta; en todas las escrituras de este módulo, cero.
- Almacén caído → ``StorageUnavailable`` sin abrir la transacción y sin la clave en la causa;
  cabeza retenida más que ``lock_timeout`` → ``chain_locked_timeout`` sin dejar nada.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import PROBE_EVENT, SOLO_EVENT, SUBSCRIBERS, probe_payload
from tests.writer_support import (
    CLASSIFICATION_TYPE,
    ORDER_TYPE,
    Fault,
    Place,
    WriterEnvironment,
    classification_document,
    order_document,
    organization_counts,
    unit_context,
    verify_ledger_chains,
    writer_environment,
)
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.storage import StorageUnavailable

pytestmark = pytest.mark.integration

LOCK_TIMEOUT_MS = 500


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "writer_atomicity") as migrated,
        writer_environment(migrated, lock_timeout_ms=LOCK_TIMEOUT_MS) as environment,
    ):
        yield environment


@pytest.fixture(autouse=True)
def _no_storage_call_inside_a_transaction(environment: WriterEnvironment) -> Iterator[None]:
    """Criterio 5: ninguna llamada al almacén con la transacción de escritura abierta."""
    yield
    assert environment.storage.calls_in_transaction == 0


@dataclass(frozen=True)
class Write:
    record_type: str
    unit: ActorUnit
    document: dict[str, Any]
    events: tuple[NewEvent, ...]
    statements: int
    """Sentencias de la transacción: registro, evidencias, etiqueta, evento y entregas."""


def _event_statements(name: str) -> int:
    return 1 + bool(SUBSCRIBERS[name])


def _prepare_subject(environment: WriterEnvironment, place: Place, clips: int) -> uuid.UUID:
    """Un registro con ``clips`` evidencias que la clasificación referencia."""
    document = order_document(place, clips=clips)
    for clip in document["clips"]:
        environment.storage.put(clip)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    receipt = environment.loop.run(environment.writer.write(context, ORDER_TYPE, document))
    assert isinstance(receipt, Receipt), receipt
    return receipt.record_id


@st.composite
def writes(draw: st.DrawFn, environment: WriterEnvironment, place: Place) -> Write:
    if draw(st.booleans()):
        clips = draw(st.integers(0, 3))
        document = order_document(place, clips=clips, level=draw(st.integers(0, 1000)))
        for clip in document["clips"]:
            environment.storage.put(clip)
        event = NewEvent(event_name=PROBE_EVENT, payload=probe_payload(zone_id=str(place.zone_id)))
        return Write(
            ORDER_TYPE,
            ActorUnit.U03,
            document,
            (event,),
            1 + clips + _event_statements(PROBE_EVENT),
        )
    subject = _prepare_subject(environment, place, draw(st.integers(0, 2)))
    document = classification_document(
        place,
        subject,
        outcome=draw(st.sampled_from(["confirmed", "authorized_operation", "false_positive"])),
    )
    event = NewEvent(event_name=SOLO_EVENT, payload=probe_payload(zone_id=str(place.zone_id)))
    return Write(
        CLASSIFICATION_TYPE, ActorUnit.U04, document, (event,), 2 + _event_statements(SOLO_EVENT)
    )


@st.composite
def faults(draw: st.DrawFn, statements: int) -> Fault | str:
    return draw(
        st.builds(Fault, statement=st.integers(1, statements))
        | st.just(Fault(commit=True))
        | st.just("invalid_event_payload")
    )


@given(data=st.data())
def test_injected_rollback_at_any_step_leaves_nothing(
    environment: WriterEnvironment, data: st.DataObject
) -> None:
    place = Place.new()
    write = data.draw(writes(environment, place))
    fault = data.draw(faults(write.statements))
    context = unit_context(place.organization_id, write.unit)
    loop, migrated = environment.loop, environment.migrated
    before = loop.run(organization_counts(migrated, place.organization_id))
    length_before = loop.run(verify_ledger_chains(migrated, place.organization_id))

    events = write.events
    if fault == "invalid_event_payload":
        events = (NewEvent(event_name=events[0].event_name, payload={"zone_id": "x"}),)
    else:
        assert isinstance(fault, Fault)
        environment.database.next_fault = fault
    with pytest.raises(Exception) as raised:
        loop.run(
            environment.writer.write(context, write.record_type, write.document, events=events)
        )
    assert not isinstance(raised.value, AssertionError)
    environment.database.next_fault = None

    assert loop.run(organization_counts(migrated, place.organization_id)) == before
    assert loop.run(verify_ledger_chains(migrated, place.organization_id)) == length_before

    # La misma escritura sin fallo: aceptada, contigua y completa.
    receipt = loop.run(
        environment.writer.write(context, write.record_type, write.document, events=write.events)
    )
    assert isinstance(receipt, Receipt), receipt
    after = loop.run(organization_counts(migrated, place.organization_id))
    assert after["ledger.ledger_record"] == before["ledger.ledger_record"] + 1
    clips = len(write.document.get("clips", ()))
    assert after["ledger.evidence"] == before["ledger.evidence"] + clips
    labels = 1 if write.record_type == CLASSIFICATION_TYPE else 0
    assert after["ledger.label"] == before["ledger.label"] + labels
    assert after["shared.outbox_event"] == before["shared.outbox_event"] + 1
    lengths = loop.run(verify_ledger_chains(migrated, place.organization_id))
    assert lengths[place.plant_id] == length_before.get(place.plant_id, 0) + 1
    if labels:
        loop.run(_assert_single_label(environment, receipt.record_id, write.document))


async def _assert_single_label(
    environment: WriterEnvironment, source_record_id: uuid.UUID, document: dict[str, Any]
) -> None:
    connection = await environment.migrated.connect()
    try:
        labels = await connection.fetch(
            "SELECT * FROM ledger.label WHERE source_record_id = $1", source_record_id
        )
        subject = uuid.UUID(document["anchor_record_id"])
        evidence = await connection.fetch(
            "SELECT evidence_id FROM ledger.evidence WHERE record_id = $1"
            " ORDER BY verified_at, evidence_id",
            subject,
        )
        record = await connection.fetchrow(
            "SELECT received_at FROM ledger.ledger_record WHERE record_id = $1", source_record_id
        )
    finally:
        await connection.close()
    assert len(labels) == 1
    label = labels[0]
    assert label["subject_record_id"] == subject
    assert (label["family"], label["outcome"], label["reason_category"]) == (
        document["family"],
        document["outcome"],
        document["reason_category"],
    )
    assert list(label["evidence_ids"]) == [row["evidence_id"] for row in evidence]
    assert label["labeled_at"] == record["received_at"]
    assert (label["plant_id"], label["zone_id"]) == (
        uuid.UUID(document["plant_id"]),
        uuid.UUID(document["zone_id"]),
    )


def test_storage_unavailable_never_opens_the_transaction(environment: WriterEnvironment) -> None:
    place = Place.new()
    document = order_document(place, clips=2)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    opened = environment.database.probe.opened
    environment.storage.unavailable = True
    try:
        with pytest.raises(StorageUnavailable) as raised:
            environment.loop.run(environment.writer.write(context, ORDER_TYPE, document))
    finally:
        environment.storage.unavailable = False
    # La causa (que puede citar la clave del objeto) no viaja con la excepción (VIG-32).
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "org/" not in str(raised.value)
    assert environment.database.probe.opened == opened
    counts = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert counts["ledger.ledger_record"] == 0


def test_held_chain_head_gives_chain_locked_timeout_and_leaves_nothing(
    environment: WriterEnvironment,
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    loop, migrated = environment.loop, environment.migrated
    first = loop.run(environment.writer.write(context, ORDER_TYPE, order_document(place, clips=0)))
    assert isinstance(first, Receipt)

    async def held_write() -> Receipt | LedgerRejection:
        blocker: Any = await asyncpg.connect(migrated.as_role().dsn)
        try:
            async with blocker.transaction():
                await blocker.execute(
                    "SELECT 1 FROM ledger.chain_head WHERE organization_id = $1"
                    " AND plant_id = $2 FOR UPDATE",
                    place.organization_id,
                    place.plant_id,
                )
                return await environment.writer.write(
                    context, ORDER_TYPE, order_document(place, clips=0)
                )
        finally:
            await blocker.close()

    before = loop.run(organization_counts(migrated, place.organization_id))
    result = loop.run(held_write())
    assert isinstance(result, LedgerRejection)
    assert result.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT
    assert loop.run(organization_counts(migrated, place.organization_id)) == before
    again = loop.run(environment.writer.write(context, ORDER_TYPE, order_document(place, clips=0)))
    assert isinstance(again, Receipt)
    assert loop.run(verify_ledger_chains(migrated, place.organization_id)) == {place.plant_id: 2}


def test_events_not_declared_by_the_type_are_refused_before_the_transaction(
    environment: WriterEnvironment,
) -> None:
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    opened = environment.database.probe.opened
    stray = NewEvent(event_name=SOLO_EVENT, payload=probe_payload())
    with pytest.raises(ValueError, match="outbox_events"):
        environment.loop.run(
            environment.writer.write(
                context, ORDER_TYPE, order_document(place, clips=0), events=(stray,)
            )
        )
    assert environment.database.probe.opened == opened


def test_storage_is_consulted_once_per_reference_before_the_transaction(
    environment: WriterEnvironment,
) -> None:
    place = Place.new()
    document = order_document(place, clips=3)
    for clip in document["clips"]:
        environment.storage.put(clip)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    calls = environment.storage.calls
    receipt = environment.loop.run(environment.writer.write(context, ORDER_TYPE, document))
    assert isinstance(receipt, Receipt)
    assert environment.storage.calls == calls + 3
