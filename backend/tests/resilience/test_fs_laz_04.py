"""FS-LAZ-04: eventos perdidos, divergencia declarada y corrección desde el expediente.

Regresión de VIG-269 (PR-LAZ-44): --hypothesis-profile=ci --hypothesis-seed=1434046402.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import loop_world
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES, SelectiveProjectionDelivery, rebuilder
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.domain.enums import InboxPartitionNotice, InboxZoneNotice
from vigia_platform.loop.projection.verify import ProjectionVerifier

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
def test_lost_finding_is_detected_by_daily_verification(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "FS-LAZ-04",
            title="Eventos de U-03 perdidos",
            injection="Omitir finding_received y gate_state_changed de una zona",
            expected="Primera alarma, diverged, cobertura no disponible y reconstrucción",
        ) as run,
        loop_world(postgres_endpoint, "fs_laz_04") as world,
    ):
        org = world.a
        consumer, ports = world.projection_consumer()
        existing = world.ingest_finding(org)
        assert world.deliver(consumer, world.outbox_event(existing)) == 1
        lost = world.ingest_finding(org)
        delivery = SelectiveProjectionDelivery(
            world, consumer, frozenset({"finding_received", "gate_state_changed"})
        )
        assert delivery.deliver(world.outbox_event(lost)) == 0
        assert delivery.deliver(world.gate_event(org, "revoked")) == 0
        assert world.finding_state(lost) is None and world.record_exists(lost.record_id)
        assert not world.finding_state(existing)["zone_agreement_revoked"]
        # Un nodo alcanzable no afirma cobertura; la bandeja debe declararla no disponible.
        world.stack.execute(
            "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id,"
            " software_version, contract_version, model_version, contract_notice,"
            " communication_state, local_queue, clock, signal_reader, uptime_seconds, updated_at)"
            " VALUES ($1, $2, $3, '1.1.0', '1.1.0', 'model-1', '{\"result\":\"accepted\"}',"
            " 'reachable', '{\"pending\":0}', '{\"synchronized\":true,\"offset_ms\":3}',"
            ' \'{"available":true,"adapter":"modbus"}\', 120, $4)',
            org.site.node_id,
            org.organization_id,
            org.plant_id,
            world.stack.now(),
        )
        database = world.stack.primary.database
        context = world.context(org)

        async def exercise() -> Any:
            diverged, resume = asyncio.Event(), asyncio.Event()
            real = rebuilder(world, ports)

            class Held:
                enrichment = real.enrichment

                async def rebuild_plant(self, *args: Any) -> Any:
                    # El verificador ya confirmó diverged y su alarma; aún no reclama rebuilding.
                    diverged.set()
                    await asyncio.wait_for(resume.wait(), 60)
                    return await real.rebuild_plant(*args)

            verifier = ProjectionVerifier(
                database=database,
                rebuilder=Held(),
                outbox=world.stack.outbox,
                clock=world.stack.authz.sessions.clock,
                random_bytes=run.random.randbytes,
            )
            task = asyncio.create_task(verifier.verify_plant(context, org.plant_id))
            try:
                await asyncio.wait_for(diverged.wait(), 60)
                rows = await database.read(
                    context,
                    text(
                        "SELECT state FROM loop.loop_projection_checkpoint"
                        " WHERE organization_id = :org AND plant_id = :plant"
                    ),
                    {"org": org.organization_id, "plant": org.plant_id},
                )
                assert rows[0].state == "diverged"
                alarms = await database.read(
                    context,
                    text(
                        "SELECT payload FROM shared.outbox_event WHERE organization_id = :org"
                        " AND event_name = 'loop_projection_divergence'"
                    ),
                    {"org": org.organization_id},
                )
                assert len(alarms) == 1
                page = await InboxService(
                    database=database, authorizer=world.stack.authz.authorizer
                ).page(context)
                assert any(
                    p.notice is InboxPartitionNotice.PROJECTION_DIVERGED for p in page.partitions
                )
                zone = next(z for z in page.zones if z.zone_id == org.zone_id)
                assert zone.notice is InboxZoneNotice.COVERAGE_UNAVAILABLE
                run.observe(
                    state_before_rebuild=rows[0].state,
                    alarm_count=len(alarms),
                    zone_notice=zone.notice.value,
                )
            finally:
                resume.set()
                outcome = await asyncio.wait_for(task, 120)
            return outcome

        outcome = world.stack.run(exercise())
        run.observe(
            dropped_names=[e.event_name for e in delivery.dropped],
            is_diverged=outcome.is_diverged,
            rebuild_triggered=outcome.rebuild_triggered,
        )
        assert outcome.is_diverged and outcome.divergence_count == 1
        assert outcome.rebuild_triggered
        assert world.finding_state(lost) is not None
        assert world.finding_state(existing)["zone_agreement_revoked"]
        assert world.checkpoint(org)["state"] == "healthy"
