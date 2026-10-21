"""FS-LAZ-06: mutación, alarma y lectura concurrente durante la reconstrucción real."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import loop_world
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES, mutate_projection, rebuilder
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.loop.domain.enums import InboxPartitionNotice
from vigia_platform.loop.projection.verify import ProjectionVerifier

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
def test_corruption_rebuilds_without_a_mixed_snapshot(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "FS-LAZ-06",
            title="Proyección divergente",
            injection="Mutar filas y digest",
            expected="Alarma a la primera, rebuilding y ninguna lectura mixta",
        ) as run,
        loop_world(postgres_endpoint, "fs_laz_06") as world,
    ):
        org = world.a
        consumer, ports = world.projection_consumer()
        subjects = [world.ingest_finding(org) for _ in range(100 if nightly else 3)]
        for subject in subjects:
            assert world.deliver(consumer, world.outbox_event(subject)) == 1
        pairs = [mutate_projection(world, subject) for subject in subjects]
        old, new = {a for a, _ in pairs}, {b for _, b in pairs}
        database = world.stack.primary.database
        context = world.context(org)
        samples: list[set[str]] = []

        async def exercise() -> None:
            staged, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
            inbox = InboxService(database=database, authorizer=world.stack.authz.authorizer)

            async def phase() -> None:
                staged.set()
                await asyncio.wait_for(release.wait(), 60)

            rebuilding = rebuilder(world, ports, on_swap_started=phase)
            verifier = ProjectionVerifier(
                database=database,
                rebuilder=rebuilding,
                outbox=world.stack.outbox,
                clock=world.stack.authz.sessions.clock,
                random_bytes=run.random.randbytes,
            )

            async def sample() -> None:
                while not stop.is_set():
                    # Una sentencia, otra conexión: el snapshot no puede mezclar generaciones.
                    rows = await database.read(
                        context,
                        text(
                            "SELECT projection_digest FROM loop.finding_state"
                            " WHERE organization_id = :org AND plant_id = :plant"
                        ),
                        {"org": org.organization_id, "plant": org.plant_id},
                    )
                    assert len(rows) == len(subjects)
                    values = {str(row.projection_digest) for row in rows}
                    samples.append(values)
                    assert values <= old or values <= new, values
                    page = await inbox.page(context)
                    assert (
                        not page.items or len({item.evidence_flagged for item in page.items}) == 1
                    )

            reader = asyncio.create_task(sample())
            task = asyncio.create_task(verifier.verify_plant(context, org.plant_id))
            try:
                await asyncio.wait_for(staged.wait(), 60)
                page = await inbox.page(context)
                assert any(
                    p.notice is InboxPartitionNotice.PROJECTION_REBUILDING for p in page.partitions
                )
                alarms = await database.read(
                    context,
                    text(
                        "SELECT event_id FROM shared.outbox_event WHERE organization_id = :org"
                        " AND event_name = 'loop_projection_divergence'"
                    ),
                    {"org": org.organization_id},
                )
                assert len(alarms) == 1
                release.set()
                result = await asyncio.wait_for(task, 120)
                assert result.is_diverged and result.divergence_count == 1
            finally:
                release.set()
                stop.set()
                await asyncio.gather(reader, task)

        world.stack.run(exercise())
        assert samples
        assert {world.finding_state(s)["projection_digest"] for s in subjects} == new
        assert world.checkpoint(org)["state"] == "healthy"
        run.observe(
            rows=len(subjects), snapshots=len(samples), alarm_count=1, final_state="healthy"
        )
