"""Verificación y reconstrucción de la proyección del lazo: escalamiento, bandeja declarada y
registro (TASK-306; LC-LAZ-03 y 04; PAT-LAZ-RES-03; BR-LAZ-148 y 156; NFR-LAZ-39).

Contra PostgreSQL 16 real como ``vigia_app``:

- una divergencia publica ``loop_projection_divergence`` una sola vez por paso, con
  ``divergence_count``; hasta la tercera divergencia en 24 horas dispara la reconstrucción (que la
  corrige, ``state`` vuelve a ``healthy``); la cuarta no, y ``state`` queda ``diverged``
  (NFR-LAZ-39);
- con la marca ``rebuilding``, la bandeja (``InboxService.page``) declara la planta con
  ``InboxPartitionNotice.PROJECTION_REBUILDING`` en vez de un estado inventado (BR-LAZ-156);
- ``verify_loop_projection`` está en ``U04_TASKS`` (diaria, por organización); ``rebuild_loop_
  projection`` (LC-LAZ-03) corre a demanda y no tiene cadencia que vigilar (U04-H-14): no está en
  ``U04_TASKS``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopWorld, loop_world
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.application.inbox import InboxFilters, InboxService, PartitionDeclaration
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import InboxPartitionNotice
from vigia_platform.loop.projection.fold import FindingStateRow
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.loop.projection.verify import (
    ESCALATION_THRESHOLD,
    PROJECTION_DIVERGENCE_EVENT,
    VERIFY_LOOP_PROJECTION,
    VERIFY_LOOP_PROJECTION_SCHEDULE,
    ProjectionVerifier,
)
from vigia_platform.loop.registration import U04_TASKS
from vigia_platform.shared.outbox.registries import TaskIteration

pytestmark = pytest.mark.integration

_DIVERGENCE_EVENTS = (
    "SELECT payload::text AS payload FROM shared.outbox_event"
    " WHERE organization_id = $1 AND event_name = $2"
    " ORDER BY ledger_sequence NULLS FIRST, created_at"
)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(postgres_endpoint, "loop_projection_rebuild") as built:
        yield built


def _rebuilder(world: LoopWorld, ports: object) -> ProjectionRebuilder:
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    return ProjectionRebuilder(
        database=database,
        catalog=ports,  # type: ignore[arg-type]
        gates=ports,  # type: ignore[arg-type]
        coverage=ports,  # type: ignore[arg-type]
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        settings=PostgresLoopConfigurationRepository(database),
        clock=sessions.clock,
    )


def _verifier(world: LoopWorld, rebuilder: ProjectionRebuilder) -> ProjectionVerifier:
    sessions = world.stack.authz.sessions
    return ProjectionVerifier(
        database=world.stack.primary.database,
        rebuilder=rebuilder,
        outbox=world.stack.outbox,
        clock=sessions.clock,
    )


def _diverge(world: LoopWorld, subject: IngestedSubject) -> None:
    """Mutación directa y consistente (como un defecto de la caché), igual que en las pruebas de
    propiedad: invierte ``evidence_flagged`` y recalcula el resumen sobre el valor mutado."""
    row = world.finding_state(subject)
    assert row is not None
    world.stack.execute(
        "UPDATE loop.finding_state SET evidence_flagged = NOT evidence_flagged"
        " WHERE organization_id = $1 AND finding_state_id = $2",
        subject.organization.organization_id,
        row["finding_state_id"],
    )
    mutated = world.finding_state(subject)
    assert mutated is not None
    digest = FindingStateRow(**mutated).computed_digest()
    world.stack.execute(
        "UPDATE loop.finding_state SET projection_digest = $1"
        " WHERE organization_id = $2 AND finding_state_id = $3",
        digest,
        subject.organization.organization_id,
        row["finding_state_id"],
    )


def test_divergence_escalates_to_incident_after_three(world: LoopWorld) -> None:
    organization = world.organization()
    consumer, ports = world.projection_consumer()
    subject = world.ingest_finding(organization)
    written = world.deliver(consumer, world.outbox_event(subject))
    assert written == 1

    rebuilder = _rebuilder(world, ports)
    verifier = _verifier(world, rebuilder)
    context = world.context(organization)

    for step in range(1, 5):
        _diverge(world, subject)
        outcome = world.stack.run(verifier.verify_plant(context, organization.plant_id))
        assert outcome.is_diverged, step
        assert outcome.divergence_count == step, step
        if step <= ESCALATION_THRESHOLD:
            assert outcome.rebuild_triggered, step
            assert not outcome.escalated, step
            checkpoint = world.checkpoint(organization)
            assert checkpoint is not None and checkpoint["state"] == "healthy", step
        else:
            assert not outcome.rebuild_triggered, step
            assert outcome.escalated, step
            checkpoint = world.checkpoint(organization)
            assert checkpoint is not None and checkpoint["state"] == "diverged", step

    rows = world.stack.fetch(
        _DIVERGENCE_EVENTS,
        organization.organization_id,
        PROJECTION_DIVERGENCE_EVENT,
    )
    assert len(rows) == 4
    counts = [json.loads(row["payload"])["divergence_count"] for row in rows]
    assert counts == [1, 2, 3, 4]
    for row in rows:
        payload = json.loads(row["payload"])
        assert payload["rows_checked"] == 1
        assert payload["rows_diverged"] == 1
        assert payload["full_scan"] is True


def test_bandeja_declares_rebuilding(world: LoopWorld) -> None:
    organization = world.organization()
    subject = world.ingest_finding(organization)
    world.project(subject)
    context = world.context(organization)
    service = InboxService(
        database=world.stack.primary.database, authorizer=world.stack.authz.authorizer
    )

    before = world.stack.run(service.page(context, filters=InboxFilters()))
    assert all(partition.plant_id != organization.plant_id for partition in before.partitions)

    world.stack.execute(
        "UPDATE loop.loop_projection_checkpoint SET state = 'rebuilding'"
        " WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    during = world.stack.run(service.page(context, filters=InboxFilters()))
    assert (
        PartitionDeclaration(organization.plant_id, InboxPartitionNotice.PROJECTION_REBUILDING)
        in during.partitions
    )

    world.stack.execute(
        "UPDATE loop.loop_projection_checkpoint SET state = 'healthy'"
        " WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    after = world.stack.run(service.page(context, filters=InboxFilters()))
    assert all(partition.plant_id != organization.plant_id for partition in after.partitions)


def test_verify_loop_projection_registered_without_rebuild() -> None:
    schedule, iteration = U04_TASKS[VERIFY_LOOP_PROJECTION]
    assert schedule == VERIFY_LOOP_PROJECTION_SCHEDULE
    assert iteration is TaskIteration.PER_ORGANIZATION
    assert "rebuild_loop_projection" not in U04_TASKS
