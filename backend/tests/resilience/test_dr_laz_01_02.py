"""DR-LAZ-01/02: copia física atrasada (regresión de VIG-273) y reconstrucción anual (TASK-329).

Las tres primeras pruebas son la regresión de VIG-273 sobre la declaración de la partición
restaurada (``InboxPartitionNotice.PROJECTION_DIVERGED``): cubren la marca compartida que no
certifica la ingesta, la copia física en otro PostgreSQL 16 y el oráculo incremental independiente.
Las dos últimas son los ensayos ``DR-LAZ-01`` y ``DR-LAZ-02`` del arnés de resiliencia de TASK-329:
dejan su informe con semilla (90 días) y ``DR-LAZ-02`` mide el tiempo frente a 1800 s solo como
tendencia (A-65).
"""

from __future__ import annotations

import io
import tarfile
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text

from tests.identity_db import MigratedDatabase
from tests.integration.conftest import POSTGRES_USER, PostgresEndpoint
from tests.laz_support import IngestedSubject, InstrumentedPorts, LoopWorld, loop_world
from tests.node_api_db import DbNode, issue
from tests.resilience.harness import WALL, dedicated_postgres, restored_postgres, scenario
from tests.resilience.laz_support import PROFILES, rebuilder
from tests.resilience.stack import laz_stack
from tests.resilience.test_fs_nuc_10 import BACKUP, PGDATA, _read_container_file
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.application.inbox import InboxService, PartitionDeclaration
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import InboxPartitionNotice
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode

pytestmark = pytest.mark.integration


def _cross_gap(world: LoopWorld, first: IngestedSubject, path: str) -> set[uuid.UUID]:
    """Avanza la marca compartida por vías reales, nunca con un UPDATE de la fixture."""
    if path == "review_queue":
        detection = world.ingest_detection(first.organization)
        consumer, _ = world.review_queue_consumer()
        assert world.deliver_review_queue(consumer, world.review_queue_event(detection))
        return {detection.record_id}
    if path == "write":
        _, applied = world.write(
            first.organization, "classification", world.classification_content(first)
        )
        assert applied is not None and applied.applied
    if path == "later_finding":
        later = world.ingest_finding(first.organization)
        consumer, _ = world.projection_consumer()
        world.deliver(consumer, world.outbox_event(later))
        return {later.record_id}
    return set()


@pytest.mark.parametrize("path", ["review_queue", "write", "later_finding"])
def test_shared_checkpoint_cannot_hide_an_ingestion_gap(
    postgres_endpoint: PostgresEndpoint, path: str
) -> None:
    """Un consumidor distinto, U-04 o una entrega fuera de orden no certifican el hueco."""
    with loop_world(postgres_endpoint, "crossed_gap") as world:
        org = world.a
        consumer, _ = world.projection_consumer()
        first = world.ingest_finding(org)
        world.deliver(consumer, world.outbox_event(first))
        lost = world.ingest_finding(org)
        extra = _cross_gap(world, first, path)
        assert (
            world.checkpoint(org)["last_applied_ledger_sequence"]
            > world.outbox_event(lost).ledger_sequence
        )
        service = InboxService(
            database=world.stack.primary.database, authorizer=world.stack.authz.authorizer
        )
        page = world.stack.run(service.page(world.context(org)))
        assert page.partitions and not page.items, "La marca compartida no certifica la ingesta"
        world.deliver(consumer, world.outbox_event(lost))
        recovered = world.stack.run(service.page(world.context(org)))
        assert not recovered.partitions
        assert {item.finding_state_id for item in recovered.items} == {
            first.record_id,
            lost.record_id,
            *extra,
        }


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


@pytest.mark.parametrize("path", ["none", "review_queue", "write"])
def test_restored_stale_projection_is_not_served(path: str) -> None:
    """pg_basebackup y otro PostgreSQL 16, sin worker ni UPDATE de salud desde la fixture."""
    with dedicated_postgres() as (endpoint, source), loop_world(endpoint, "dr_laz_01") as world:
        org = world.a
        consumer, _ = world.projection_consumer()
        first = world.ingest_finding(org)
        world.deliver(consumer, world.outbox_event(first))
        lost = world.ingest_finding(org)
        extra = _cross_gap(world, first, path)
        checkpoint = world.checkpoint(org)
        assert checkpoint is not None and checkpoint["state"] == "healthy"
        if path == "none":
            assert (
                checkpoint["last_applied_ledger_sequence"]
                < world.outbox_event(lost).ledger_sequence
            )
        else:
            assert (
                checkpoint["last_applied_ledger_sequence"]
                > world.outbox_event(lost).ledger_sequence
            )

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
                    assert rebuilt.findings_written == 2 + len(extra)
                    assert rebuilt.projection_digest == expected_digest
                    after = await service.page(context)
                    assert len(after.items) == 2 + len(extra)
                    assert not after.partitions
                    assert {item.finding_state_id for item in after.items} == {
                        first.record_id,
                        lost.record_id,
                        *extra,
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


@pytest.mark.parametrize("nightly", PROFILES)
def test_scenario_restored_stale_projection_is_not_served(nightly: bool) -> None:
    """Ensayo ``DR-LAZ-01``: la copia física atrasada se declara y no se sirve hasta reconstruir."""
    with (
        scenario(
            "DR-LAZ-01",
            title="Restauración con proyección atrasada",
            injection="pg_basebackup con un finding_received aún no aplicado",
            expected="La partición restaurada se declara indisponible hasta reconstruir",
        ) as run,
        laz_stack("dr_laz_01") as (world, container),
    ):
        org = world.a
        consumer, _ = world.projection_consumer()
        first = world.ingest_finding(org)
        world.deliver(consumer, world.outbox_event(first))
        lost = world.ingest_finding(org)
        assert world.finding_state(lost) is None
        code, output = container.exec(
            [
                "pg_basebackup",
                "-D",
                BACKUP,
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
        base = _read_container_file(container, f"{BACKUP}/base.tar")
        # La copia se acaba de tomar: está dentro de la ventana de recuperación de siete días.
        with restored_postgres([(PGDATA, base)], command=["postgres"]) as (restored, _):
            source = world.stack.authz.sessions.migrated
            migrated = MigratedDatabase(
                restored, source.database, source.app_password, source.migrate_password
            )
            database = Database.create(
                DatabaseSettings(
                    url=migrated.as_role("vigia_app").sqlalchemy_url,
                    process=ProcessKind.WORKER,
                    sslmode=SslMode.DISABLE,
                    lock_timeout_ms=60_000,
                )
            )
            try:
                service = InboxService(database=database, authorizer=world.stack.authz.authorizer)
                page = world.stack.run(service.page(world.context(org)))
                run.observe(
                    items_before_rebuild=len(page.items),
                    partitions=page.partitions,
                    snapshot_age_seconds=0,
                    recovery_window_seconds=7 * 86400,
                )
                assert page.partitions, "Una copia con checkpoint atrasado debe declararse"
                assert not page.items, "La planta atrasada no se sirve hasta reconstruir"
                _, ports = world.projection_consumer(database=database)
                rebuilt = world.stack.run(
                    rebuilder(world, ports, database=database).rebuild_plant(
                        world.context(org), org.plant_id
                    )
                )
                assert rebuilt.findings_written == 2
                after = world.stack.run(service.page(world.context(org)))
                assert not after.partitions
                assert len(after.items) == 2
                run.observe(items_after_rebuild=len(after.items), reconstructed=True)
            finally:
                world.stack.run(database.dispose())


@pytest.mark.parametrize("nightly", PROFILES)
def test_annual_rebuild_matches_incremental(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "DR-LAZ-02",
            title="Reconstrucción anual",
            injection="Reconstrucción completa por planta",
            expected="Estado idéntico; tiempo frente a 1800 s solo como tendencia",
        ) as run,
        loop_world(postgres_endpoint, "dr_laz_02") as world,
    ):
        org = world.a
        consumer, ports = world.projection_consumer()
        # Año de tiempo inyectado, acotado a una muestra mensual en ci y diaria en nightly.
        count = 365 if nightly else 13
        subjects = []
        for index in range(count):
            if index:
                world.stack.tick(86400 * 364 / (count - 1))
            if index == count // 2:
                # Renovación sintética válida; no se desactiva la verificación de certificados.
                certificate, _ = world.stack.run(
                    issue(
                        world.stack.authz.sessions.admin,
                        world.stack.authority,
                        DbNode(
                            org.site.node_id,
                            org.organization_id,
                            org.plant_id,
                            org.zone_id,
                            world.stack.authz.operator_id,
                        ),
                        world.stack.now(),
                    )
                )
                org = replace(org, site=replace(org.site, certificate=certificate))
            subject = world.ingest_finding(org)
            assert world.deliver(consumer, world.outbox_event(subject)) == 1
            subjects.append(subject)
        before = {s.subject_id: world.finding_state(s)["projection_digest"] for s in subjects}
        started = WALL.monotonic()
        result = world.stack.run(
            rebuilder(world, ports).rebuild_plant(world.context(org), org.plant_id)
        )
        elapsed = WALL.monotonic() - started
        after = {s.subject_id: world.finding_state(s)["projection_digest"] for s in subjects}
        run.observe(
            rows=count,
            generated_calendar_days=365,
            rebuild_seconds=elapsed,
            target_seconds=1800,
            within_target=elapsed < 1800,
            timing_is_trend=True,
        )
        assert after == before
        assert result.findings_written == count
        again = world.stack.run(
            rebuilder(world, ports).rebuild_plant(world.context(org), org.plant_id)
        )
        assert again.projection_digest == result.projection_digest
