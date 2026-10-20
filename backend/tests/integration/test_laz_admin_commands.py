"""TASK-326: las órdenes administrativas de U-04 atraviesan CLI, raíz y PostgreSQL reales."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopWorld, loop_world, register_u04
from tests.runtime_support import FakeReader, database_secret, runtime_environ
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.identity.application import admin_cli
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.coverage import CoverageService, CoverageZoneNotFound
from vigia_platform.ledger.application.evidence_read import EvidenceNotFound, EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.projection.admin import RebuildTask
from vigia_platform.loop.projection.rebuild import LEASE_DURATION
from vigia_platform.loop.worker.bulkheads import ConsumerBulkheads
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.dispatcher import Dispatcher, Outcome
from vigia_platform.shared.outbox.registries import Consumer, OutboxCatalog
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration


@dataclass(repr=False)
class AdminWorld:
    world: LoopWorld
    catalog: OutboxCatalog
    delegate: dict[str, Any]

    def cli(self, *arguments: str, operator: uuid.UUID | None = None) -> tuple[int, Any, str]:
        world = self.world
        authz = world.stack.authz
        endpoint = authz.sessions.migrated.as_role("vigia_app")
        runtime = RuntimeConfig.from_environ(
            runtime_environ(
                VIGIA_PROVIDER_ORGANIZATION_ID=str(authz.provider_organization_id),
                VIGIA_DB_STATEMENT_TIMEOUT_MS="60000",
                VIGIA_DB_POOL_TIMEOUT_SECONDS="60",
            )
        )
        reader = FakeReader(
            database_secret(
                host=endpoint.host,
                port=endpoint.port,
                dbname=endpoint.database,
                password=endpoint.password,
            )
        )

        async def build(config: Any, provider: uuid.UUID) -> Any:
            return await compose_admin_runtime(
                config, provider, runtime, clock=authz.sessions.clock, reader=reader
            )

        out, err = io.StringIO(), io.StringIO()
        code = admin_cli.run(
            [*arguments, "--operator", str(operator or authz.operator_id)],
            environ={
                "VIGIA_ENVIRONMENT": "test",
                "VIGIA_PROVIDER_ORGANIZATION_ID": str(authz.provider_organization_id),
            },
            builder=build,
            stdout=out,
            stderr=err,
            stdin=io.StringIO(),
        )
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def rebuild(
        self,
        organization: Any,
        *,
        plant: uuid.UUID | None = None,
        flags: tuple[str, ...] = ("--yes",),
        **changes: Any,
    ) -> tuple[int, Any, str]:
        return self.cli(
            "rebuild-loop-projection",
            "--organization",
            str(organization.organization_id),
            "--plant",
            str(plant or organization.plant_id),
            *flags,
            **changes,
        )


@pytest.fixture(scope="module")
def admin_world(postgres_endpoint: PostgresEndpoint) -> Iterator[AdminWorld]:
    delegate: dict[str, Any] = {"fail": False}
    catalogs = []

    async def handler(event: Any, transaction: Any) -> None:
        if delegate["fail"]:
            raise RuntimeError("fallo sintético permanente de entrega")
        await delegate["consumer"](event, transaction)

    def register(types: RecordTypeRegistry, catalog: OutboxCatalog) -> None:
        register_u04(types, catalog)
        consumer = Consumer(
            consumer_name="review_queue",
            unit=ActorUnit.U04,
            subscribed_events=("detection_for_review_received",),
            handler=handler,
        )
        catalog.consumers.register(ConsumerBulkheads({"review_queue": 2}).wrap(consumer))
        catalogs.append(catalog)

    with loop_world(postgres_endpoint, "laz_admin", register=register) as world:
        delegate["consumer"] = world.review_queue_consumer()[0]
        yield AdminWorld(world, catalogs[0], delegate)


def _records(world: LoopWorld, organization_id: uuid.UUID) -> set[uuid.UUID]:
    return {
        r["record_id"]
        for r in world.stack.fetch(
            "SELECT record_id FROM ledger.ledger_record WHERE organization_id=$1", organization_id
        )
    }


def test_rebuild_cli_repairs_only_its_plant_and_retries_without_new_records(
    admin_world: AdminWorld,
) -> None:
    world = admin_world.world
    first, other = world.organization(), world.organization()
    subjects = [world.ingest_finding(o) for o in (first, other)]
    consumer, _ = world.projection_consumer()
    for subject in subjects:
        world.deliver(consumer, world.outbox_event(subject))
        world.stack.execute(
            "UPDATE loop.finding_state SET evidence_flagged = true WHERE organization_id=$1",
            subject.organization.organization_id,
        )
    records = _records(world, first.organization_id)
    code, result, error = admin_world.rebuild(first)
    assert code == 0, error
    assert result["findings_written"] == 1
    row = world.finding_state(subjects[0])
    assert row is not None and row["evidence_flagged"] is False
    assert world.finding_state(subjects[1])["evidence_flagged"] is True
    assert world.checkpoint(first)["state"] == "healthy"
    code, repeated, error = admin_world.rebuild(first)
    assert code == 0, error
    assert repeated["projection_digest"] == result["projection_digest"]
    assert _records(world, first.organization_id) == records
    audits = world.stack.fetch(
        "SELECT filters FROM shared.audit_entry WHERE operation='loop_projection_rebuild'"
        " AND organization_id=$1",
        world.stack.authz.provider_organization_id,
    )
    assert len(audits) >= 4


def test_rebuild_cli_dry_run_and_missing_confirmation_never_write(admin_world: AdminWorld) -> None:
    world = admin_world.world
    organization = world.organization()
    before = _records(world, organization.organization_id)
    for flags, expected in ((("--dry-run",), 0), ((), 3)):
        code, result, error = admin_world.rebuild(organization, flags=flags)
        assert code == expected, error
        if result is not None:
            assert result["dry_run"] is True
        assert world.checkpoint(organization) is None
        assert _records(world, organization.organization_id) == before


def test_rebuild_cli_rejects_unknown_operator_and_cross_organization_plant(
    admin_world: AdminWorld,
) -> None:
    world = admin_world.world
    first, other = world.organization(), world.organization()
    for changes in ({"plant": other.plant_id}, {"plant": uuid.uuid4()}, {"operator": uuid.uuid4()}):
        code, _, error = admin_world.rebuild(first, **changes)
        assert code == 4, error
        assert json.loads(error)["error"] in {"not_found", "operator_invalid"}
    assert world.checkpoint(first) is None and world.checkpoint(other) is None


def test_rebuild_cli_returns_retryable_for_an_owned_lease(admin_world: AdminWorld) -> None:
    world = admin_world.world
    organization = world.organization()
    subject = world.ingest_finding(organization)
    world.project(subject)
    world.stack.execute(
        "UPDATE loop.loop_projection_checkpoint SET rebuild_owner=$1, rebuild_lease_until=$2"
        " WHERE organization_id=$3 AND plant_id=$4",
        uuid.uuid4(),
        world.stack.now() + LEASE_DURATION,
        organization.organization_id,
        organization.plant_id,
    )
    code, _, error = admin_world.rebuild(organization)
    assert code == 5 and json.loads(error)["error"] == "loop_projection_busy"


def test_replay_dead_letter_cli_reprocesses_review_queue_without_duplicate_effects(
    admin_world: AdminWorld,
) -> None:
    world = admin_world.world
    organization = world.organization()
    subject = world.ingest_detection(organization)
    event = world.review_queue_event(subject)
    # El efecto ya ocurrió antes de una caída; la entrega pendiente todavía no se confirmó.
    world.deliver_review_queue(admin_world.delegate["consumer"], event)
    before = world.stack.fetch(
        "SELECT detection_id, source_event_id FROM loop.review_queue_item WHERE organization_id=$1",
        organization.organization_id,
    )
    records = _records(world, organization.organization_id)
    dispatcher = Dispatcher(
        database=world.stack.primary.database,
        catalog=admin_world.catalog,
        outbox=world.stack.outbox,
        contexts=world.stack.authz.contexts,
        clock=world.stack.authz.sessions.clock,
    )
    admin_world.delegate["fail"] = True
    try:
        world.stack.tick(601)
        dead = 0
        for _ in range(MAX_ATTEMPTS):
            report = world.stack.run(dispatcher.dispatch_once("review_queue"))
            dead += report.count(Outcome.DEAD_LETTERED)
            world.stack.tick(661)
        assert dead >= 1, world.stack.fetch(
            "SELECT event_id, attempts, status, next_attempt_at FROM shared.outbox_delivery"
            " WHERE consumer_name='review_queue'"
        )
    finally:
        admin_world.delegate["fail"] = False
    code, result, error = admin_world.cli("replay-dead-letter", str(event.event_id), "review_queue")
    assert code == 0, error
    assert result["event_id"] == str(event.event_id)
    report = world.stack.run(dispatcher.dispatch_once("review_queue"))
    assert report.count(Outcome.DELIVERED) == 1
    after = world.stack.fetch(
        "SELECT detection_id, source_event_id FROM loop.review_queue_item WHERE organization_id=$1",
        organization.organization_id,
    )
    assert set(map(tuple, before)) == set(map(tuple, after))
    assert _records(world, organization.organization_id) == records


def test_background_read_ports_keep_the_organization_boundary(admin_world: AdminWorld) -> None:
    world = admin_world.world
    first, other = world.organization(), world.organization()
    subjects = [world.ingest_finding(o) for o in (first, other)]
    sessions = world.stack.authz.sessions
    database = world.stack.primary.database
    ports = catalog_query_ports(database)
    coverage = CoverageService(database=database, audit=sessions.audit, clock=sessions.clock)
    reader = LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock)
    evidence = EvidenceService(database=database, audit=sessions.audit, storage=world.stack.storage)
    ids = [
        uuid.UUID(
            str(
                world.stack.fetch(
                    "SELECT evidence_id FROM ledger.evidence WHERE record_id=$1 LIMIT 1",
                    s.record_id,
                )[0]["evidence_id"]
            )
        )
        for s in subjects
    ]
    contexts = world.stack.authz.contexts
    for context in (
        contexts.context_for_organization(RebuildTask(), first.organization_id),
        contexts.context_from_event(world.outbox_event(subjects[0]), unit=ActorUnit.U04),
    ):
        assert context.allowed_scopes == ()
        assert world.stack.run(reader.get(context, subjects[0].record_id)) is not None
        assert world.stack.run(reader.get(context, subjects[1].record_id)) is None
        assert world.stack.run(coverage.estado_en(context, first.zone_id, world.stack.now()))
        with pytest.raises(CoverageZoneNotFound):
            world.stack.run(coverage.estado_en(context, other.zone_id, world.stack.now()))
        assert world.stack.run(ports.catalog.single_occupancy_many(context, [first.zone_id]))
        with pytest.raises(ResourceNotFound):
            world.stack.run(ports.catalog.single_occupancy_many(context, [other.zone_id]))
        assert world.stack.run(
            ports.gates.state_at(context, first.zone_id, GateKind.USAGE, world.stack.now())
        )
        with pytest.raises(ResourceNotFound):
            world.stack.run(
                ports.gates.state_at(context, other.zone_id, GateKind.USAGE, world.stack.now())
            )
        assert set(world.stack.run(evidence.resultados_marca(context, ids))) == {ids[0]}
        with pytest.raises(EvidenceNotFound):
            world.stack.run(evidence.url_lectura(context, ids[0]))
