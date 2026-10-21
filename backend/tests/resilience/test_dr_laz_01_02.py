"""DR-LAZ-01/02: copia física desactualizada y reconstrucción anual con tiempo informativo."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.identity_db import MigratedDatabase
from tests.integration.conftest import POSTGRES_USER, PostgresEndpoint
from tests.laz_support import loop_world
from tests.node_api_db import DbNode, issue
from tests.resilience.harness import WALL, restored_postgres, scenario
from tests.resilience.laz_support import PROFILES, rebuilder
from tests.resilience.stack import laz_stack
from tests.resilience.test_fs_nuc_10 import BACKUP, PGDATA, _read_container_file
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nightly", PROFILES)
def test_restored_stale_projection_is_not_served(nightly: bool) -> None:
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
                page = world.stack.run(
                    InboxService(database=database, authorizer=world.stack.authz.authorizer).page(
                        world.context(org)
                    )
                )
                run.observe(
                    items_before_rebuild=len(page.items),
                    partitions=page.partitions,
                    snapshot_age_seconds=0,
                    recovery_window_seconds=7 * 86400,
                )
                _, ports = world.projection_consumer(database=database)
                rebuilt = world.stack.run(
                    rebuilder(world, ports, database=database).rebuild_plant(
                        world.context(org), org.plant_id
                    )
                )
                assert rebuilt.findings_written == 2
                after = world.stack.run(
                    InboxService(database=database, authorizer=world.stack.authz.authorizer).page(
                        world.context(org)
                    )
                )
                assert not after.partitions
                assert len(after.items) == 2
                run.observe(items_after_rebuild=len(after.items), reconstructed=True)
                assert page.partitions, "Una copia con checkpoint atrasado debe declararse"
                assert not page.items, "La planta atrasada no se sirve hasta reconstruir"
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
