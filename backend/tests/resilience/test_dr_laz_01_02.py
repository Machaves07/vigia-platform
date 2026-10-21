"""Regresión de DR-LAZ-01: copia física atrasada, sin manipular su salud (VIG-273)."""

from __future__ import annotations

import io
import tarfile

import pytest
from sqlalchemy import text

from tests.identity_db import MigratedDatabase
from tests.integration.conftest import POSTGRES_USER, PostgresEndpoint
from tests.laz_support import InstrumentedPorts, loop_world
from tests.resilience.harness import dedicated_postgres, restored_postgres
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.application.inbox import InboxService, PartitionDeclaration
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import InboxPartitionNotice
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode

pytestmark = pytest.mark.integration


def test_checkpoint_behind_ingestion_is_not_served(postgres_endpoint: PostgresEndpoint) -> None:
    """La primera lectura no espera al verificador diario; la entrega normal recupera la lectura."""
    with loop_world(postgres_endpoint, "restore_gap") as world:
        org = world.a
        consumer, _ = world.projection_consumer()
        first = world.ingest_finding(org)
        world.deliver(consumer, world.outbox_event(first))
        lost = world.ingest_finding(org)
        assert world.record_exists(lost.record_id)
        assert world.finding_state(lost) is None
        service = InboxService(
            database=world.stack.primary.database, authorizer=world.stack.authz.authorizer
        )
        page = world.stack.run(service.page(world.context(org)))
        assert page.partitions and not page.items
        assert page.next_cursor is None
        world.deliver(consumer, world.outbox_event(lost))
        recovered = world.stack.run(service.page(world.context(org)))
        assert len(recovered.items) == 2
        assert not recovered.partitions


def test_restored_stale_projection_is_not_served() -> None:
    """pg_basebackup y otro PostgreSQL 16, sin worker ni UPDATE de salud desde la fixture."""
    with dedicated_postgres() as (endpoint, source), loop_world(endpoint, "dr_laz_01") as world:
        org = world.a
        consumer, _ = world.projection_consumer()
        first = world.ingest_finding(org)
        world.deliver(consumer, world.outbox_event(first))
        lost = world.ingest_finding(org)
        checkpoint = world.checkpoint(org)
        assert checkpoint is not None and checkpoint["state"] == "healthy"
        assert checkpoint["last_applied_ledger_sequence"] < world.outbox_event(lost).ledger_sequence

        code, output = source.exec(
            [
                "pg_basebackup",
                "-D",
                "/var/lib/postgresql/backup",
                "-Ft",
                "-X",
                "fetch",
                "--checkpoint=fast",
                "-U",
                POSTGRES_USER,
                "-h",
                "/var/run/postgresql",
            ],
            user="postgres",
        )
        assert code == 0, output
        stream, _ = source.raw.get_archive("/var/lib/postgresql/backup/base.tar")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as outer:
            (member,) = [entry for entry in outer.getmembers() if entry.isfile()]
            file = outer.extractfile(member)
            assert file is not None
            base = file.read()

        # Oráculo independiente: la vía incremental completa en el origen, DESPUÉS de copiar.
        world.deliver(consumer, world.outbox_event(lost))
        expected_digest = world.checkpoint(org)["projection_digest"]
        with restored_postgres([("/var/lib/postgresql/data", base)], command=["postgres"]) as (
            restored_endpoint,
            restored_container,
        ):
            assert restored_container.raw.id != source.raw.id
            migrated = world.stack.authz.sessions.migrated
            restored = MigratedDatabase(
                restored_endpoint,
                migrated.database,
                migrated.app_password,
                migrated.migrate_password,
            )
            database = Database.create(
                DatabaseSettings(
                    url=restored.as_role("vigia_app").sqlalchemy_url,
                    process=ProcessKind.WORKER,
                    sslmode=SslMode.DISABLE,
                    lock_timeout_ms=60_000,
                )
            )
            context = world.context(org)
            service = InboxService(database=database, authorizer=world.stack.authz.authorizer)

            async def verify() -> None:
                try:
                    async with database.transaction(context) as tx:
                        role = (await tx.execute(text("SELECT current_user"))).scalar_one()
                        assert role == "vigia_app"
                        records = (
                            await tx.execute(
                                text(
                                    "SELECT count(*) FROM ledger.ledger_record"
                                    " WHERE organization_id = :org AND record_id IN (:first, :lost)"
                                ),
                                {
                                    "org": org.organization_id,
                                    "first": first.record_id,
                                    "lost": lost.record_id,
                                },
                            )
                        ).scalar_one()
                        assert records == 2
                    page = await service.page(context)
                    assert page.partitions, "Una copia con checkpoint atrasado debe declararse"
                    assert not page.items
                    assert page.partitions == (
                        PartitionDeclaration(
                            org.plant_id, InboxPartitionNotice.PROJECTION_DIVERGED
                        ),
                    )
                    sessions = world.stack.authz.sessions
                    ports = InstrumentedPorts(wrapped=world.real_ports(database))
                    rebuilder = ProjectionRebuilder(
                        database=database,
                        catalog=ports,
                        gates=ports,
                        coverage=ports,
                        markers=EvidenceService(
                            database=database, audit=sessions.audit, storage=world.stack.storage
                        ),
                        settings=PostgresLoopConfigurationRepository(database),
                        clock=sessions.clock,
                    )
                    rebuilt = await rebuilder.rebuild_plant(context, org.plant_id)
                    assert rebuilt.findings_written == 2
                    assert rebuilt.projection_digest == expected_digest
                    after = await service.page(context)
                    assert len(after.items) == 2
                    assert not after.partitions
                    assert {item.finding_state_id for item in after.items} == {
                        first.record_id,
                        lost.record_id,
                    }
                    async with database.transaction(context) as tx:
                        state = (
                            await tx.execute(
                                text(
                                    "SELECT state FROM loop.loop_projection_checkpoint"
                                    " WHERE organization_id = :org AND plant_id = :plant"
                                ),
                                {"org": org.organization_id, "plant": org.plant_id},
                            )
                        ).scalar_one()
                        assert state == "healthy"
                finally:
                    await database.dispose()

            world.stack.run(verify())
