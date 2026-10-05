"""Garantías de «una sola vez» de la ingesta bajo concurrencia real (TASK-221; AGENTS.md, retro 13).

Contra PostgreSQL 16 real como ``vigia_app``, con **dos instancias** que no comparten nada en
memoria (salvo el almacén de metadatos de los clips):

- **10 envíos simultáneos del mismo hallazgo** (y de la misma detección y del mismo cierre
  huérfano):
  exactamente un registro, un evento, una marca de cierre huérfano y una transición de la concesión,
  y nueve ``accepted_duplicate`` con el **mismo** ``Receipt``; la cadena de la planta queda íntegra.
  Una barrera retiene la lectura del paso 5 hasta que llegan los diez, así que los diez pasan la
  comprobación previa y la carrera se resuelve **siempre** en la escritura (unicidad de la clave,
  ``UPDATE`` condicional de la concesión y ``ON CONFLICT DO NOTHING`` del cierre huérfano);
- **orden de candados** entre operaciones distintas: un ``ingest_rejected`` (cadena de la planta y
  después auditoría) y la secuencia de las operaciones de identidad del nodo en la misma planta
  (registro de la planta, retención, auditoría) terminan las dos;
- una aceptación y ``mark_orphan_clips`` sobre las mismas concesiones terminan las dos, con un
  resultado coherente.

Topes: la base 60 s (``LOCK_TIMEOUT_MS``), la barrera y la llegada 60 s (eventos, nunca topes de
«llegó a tiempo»). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from vigia_contracts.models import api

from tests.fleet_ingest_support import IngestInstance, IngestSite, IngestStack, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import verify_audit_chain, verify_ledger_chains
from vigia_platform.fleet.application.orphan_clips import OrphanClipSweeper
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import Receipt, RecordScope
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

SUBMISSIONS: Final = 10
HOUR: Final = dt.timedelta(hours=1)
BARRIER_SECONDS: Final = 60.0
HOLD_SECONDS: Final = 3.0
"""Cuánto retiene la primera operación su transacción: el tiempo para que la segunda arranque y
tome lo que pueda. No es un tope de «llegó a tiempo»: con el orden bueno el resultado no depende."""
FINDING, DETECTION, EVENT = (
    IngestKind.FINDING,
    IngestKind.DETECTION_FOR_REVIEW,
    IngestKind.OBSERVABILITY_EVENT,
)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[IngestStack]:
    with ingest_stack(postgres_endpoint, "fleet_ingest_concurrency") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: IngestStack) -> Iterator[None]:
    yield
    stack.run(stack.release())


class Barrier:
    """Retiene las ``count`` primeras lecturas del paso 5 hasta que llegan todas."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.arrived = 0
        self.all_here = asyncio.Event()

    def wrap(self, instance: IngestInstance) -> None:
        store: Any = instance.service._deps.store
        original = store.accepted

        async def accepted(*args: Any, **kwargs: Any) -> Any:
            found = await original(*args, **kwargs)
            if self.arrived < self.count:
                self.arrived += 1
                if self.arrived == self.count:
                    self.all_here.set()
                await asyncio.wait_for(self.all_here.wait(), BARRIER_SECONDS)
            return found

        store.accepted = accepted


def _simultaneous(
    stack: IngestStack, site: IngestSite, kind: IngestKind, document: dict[str, Any]
) -> list[Any]:
    other = stack.instance()
    barrier = Barrier(SUBMISSIONS)
    barrier.wrap(stack.primary)
    barrier.wrap(other)
    instances = [stack.primary if index % 2 == 0 else other for index in range(SUBMISSIONS)]

    async def run() -> list[Any]:
        return list(
            await asyncio.gather(
                *(stack.send(site, kind, document, instance) for instance in instances)
            )
        )

    try:
        return stack.run(run())
    finally:
        del stack.primary.service._deps.store.accepted  # type: ignore[attr-defined]


def _one_accepted_and_nine_duplicates(responses: list[Any]) -> Any:
    assert [response.status_code for response in responses] == [200] * SUBMISSIONS, [
        response.text for response in responses
    ]
    receipts = [api.parse_receipt(response.content) for response in responses]
    statuses = sorted(receipt.status.value for receipt in receipts)
    assert statuses == ["accepted"] + ["accepted_duplicate"] * (SUBMISSIONS - 1)
    assert {(r.platform_record_id, r.received_at) for r in receipts} == {
        (receipts[0].platform_record_id, receipts[0].received_at)
    }
    return receipts[0]


@pytest.mark.parametrize("kind", [FINDING, DETECTION])
def test_ten_simultaneous_submissions_from_two_instances_write_one_record(
    stack: IngestStack, kind: IngestKind
) -> None:
    site = stack.site()
    document = stack.finding(site) if kind is FINDING else stack.detection(site)
    clip = document["cameras"][0]["clips"][0]
    receipt = _one_accepted_and_nine_duplicates(_simultaneous(stack, site, kind, document))
    (record,) = stack.records(kind.record_type, site.organization_id)
    assert str(record["record_id"]) == receipt.platform_record_id
    assert len(stack.events(kind.event_name, site.organization_id)) == 1
    assert len(stack.evidence(record["record_id"])) == 1
    status, used_at = stack.grant_status(clip)
    assert status == "used" and used_at is not None
    assert stack.audit(site.organization_id) == []
    lengths = stack.run(verify_ledger_chains(stack.authz.sessions.migrated, site.organization_id))
    assert lengths == {site.plant_id: 1}


def test_ten_simultaneous_orphan_closes_write_one_record_and_one_mark(stack: IngestStack) -> None:
    site = stack.site()
    document = stack.event(site, opened_event_id=str(stack.uuid7()))
    receipt = _one_accepted_and_nine_duplicates(_simultaneous(stack, site, EVENT, document))
    (record,) = stack.records(EVENT.record_type, site.organization_id)
    assert str(record["record_id"]) == receipt.platform_record_id
    (mark,) = stack.orphan_closes(site.organization_id)
    assert str(mark["ledger_record_id"]) == receipt.platform_record_id
    assert len(stack.events(EVENT.event_name, site.organization_id)) == 1
    stack.run(verify_ledger_chains(stack.authz.sessions.migrated, site.organization_id))


def test_ten_different_findings_at_once_keep_the_plant_chain_whole(stack: IngestStack) -> None:
    site = stack.site()
    other = stack.instance()
    documents = [stack.finding(site) for _ in range(SUBMISSIONS)]

    async def run() -> list[Any]:
        return list(
            await asyncio.gather(
                *(
                    stack.send(site, FINDING, document, stack.primary if i % 2 else other)
                    for i, document in enumerate(documents)
                )
            )
        )

    responses = stack.run(run())
    assert [response.status_code for response in responses] == [200] * SUBMISSIONS
    lengths = stack.run(verify_ledger_chains(stack.authz.sessions.migrated, site.organization_id))
    assert lengths == {site.plant_id: SUBMISSIONS}


# --- Orden de candados entre operaciones ---------------------------------------------------------


def test_a_gate_rejection_and_a_plant_then_audit_operation_both_finish(stack: IngestStack) -> None:
    """``ingest_rejected`` toma la cadena de la planta y después la de auditoría, como las
    operaciones de identidad del nodo (``fleet.application.common``): la segunda espera a la
    primera y las dos terminan. Con el orden invertido (mutación del PR), bloqueo mutuo."""
    site = stack.site(usage_approved=False)
    scope = stack.run(_node_scope(stack, site))
    arrived = asyncio.Event()

    async def plant_then_audit() -> None:
        """La secuencia de una operación de identidad del nodo: registro y después auditoría."""
        instance = stack.primary
        context = scope.context
        async with instance.database.transaction(context) as transaction:
            written = await instance.writer.write(
                context,
                "node_communication_state_changed",
                {
                    "node_id": str(site.node_id),
                    "state": "reachable",
                    "since": since,
                },
                scope=RecordScope(plant_id=site.plant_id),
                transaction=transaction,
            )
            assert isinstance(written, Receipt)
            arrived.set()
            await asyncio.sleep(HOLD_SECONDS)
            await stack.authz.sessions.audit.append(
                context,
                AuditOperation.NODE_REVOKED,
                outcome=AuditOutcome.SUCCESS,
                plant_id=site.plant_id,
                resource=ResourceRef("node", site.node_id),
                transaction=transaction,
            )

    document = stack.finding(site)
    since = format_timestamp(stack.now())

    async def rejection() -> Any:
        await asyncio.wait_for(arrived.wait(), BARRIER_SECONDS)
        return await stack.send(site, FINDING, document)

    async def both() -> tuple[None, Any]:
        return await asyncio.gather(plant_then_audit(), rejection())

    _, response = stack.run(both())
    assert response.status_code == 403, response.text
    assert api.parse_rejection_response(response.content).code.value == "zone_gate_not_approved"
    assert len(stack.records("ingest_rejected", site.organization_id)) == 1
    assert len(stack.audit(site.organization_id)) == 1
    migrated = stack.authz.sessions.migrated
    stack.run(verify_ledger_chains(migrated, site.organization_id))
    stack.run(verify_audit_chain(migrated, site.organization_id))


async def _node_scope(stack: IngestStack, site: IngestSite) -> Any:
    from vigia_platform.identity.authz.context import PresentedNode
    from vigia_platform.node_api.certificate_profile import serial_hex

    presented = PresentedNode(
        node_id=site.node_id,
        organization_id=site.organization_id,
        plant_id=site.plant_id,
        certificate_serial=serial_hex(site.certificate.serial_number),
    )
    return await stack.primary.gate.identity.resolve(presented, stack.uuid7())


class _Objects:
    """``ClipObjectStore.heads`` sobre el almacén de metadatos de la pila."""

    def __init__(self, stack: IngestStack) -> None:
        self._stack = stack

    async def heads(self, keys: list[str]) -> dict[str, Any]:
        return {key: await self._stack.storage.head_object(key) for key in keys}


def test_an_acceptance_and_the_orphan_sweep_on_the_same_grants_both_finish(
    stack: IngestStack,
) -> None:
    """La aceptación bloquea las concesiones que cita (por ``clip_id``) y ``mark_orphan_clips``
    solo pasa a ``orphan`` las que siguen ``issued``: termine primero quien termine, la concesión
    acaba ``used`` u ``orphan`` una sola vez y el registro se acepta."""
    site = stack.site()
    document = stack.finding(site, grant=False)
    clip = document["cameras"][0]["clips"][0]
    stack.grant(site, clip, stack.now() - 25 * HOUR)  # emitida hace más de 24 h
    sweeper = OrphanClipSweeper(
        database=stack.primary.database,
        store=_Objects(stack),  # type: ignore[arg-type]
        clock=stack.authz.sessions.clock,
    )
    context = stack.run(_node_scope(stack, site)).context

    async def sweep() -> Any:
        async with stack.primary.database.transaction(context) as transaction:
            return await sweeper.sweep(transaction)

    async def both() -> tuple[Any, Any]:
        return await asyncio.gather(stack.send(site, FINDING, document), sweep())

    response, report = stack.run(both())
    assert response.status_code == 200, response.text
    status, _ = stack.grant_status(clip)
    assert status in {"used", "orphan"}
    assert (status == "orphan") == (uuid.UUID(clip["clip_id"]) in report.orphaned)
    assert len(stack.records(FINDING.record_type, site.organization_id)) == 1
