"""Frontera contigua e índice de lectura: VIG-273, DR-LAZ-01 y PR-LAZ-57.

Reproducción: uv run pytest -q --hypothesis-profile=ci
    tests/integration/test_loop_ingestion_frontier.py
Volumen: el mismo archivo con --hypothesis-profile=nightly -s (un millón de eventos sintéticos).
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from typing import Any, Final

import pytest
from sqlalchemy import text

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, loop_world
from tests.loop_inbox_support import inbox_stack
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.loop.adapters.postgres.inbox_query import inbox_statement
from vigia_platform.loop.application.inbox import InboxService

pytestmark = pytest.mark.integration

_EXPLAIN_SQL: Final = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + inbox_statement(
    newest_first=True
)
_EXPLAIN: Final = text(_EXPLAIN_SQL)


def test_certification_ignores_ingestion_from_another_plant(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    with loop_world(postgres_endpoint, "frontier_plant") as world:
        org = world.a
        subject = world.ingest_finding(org)
        other_plant = world.add_plant(org)
        event = world.outbox_event(subject)
        world.stack.execute(
            "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
            " ledger_sequence, payload, correlation_id, created_at)"
            " SELECT $1, organization_id, $2, event_name, ledger_sequence,"
            " jsonb_set(payload, '{platform_record_id}', to_jsonb($3::text)),"
            " correlation_id, created_at FROM shared.outbox_event WHERE event_id = $4",
            uuid.uuid4(),
            other_plant,
            str(uuid.uuid4()),
            event.event_id,
        )
        consumer, _ = world.projection_consumer()
        world.deliver(consumer, event)
        checkpoint = world.checkpoint(org)
        assert checkpoint["ingestion_frontier"] == event.ledger_sequence


def test_pending_detection_is_not_hidden_by_a_finding(postgres_endpoint: PostgresEndpoint) -> None:
    with loop_world(postgres_endpoint, "detection_gap") as world:
        org = world.a
        detection = world.ingest_detection(org)
        later = world.ingest_finding(org)
        consumer, _ = world.projection_consumer()
        world.deliver(consumer, world.outbox_event(later))
        checkpoint = world.checkpoint(org)
        sequence = world.review_queue_event(detection).ledger_sequence
        assert checkpoint["ingestion_frontier"] < sequence
        assert checkpoint["last_applied_ledger_sequence"] > sequence
        service = InboxService(
            database=world.stack.primary.database, authorizer=world.stack.authz.authorizer
        )
        page = world.stack.run(service.page(world.context(org)))
        assert page.partitions and not page.items
        review_queue, _ = world.review_queue_consumer()
        world.deliver_review_queue(review_queue, world.review_queue_event(detection))
        after = world.stack.run(service.page(world.context(org)))
        assert not after.partitions and len(after.items) == 2


def test_a_late_segment_must_be_certified_too(postgres_endpoint: PostgresEndpoint) -> None:
    with loop_world(postgres_endpoint, "segment_gap") as world:
        org = world.a
        first = world.ingest_finding(org)
        consumer, _ = world.projection_consumer()
        world.deliver(consumer, world.outbox_event(first))
        document = copy.deepcopy(dict(world.stack.finding(org.site)))
        document["episode"] = {
            **dict(first.document["episode"]),
            "segment_index": 1,
            "continues": False,
            "previous_segment_record_id": str(first.record_id),
        }
        second = world._ingest(org, IngestKind.FINDING, document)
        later = world.ingest_detection(org)
        review_queue, _ = world.review_queue_consumer()
        world.deliver_review_queue(review_queue, world.review_queue_event(later))
        assert (
            world.checkpoint(org)["ingestion_frontier"] < world.outbox_event(second).ledger_sequence
        )
        world.deliver(consumer, world.outbox_event(second))
        checkpoint = world.checkpoint(org)
        assert checkpoint["ingestion_frontier"] >= world.review_queue_event(later).ledger_sequence
        assert checkpoint["ingestion_frontier"] <= checkpoint["last_applied_ledger_sequence"]
        assert world.episode(first)["segment_count"] == 2
        service = InboxService(
            database=world.stack.primary.database, authorizer=world.stack.authz.authorizer
        )
        page = world.stack.run(service.page(world.context(org)))
        assert not page.partitions and len(page.items) == 2


def test_concurrent_consumers_preserve_a_gap_and_rollback_the_frontier(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    """Conexiones distintas, bajo el candado de planta existente; también se revierte el avance."""
    with loop_world(postgres_endpoint, "concurrent_frontier") as world:
        org = world.a
        lost = world.ingest_finding(org)
        finding = world.ingest_finding(org)
        detection = world.ingest_detection(org)
        database = world.stack.primary.database
        context = world.event_context(org)

        async def apply(subject: IngestedSubject) -> None:
            async with database.transaction(context) as tx:
                item = await world.store.record_input(tx, subject.record_id)
                await world.store.apply_in_transaction(tx, item)

        async def run() -> None:
            await asyncio.gather(apply(finding), apply(detection))
            async with database.transaction(context) as tx:
                row = (
                    await tx.execute(
                        text("SELECT ingestion_frontier FROM loop.loop_projection_checkpoint")
                    )
                ).one()
                assert row.ingestion_frontier == 0
            with pytest.raises(RuntimeError, match="reversión sintética"):
                async with database.transaction(context) as tx:
                    item = await world.store.record_input(tx, lost.record_id)
                    await world.store.apply_in_transaction(tx, item)
                    raise RuntimeError("reversión sintética")

        world.stack.run(run())
        assert world.checkpoint(org)["ingestion_frontier"] == 0
        world.project(lost)
        checkpoint = world.checkpoint(org)
        assert checkpoint["ingestion_frontier"] == checkpoint["last_applied_ledger_sequence"]


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [plan, *(node for child in plan.get("Plans", []) for node in _nodes(child))]


@pytest.mark.nightly
def test_read_health_is_an_indexed_probe_with_a_million_ingestions(
    postgres_endpoint: PostgresEndpoint,
) -> None:
    """Banco de planes, NO prueba de restauración: la frontera histórica se modela en la fixture.

    Un millón de eventos de ingesta de la misma organización y dos plantas consultadas;
    todos detrás de la frontera. Sin desactivar seqscan ni fijar un plan ni un tope de pared.
    """
    with inbox_stack(postgres_endpoint, "frontier_volume") as stack:
        world = stack.world
        org = world.a
        finding = stack.finding(org)
        other_plant = world.add_plant(org)
        stack.zone_in(org, other_plant)
        stack.execute(
            "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
            " ledger_sequence, payload, correlation_id, created_at)"
            " SELECT md5('vig273-' || n::text)::uuid, e.organization_id,"
            " CASE WHEN n % 2 = 0 THEN e.plant_id ELSE $1::uuid END, e.event_name,"
            " n, e.payload, e.correlation_id, e.created_at"
            " FROM shared.outbox_event e CROSS JOIN generate_series(1, 1000000) n"
            " WHERE e.event_id = $2",
            other_plant,
            world.outbox_event(finding).event_id,
        )
        # Solo modelado del prefijo histórico para medir el plan caliente. Las regresiones DR
        # usan exclusivamente ingesta, consumidores, reconstrucción y copia física reales.
        stack.execute(
            "INSERT INTO loop.loop_projection_checkpoint"
            " (organization_id, plant_id, last_applied_ledger_sequence, ingestion_frontier)"
            " VALUES ($1, $2, 1000000, 1000000)"
            " ON CONFLICT (organization_id, plant_id) DO UPDATE SET"
            " last_applied_ledger_sequence = 1000000, ingestion_frontier = 1000000",
            org.organization_id,
            other_plant,
        )
        stack.execute(
            "UPDATE loop.loop_projection_checkpoint SET last_applied_ledger_sequence = 1000000,"
            " ingestion_frontier = 1000000 WHERE organization_id = $1 AND plant_id = $2",
            org.organization_id,
            org.plant_id,
        )
        # El contenedor de CI tiene /dev/shm de 64 MiB: no lanzar trabajadores de VACUUM.
        stack.execute("VACUUM (ANALYZE, PARALLEL 0) shared.outbox_event")

        async def explain() -> Any:
            async with stack.database.transaction(world.context(org)) as tx:
                assert (await tx.execute(text("SELECT current_user"))).scalar_one() == "vigia_app"
                return (
                    await tx.execute(
                        _EXPLAIN,
                        {
                            "organization_id": org.organization_id,
                            "organization_wide": True,
                            "scope_plant_ids": [],
                            "scope_zone_ids": [],
                            "zone_ids": None,
                            "lifecycle_states": None,
                            "family": None,
                            "tier": None,
                            "period_from": None,
                            "period_to": None,
                            "after_bucket": None,
                            "after_id": None,
                            "limit": 201,
                        },
                    )
                ).scalar_one()

        (plan,) = stack.run(explain())
        print("EXPLAIN ANALYZE (1000000 ingestas, dos plantas, vigia_app): " + json.dumps(plan))
        nodes = _nodes(plan["Plan"])
        probes = [n for n in nodes if n.get("Index Name") == "outbox_event_loop_ingestion"]
        assert probes, json.dumps(plan)
        assert all(n["Node Type"] in {"Index Only Scan", "Index Scan"} for n in probes)
        assert all(n["Actual Rows"] <= 1 and n["Actual Loops"] == 2 for n in probes)
        assert all(n["Scan Direction"] == "Backward" for n in probes)
        assert all(n.get("Rows Removed by Filter", 0) == 0 for n in probes)
        assert not any(
            n.get("Relation Name") == "outbox_event" and n["Node Type"] == "Seq Scan" for n in nodes
        )
