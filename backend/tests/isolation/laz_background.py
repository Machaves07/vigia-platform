"""Trabajo sintético pendiente y manejadores reales para el arnés transversal de U-04.

Solo se sustituyen proveedores externos (IA, correo y composición de exportación); repositorios,
RLS, escritor, contextos y registro de manejadores son los de producción. Las fábricas no son
nombres de archivos: cada caso entrega una invocación ejecutable y trabajo en ambas organizaciones.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from unittest.mock import Mock

import pytest

from tests.isolation.gob_world import fingerprint
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import ConsumerRegistry, PeriodicTaskRegistry
from vigia_platform.shared.worker.scheduler import OrganizationReads


@dataclass
class Scenario:
    """Una entrega o iteración real, con su catálogo, base y contextos del despachador."""

    name: str
    run: Callable[..., Any]
    fetch: Callable[..., list[Any]]
    database: Any
    contexts: Any
    registration: Any
    organizations: tuple[uuid.UUID, uuid.UUID]
    audit: Any
    outbox: Any
    clock: Any
    events: dict[uuid.UUID, Any] = field(default_factory=dict)
    prepare: Callable[[uuid.UUID], None] = lambda _: None

    def prints(self, organization_id: uuid.UUID) -> dict[str, str]:
        # Incluye U-04 y todas sus dependencias, también tablas nuevas y tablas particionadas.
        rows = self.fetch(
            "SELECT DISTINCT n.nspname || '.' || c.relname AS name"
            " FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid"
            " JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE a.attname='organization_id' AND NOT a.attisdropped"
            " AND c.relkind IN ('r','p') AND NOT c.relispartition"
            " AND n.nspname=ANY($1::text[]) ORDER BY 1",
            [
                "identity",
                "ledger",
                "shared",
                "catalog",
                "fleet",
                "loop",
                "accreditation",
                "notifications",
            ],
        )
        tables = [str(row["name"]) for row in rows]
        assert any(name.startswith("loop.") for name in tables)
        assert any(name.startswith("notifications.") for name in tables)
        assert any(name.startswith("accreditation.") for name in tables)
        return fingerprint(self.fetch, organization_id, tables)

    async def invoke(self, organization_id: uuid.UUID, *, absent: bool = False) -> None:
        """La misma frontera transaccional que Dispatcher y PeriodicScheduler.

        ``absent`` simula que la fábrica del alcance no produjo contexto. No se entra al
        manejador con una transacción sin contexto: Database debe rechazarlo antes.
        """
        if self.name == "email_delivery_events_poll":
            contexts = self.contexts if not absent else Mock(provider_audit_context=lambda: None)
            scope = OrganizationReads(
                database=self.database, contexts=contexts, task=self.registration
            )
            await self.registration.handler(scope)
            return
        if self.events:
            event = self.events[organization_id]
            context = self.contexts.context_from_event(event, unit=ActorUnit.U04)
        else:
            context = self.contexts.context_for_organization(self.registration, organization_id)
        async with self.database.transaction(None if absent else context) as transaction:
            if self.events:
                await self.registration.handler(event, transaction)
            else:
                await self.registration.handler(transaction)


def loop_scenario(
    world: Any, name: str, registration: Any, events: dict[uuid.UUID, Any] | None = None
) -> Scenario:
    stack = world.stack
    return Scenario(
        name,
        stack.run,
        stack.fetch,
        stack.primary.database,
        stack.authz.contexts,
        registration,
        (world.a.organization_id, world.b.organization_id),
        stack.authz.sessions.audit,
        stack.outbox,
        stack.authz.sessions.clock,
        events or {},
    )


@contextlib.contextmanager
def loop_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.integration.test_loop_projection_rebuild import _diverge, _rebuilder, _verifier
    from vigia_platform.loop.application.review_queue import (
        ReviewQueueReminder,
        register_review_queue,
        register_review_reminders,
    )
    from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
    from vigia_platform.loop.projection.consumer import register_loop_projection
    from vigia_platform.loop.projection.verify import (
        register_verify_loop_projection,
        verify_loop_projection_handler,
    )

    with contextlib.nullcontext(request.getfixturevalue("loop_background_world")) as world:
        events: dict[uuid.UUID, Any] = {}
        consumers, tasks = ConsumerRegistry(), PeriodicTaskRegistry()
        consumer, ports = world.projection_consumer()
        review, repository = world.review_queue_consumer()
        register_loop_projection(consumers, consumer)
        register_review_queue(consumers, review)
        register_review_reminders(
            tasks,
            ReviewQueueReminder(
                repository=repository,
                configuration=PostgresLoopConfigurationRepository(world.stack.primary.database),
                outbox=world.stack.outbox,
                clock=world.stack.authz.sessions.clock,
            ),
        )
        register_verify_loop_projection(
            tasks, verify_loop_projection_handler(_verifier(world, _rebuilder(world, ports)))
        )
        for organization in (world.a, world.b):
            if name in ("review_queue", "review_reminders"):
                subject = world.ingest_detection(organization)
                event = world.review_queue_event(subject)
                if name == "review_reminders":
                    world.deliver_review_queue(review, event)
                else:
                    events[organization.organization_id] = event
            else:
                subject = world.ingest_finding(organization)
                event = world.outbox_event(subject)
                if name == "verify_loop_projection":
                    world.deliver(consumer, event)
                    _diverge(world, subject)
                else:
                    events[organization.organization_id] = event
        if name == "review_reminders":
            world.stack.tick(20 * 86400)
        registration = consumers.get(name) if events else tasks.get(name)
        assert registration is not None
        yield loop_scenario(world, name, registration, events)


@contextlib.contextmanager
def deadlines_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.loop_deadlines_support import deadlines_stack

    with deadlines_stack(
        request.getfixturevalue("postgres_endpoint"), "isolation_deadlines"
    ) as stack:
        world = stack.actions.world
        dates = []
        for organization in (world.a, world.b):
            _, _, due = stack.with_action(
                organization, stack.actions.member(organization), stack.actions.user(organization)
            )
            dates.append(due)
        stack.tick_to(max(dates))
        yield loop_scenario(world, name, stack.task)


@contextlib.contextmanager
def unclosable_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.loop_unclosable_support import unclosable_stack
    from vigia_platform.loop.registration import loop_tasks
    from vigia_platform.shared.context import Role

    with unclosable_stack(
        request.getfixturevalue("postgres_endpoint"), "isolation_unclosable"
    ) as stack:
        subjects = []
        for organization in (stack.world.a, stack.world.b):
            subject = stack.opened(organization)
            stack.ensure_signed_policy(organization)
            who = stack.member(organization, Role.COORDINATOR_SST)
            response = stack.declare(who, subject, stack.declaration_body(stack.user(organization)))
            assert response.status_code == 201, response.text
            subjects.append(subject)
        for subject in subjects:
            stack.reach_review_date(subject)
        registry = PeriodicTaskRegistry()
        loop_tasks(registry, stack.unit_services())
        yield loop_scenario(stack.world, name, registry.get(name))


@contextlib.contextmanager
def assistant_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.loop_assistant_support import assistant_stack
    from vigia_platform.loop.assistant.consumer import register_assistant_worker

    with assistant_stack(
        request.getfixturevalue("postgres_endpoint"), "isolation_assistant"
    ) as stack:
        events = {}
        for which in ("a", "b"):
            requested = stack.requested(stack.coordinator(which), which=which)
            events[stack.organization(which).organization_id] = stack.event_of(requested)
        registry = ConsumerRegistry()
        register_assistant_worker(registry, stack.use(stack.provider()))
        yield loop_scenario(stack.base.world, name, registry.get(name), events)


@contextlib.contextmanager
def export_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.export_support import export_body, export_stack
    from tests.factories import uuid7
    from vigia_platform.accreditation.export.worker import register_export_worker
    from vigia_platform.shared.outbox.publish import OutboxEvent

    with export_stack(request.getfixturevalue("postgres_endpoint"), "isolation_export") as stack:
        events = {}
        for _ in range(2):
            site = stack.site()
            plant, zones = next(iter(site.plants.items()))
            response = stack.request_export(
                stack.member(site),
                export_body(plant, zones, start=stack.now() - timedelta(days=31)),
            )
            assert response.status_code == 202, response.text
            (row,) = stack.events(site.organization_id)
            payload = row["payload"]
            events[site.organization_id] = OutboxEvent(
                event_id=row["event_id"],
                organization_id=site.organization_id,
                plant_id=row["plant_id"],
                event_name="export_requested",
                partition_key=row["partition_key"],
                ledger_sequence=None,
                payload=json.loads(payload) if isinstance(payload, str) else payload,
                correlation_id=uuid7(),
                created_at=stack.now(),
            )
        registry = ConsumerRegistry()
        register_export_worker(registry, stack.export_worker())
        yield Scenario(
            name,
            stack.run,
            stack.fetch,
            stack.database,
            stack.authz.contexts,
            registry.get(name),
            tuple(events),
            stack.authz.sessions.audit,
            stack.outbox,
            stack.clock,
            events,
        )


@contextlib.contextmanager
def adoption_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.adoption_support import adoption_stack
    from vigia_platform.accreditation.adoption.task import register_adoption_check

    with adoption_stack(
        request.getfixturevalue("postgres_endpoint"), "isolation_adoption"
    ) as stack:
        world = stack.actions.world
        for organization in (world.a, world.b):
            stack.set_silence_days(organization, 1)
            stack.activate(stack.site_zone(organization))
        stack.tick(4 * 86400)
        registry = PeriodicTaskRegistry()
        register_adoption_check(registry, stack.check_like(stack.real_activations()))
        yield loop_scenario(world, name, registry.get(name))


@contextlib.contextmanager
def metrics_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.exposure_support import exposure_stack
    from tests.integration.test_metrics_routes import service
    from vigia_platform.accreditation.metrics.task import (
        MetricsMaterialize,
        register_metrics_materialize,
    )

    with exposure_stack(request.getfixturevalue("postgres_endpoint"), "isolation_metrics") as stack:
        world = stack.world
        start, _ = stack.bounds(stack.period())
        stack.execute(
            "UPDATE identity.zone SET created_at=$1 WHERE zone_id=ANY($2::uuid[])",
            start - timedelta(days=2),
            [world.a.zone_id, world.b.zone_id],
        )
        registry = PeriodicTaskRegistry()
        register_metrics_materialize(
            registry,
            MetricsMaterialize(service=service(stack), clock=world.stack.authz.sessions.clock),
        )
        yield loop_scenario(world, name, registry.get(name))


@contextlib.contextmanager
def monthly_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from vigia_platform.accreditation.monthly.task import register_monthly_package

    # Fixture del módulo de sellado: PostgreSQL y S3 LocalStack, firma y escritor reales.
    monthly = request.getfixturevalue("monthly_world")
    world = monthly.loop
    for organization in (world.a, world.b):
        monthly.configure(organization)
        monthly.grant_coordinator(organization)
        subject = world.ingest_finding(organization)
        monthly.classify(subject)
    monthly.clock.advance(45 * 86400)
    registry = PeriodicTaskRegistry()
    register_monthly_package(registry, monthly.task(max_catchup_months=1))
    yield loop_scenario(world, name, registry.get(name))


@contextlib.contextmanager
def notification_case(request: pytest.FixtureRequest, name: str) -> Iterator[Scenario]:
    from tests.notification_support import Senders, StubSender
    from tests.properties.laz.test_pr_laz_58_bounces_complaints import (
        FakeSqsClient,
        _bounce_body,
        _consumer,
        _setup,
    )
    from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
    from vigia_platform.notifications.adapters.ses.delivery_events import (
        EmailDeliveryEventsPollTrigger,
        register_email_delivery_events,
        register_email_delivery_events_poll,
    )
    from vigia_platform.notifications.digest import DigestWeeklyTask, register_digest_weekly
    from vigia_platform.notifications.engine import register_notification_engine
    from vigia_platform.notifications.grouping import EmailGroupingTask, register_email_grouping
    from vigia_platform.shared.context import Role, ScopeLevel

    with contextlib.nullcontext(request.getfixturevalue("notification_background_world")) as world:
        consumers, tasks = ConsumerRegistry(), PeriodicTaskRegistry()
        events, messages = {}, {}
        organizations = []
        sqs = FakeSqsClient()
        for _ in range(2):
            if name == "email_delivery_events":
                organization, _, message = _setup(world)
                messages[organization] = message
                event = world.event(world.provider_id, "email_delivery_events_poll", {})
            else:
                organization, plant, zone = world.new_organization()
                recipient = world.add_member(
                    organization, Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION, organization
                )
                event = world.event(
                    organization,
                    "finding_received",
                    {"zone_id": str(zone), "platform_record_id": str(uuid.uuid4())},
                    plant_id=plant,
                )
                if name in ("email_grouping", "digest_weekly"):
                    world.seed_delivery(
                        organization,
                        recipient.user_id,
                        urgency="normal" if name == "email_grouping" else "digest",
                        plant_id=plant,
                        zone_id=zone,
                    )
            organizations.append(organization)
            if name in ("notification_engine", "email_delivery_events"):
                events[organization] = event
        senders = Senders(StubSender())
        register_notification_engine(consumers, world.engine(senders=senders))
        register_email_delivery_events(consumers, _consumer(world, sqs))
        register_email_grouping(
            tasks,
            EmailGroupingTask(
                configuration=PostgresLoopConfigurationRepository(world.database),
                senders=senders,
                clock=world.clock,
            ),
        )
        register_digest_weekly(tasks, DigestWeeklyTask(senders=senders, clock=world.clock))
        # La bandeja real se obtiene de la pila de identidad; no se sustituye publish.
        from vigia_platform.ledger.application.audit_writer import AuditWriter
        from vigia_platform.shared.outbox.publish import Outbox
        from vigia_platform.shared.outbox.registries import OutboxCatalog
        from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
        from vigia_platform.shared.runtime.units import registered_units

        catalog = OutboxCatalog()
        for unit in registered_units():
            unit.event_types(catalog.event_types)

        async def synchronize() -> None:
            async with world.database.transaction(
                world.contexts.provider_audit_context()
            ) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), world.clock)

        world.run(synchronize())
        outbox = Outbox(catalog, world.clock)
        register_email_delivery_events_poll(tasks, EmailDeliveryEventsPollTrigger(outbox))
        if name in ("email_grouping", "digest_weekly"):
            world.clock.advance(8 * 86400)
        registration = consumers.get(name) if events else tasks.get(name)
        yield Scenario(
            name,
            world.run,
            world.fetch,
            world.database,
            world.contexts,
            registration,
            tuple(organizations),
            AuditWriter(
                database=world.database,
                clock=world.clock,
                provider_organization_id=world.provider_id,
            ),
            outbox,
            world.clock,
            events,
            prepare=(
                lambda organization: sqs.enqueue(_bounce_body(messages[organization], "Permanent"))
            )
            if messages
            else (lambda _: None),
        )
