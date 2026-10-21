"""Los seis consumidores de U-04 sobre el despachador real de U-02 (LC-LAZ-42; TASK-352).

Lo comparten FS-LAZ-08 (reentrega y entrega tardía), FS-LAZ-09 (cola muerta y reproceso) y
DR-LAZ-03 (reproceso de la cola muerta acumulada durante una caída). ``consumer_case(nombre)``
construye, para cada consumidor, su mundo de pruebas real sobre PostgreSQL 16 (el mismo de su
tarea: ``loop_world``, ``assistant_stack``, ``export_stack`` o ``notification_world``) con:

- el manejador **real** registrado con su función de registro de producción (nombre,
  suscripciones y ``has_external_dependency``), envuelto en ``Outage``: mientras ``down``, la
  entrega falla **sin efecto** con un error no declarado, el que consume los intentos de la
  política de U-02 hasta la cola muerta (nunca ``ExternalDependencyDown``, que abriría el
  circuito sin consumir intentos);
- eventos publicados de verdad (con sus entregas) en dos particiones distintas
  (``produce(0 | 1)``) y su entrega directa (``deliver``), con la frontera transaccional del
  despachador;
- el ``Dispatcher`` real con métricas en exportadores en memoria (``dead_letter_created_total`` de
  U-02 y ``loop_dead_letter_entries_total`` de U-04 por ``LoopWorkerTelemetry``, como lo compone
  la raíz del worker: las alarmas a la primera entrada) y la orden real
  ``vigia-admin replay-dead-letter`` (``admin_cli.run`` con la raíz de composición
  administrativa, como ``vigia_app``);
- ``effects()`` (lo escrito por el consumidor, para comparar el estado final) y ``shape(evento)``
  (el efecto de un evento sin identificadores, para comparar particiones).

Solo dobles de lo externo: el proveedor del asistente (``FakeAssistantProvider``), el compositor
de la exportación (``SyntheticComposer``), el remitente de correo (``StubSender``) y la cola SQS
(``FakeSqsClient``). Solo datos generados; un solo reloj simulado por mundo.
"""

from __future__ import annotations

import contextlib
import io
import json
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.laz_support import LoopMetricsExport, loop_metrics_export
from tests.runtime_support import FakeReader, database_secret, runtime_environ
from vigia_platform.identity.application import admin_cli
from vigia_platform.loop.observability.metrics import LoopMetricName
from vigia_platform.loop.observability.worker import LoopWorkerTelemetry
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.dispatcher import Dispatcher, DispatchReport, Outcome
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS
from vigia_platform.shared.runtime.admin import compose_admin_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

__all__ = ["CONSUMERS", "ConsumerCase", "Outage", "consumer_case"]

CONSUMERS: Final = (
    "loop_projection",
    "review_queue",
    "notification_engine",
    "assistant_worker",
    "export_worker",
    "email_delivery_events",
)
"""Los seis consumidores de U-04 (``loop.registration.U04_CONSUMERS``)."""

RETRY_TICK_SECONDS: Final = 661
"""Mayor que el retroceso más largo de U-02 con su variación (600 s + 10 %)."""


@dataclass
class Outage:
    """El manejador real del consumidor; mientras ``down``, la entrega falla sin efecto."""

    real: Any = None
    down: bool = False
    failures: int = 0

    async def __call__(self, event: OutboxEvent, transaction: Any) -> None:
        if self.down:
            self.failures += 1
            raise RuntimeError("caída sintética del manejador")
        if self.real is None:
            raise RuntimeError("la prueba no fijó el manejador real")
        await self.real(event, transaction)


@dataclass
class ConsumerCase:
    name: str
    outage: Outage
    run: Callable[[Any], Any]
    fetch: Callable[..., list[Any]]
    tick: Callable[[float], None]
    database: Any
    contexts: Any
    catalog: Any
    outbox: Any
    clock: Any
    cli_identity: tuple[Any, uuid.UUID, uuid.UUID]
    """``(base migrada, organización proveedora, operador)`` para ``vigia-admin``."""
    produce: Callable[[int], OutboxEvent]
    effects: Callable[[], Any]
    shape: Callable[[OutboxEvent], Any]
    extra: dict[str, Any] = field(default_factory=dict)
    reader: InMemoryMetricReader | None = None
    metrics: Any = None
    loop_export: LoopMetricsExport | None = None

    # --- entregas -----------------------------------------------------------------------------

    def deliver(self, event: OutboxEvent) -> None:
        """Una entrega directa del manejador real, como la haría el despachador."""
        context = self.contexts.context_from_event(event, unit=ActorUnit.U04)

        async def run() -> None:
            async with self.database.transaction(context) as transaction:
                await self.outage.real(event, transaction)

        self.run(run())

    def dispatcher(self) -> Dispatcher:
        """El despachador real con las dos métricas de la alarma en memoria: la de U-02
        (``dead_letter_created_total``) y la de U-04 (``loop_dead_letter_entries_total``, por la
        telemetría que la raíz del worker le pasa)."""
        if self.metrics is None:
            self.metrics, self.reader = metrics_with_reader()
            self.loop_export = loop_metrics_export()
        return Dispatcher(
            database=self.database,
            catalog=self.catalog,
            outbox=self.outbox,
            contexts=self.contexts,
            clock=self.clock,
            jitter=lambda: 0.5,
            metrics=self.metrics,
            telemetry=LoopWorkerTelemetry(
                database=self.database,
                contexts=self.contexts,
                clock=self.clock,
                task=None,  # type: ignore[arg-type]  # solo report_once la usa
                metrics=self.loop_export.metrics,
            ),
        )

    def dispatch(self) -> DispatchReport:
        report: DispatchReport = self.run(self.dispatcher().dispatch_once(self.name))
        return report

    def statuses(self, events: list[OutboxEvent]) -> dict[uuid.UUID, str]:
        rows = self.fetch(
            "SELECT event_id, status FROM shared.outbox_delivery"
            " WHERE consumer_name = $1 AND event_id = ANY($2::uuid[])",
            self.name,
            [event.event_id for event in events],
        )
        return {uuid.UUID(str(row["event_id"])): str(row["status"]) for row in rows}

    def drain(self, events: list[OutboxEvent], *, settled: set[str]) -> list[DispatchReport]:
        """Rondas del despachador hasta que cada entrega de ``events`` esté en ``settled``; tope de
        rondas generoso, nunca de pared. El reloj solo avanza (más que el retroceso) tras una
        ronda que no entregó nada: si avanzara siempre, la segunda solicitud de una partición
        pasaría su tope duro de 120 s antes de entregarse."""
        reports = []
        for _ in range((len(events) + 2) * (MAX_ATTEMPTS + 2)):
            if set(self.statuses(events).values()) <= settled:
                break
            report = self.dispatch()
            reports.append(report)
            if not report.count(Outcome.DELIVERED):
                self.tick(RETRY_TICK_SECONDS)
        assert set(self.statuses(events).values()) <= settled, self.statuses(events)
        return reports

    def dead_letter_alarms(self) -> float:
        """``dead_letter_created_total`` de este consumidor en el exportador en memoria. Un
        nombre de evento sin dimensión registrada sale como ``other``; por eso se cuenta por
        consumidor. ``notification_engine`` también recibe el propio ``dead_letter_created``:
        con su manejador caído, ese aviso puede ir después a su cola muerta."""
        if self.reader is None:
            return 0.0
        return sum(
            value
            for attributes, value in metric_points(
                self.reader, MetricName.DEAD_LETTER_CREATED_TOTAL
            )
            if attributes.get("consumer") == self.name
        )

    def loop_dead_letter_alarms(self) -> float:
        """``loop_dead_letter_entries_total`` de este consumidor (la alarma ``loop-dead-letter``
        de U-04) en el exportador en memoria."""
        if self.loop_export is None:
            return 0.0
        value = self.loop_export.value(
            LoopMetricName.LOOP_DEAD_LETTER_ENTRIES_TOTAL, consumer=self.name
        )
        return 0.0 if value is None else value

    def dead_letter_rows(self, events: list[OutboxEvent]) -> list[Any]:
        return self.fetch(
            "SELECT event_id, attempts, last_error_code FROM shared.dead_letter"
            " WHERE consumer_name = $1 AND event_id = ANY($2::uuid[])",
            self.name,
            [event.event_id for event in events],
        )

    def dead_letter_events(self, events: list[OutboxEvent]) -> list[Any]:
        """Los ``dead_letter_created`` publicados (la alarma en la bandeja)."""
        return self.fetch(
            "SELECT payload ->> 'event_id' AS event_id FROM shared.outbox_event"
            " WHERE event_name = 'dead_letter_created' AND payload ->> 'consumer_name' = $1"
            " AND payload ->> 'event_id' = ANY($2::text[])",
            self.name,
            [str(event.event_id) for event in events],
        )

    # --- vigia-admin --------------------------------------------------------------------------

    def cli(self, *arguments: str) -> tuple[int, Any, str]:
        """``vigia-admin`` real (raíz administrativa sobre la misma base, como ``vigia_app``)."""
        migrated, provider, operator = self.cli_identity
        endpoint = migrated.as_role("vigia_app")
        runtime = RuntimeConfig.from_environ(
            runtime_environ(
                VIGIA_PROVIDER_ORGANIZATION_ID=str(provider),
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

        async def build(config: Any, provider_id: uuid.UUID) -> Any:
            return await compose_admin_runtime(
                config, provider_id, runtime, clock=self.clock, reader=reader
            )

        out, err = io.StringIO(), io.StringIO()
        code = admin_cli.run(
            [*arguments, "--operator", str(operator)],
            environ={"VIGIA_ENVIRONMENT": "test", "VIGIA_PROVIDER_ORGANIZATION_ID": str(provider)},
            builder=build,
            stdout=out,
            stderr=err,
            stdin=io.StringIO(),
        )
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def replay(self, event: OutboxEvent) -> dict[str, Any]:
        code, result, error = self.cli("replay-dead-letter", str(event.event_id), self.name)
        assert code == 0, error
        assert isinstance(result, dict)
        return result


def stored_event(fetch: Callable[..., list[Any]], event_id: uuid.UUID) -> OutboxEvent:
    (row,) = fetch(
        "SELECT event_id, organization_id, plant_id, event_name, partition_key,"
        " ledger_sequence, payload::text AS payload, correlation_id, created_at"
        " FROM shared.outbox_event WHERE event_id = $1",
        event_id,
    )
    return OutboxEvent(
        event_id=uuid.UUID(str(row["event_id"])),
        organization_id=uuid.UUID(str(row["organization_id"])),
        plant_id=None if row["plant_id"] is None else uuid.UUID(str(row["plant_id"])),
        event_name=str(row["event_name"]),
        partition_key=str(row["partition_key"]),
        ledger_sequence=row["ledger_sequence"],
        payload=json.loads(row["payload"]),
        correlation_id=uuid.UUID(str(row["correlation_id"])),
        created_at=row["created_at"],
    )


def _rows(rows: list[Any]) -> list[tuple[Any, ...]]:
    return sorted((tuple(dict(row).values()) for row in rows), key=repr)


# --- loop_projection y review_queue --------------------------------------------------------------


@contextlib.contextmanager
def _loop_case(endpoint: PostgresEndpoint, name: str) -> Iterator[ConsumerCase]:
    from tests.laz_support import loop_world, register_u04_and_catalog
    from vigia_platform.loop.application.review_queue import register_review_queue
    from vigia_platform.loop.projection.consumer import register_loop_projection

    outage = Outage()
    catalogs: list[Any] = []

    def register(registry: Any, catalog: Any) -> None:
        register_u04_and_catalog(registry, catalog)
        if name == "loop_projection":
            register_loop_projection(catalog.consumers, outage)  # type: ignore[arg-type]
        else:
            register_review_queue(catalog.consumers, outage)  # type: ignore[arg-type]
        catalogs.append(catalog)

    with loop_world(endpoint, f"fs_{name}", register=register) as world:
        stack = world.stack
        if name == "loop_projection":
            outage.real = world.projection_consumer()[0]
        else:
            outage.real = world.review_queue_consumer()[0]
        organizations = (world.a, world.b)
        subjects: dict[uuid.UUID, Any] = {}

        def produce(partition: int) -> OutboxEvent:
            organization = organizations[partition]
            if name == "loop_projection":
                subject = world.ingest_finding(organization)
                event = world.outbox_event(subject)
            else:
                subject = world.ingest_detection(organization)
                event = world.review_queue_event(subject)
            subjects[event.event_id] = subject
            return event

        orgs = [o.organization_id for o in organizations]

        def records() -> list[tuple[Any, ...]]:
            return _rows(
                stack.fetch(
                    "SELECT record_type, count(*) AS n FROM ledger.ledger_record"
                    " WHERE organization_id = ANY($1::uuid[]) GROUP BY record_type",
                    orgs,
                )
            )

        def effects() -> Any:
            if name == "loop_projection":
                rows = stack.fetch(
                    "SELECT subject_id, lifecycle_state, last_event_id, last_record_id,"
                    " projection_digest FROM loop.finding_state"
                    " WHERE organization_id = ANY($1::uuid[])",
                    orgs,
                )
            else:
                rows = stack.fetch(
                    "SELECT detection_id, source_event_id, state FROM loop.review_queue_item"
                    " WHERE organization_id = ANY($1::uuid[])",
                    orgs,
                )
            return {"rows": _rows(rows), "records": records()}

        def shape(event: OutboxEvent) -> Any:
            subject = subjects[event.event_id]
            if name == "loop_projection":
                rows = stack.fetch(
                    "SELECT lifecycle_state, last_event_id = $2 AS applied FROM loop.finding_state"
                    " WHERE organization_id = $1 AND subject_id = $3",
                    event.organization_id,
                    event.event_id,
                    subject.subject_id,
                )
            else:
                rows = stack.fetch(
                    "SELECT state, source_event_id = $2 AS applied FROM loop.review_queue_item"
                    " WHERE organization_id = $1 AND detection_id = $3",
                    event.organization_id,
                    event.event_id,
                    subject.subject_id,
                )
            return _rows(rows)

        authz = stack.authz
        yield ConsumerCase(
            name=name,
            outage=outage,
            run=stack.run,
            fetch=stack.fetch,
            tick=stack.tick,
            database=stack.primary.database,
            contexts=authz.contexts,
            catalog=catalogs[0],
            outbox=stack.outbox,
            clock=authz.sessions.clock,
            cli_identity=(
                authz.sessions.migrated,
                authz.provider_organization_id,
                authz.operator_id,
            ),
            produce=produce,
            effects=effects,
            shape=shape,
        )


# --- assistant_worker -----------------------------------------------------------------------------


@contextlib.contextmanager
def _assistant_case(endpoint: PostgresEndpoint) -> Iterator[ConsumerCase]:
    from tests.attribution_support import both_policy
    from tests.loop_assistant_support import assistant_stack
    from vigia_platform.loop.assistant.consumer import AssistantWorker

    with assistant_stack(endpoint, "fs_assistant_worker") as stack:
        provider = stack.provider()
        outage = Outage(
            AssistantWorker(
                database=stack.base.database,
                provider=provider,
                free_text=both_policy(),
                clock=stack.clock,
                sleep=stack.sleep,
            )
        )
        stack.worker = outage  # type: ignore[assignment]
        requests: dict[uuid.UUID, str] = {}

        def produce(partition: int) -> OutboxEvent:
            which = "ab"[partition]
            request_id = stack.requested(stack.coordinator(which), which=which)
            event = stack.event_of(request_id)
            requests[event.event_id] = request_id
            return event

        def effects() -> Any:
            rows = stack.fetch(
                "SELECT request_id, state, unavailable_cause, attempt_count, input_tokens,"
                " output_tokens FROM loop.assistant_request"
            )
            drafts = stack.fetch("SELECT request_id, draft_text FROM loop.assistant_draft")
            return {"rows": _rows(rows), "drafts": _rows(drafts), "calls": provider.call_count}

        def shape(event: OutboxEvent) -> Any:
            row = stack.request_row(requests[event.event_id])
            drafts = stack.drafts(requests[event.event_id])
            return (row["state"], row["unavailable_cause"], row["attempt_count"], len(drafts))

        world = stack.base.world
        authz = world.stack.authz
        yield ConsumerCase(
            name="assistant_worker",
            outage=outage,
            run=stack.run,
            fetch=stack.fetch,
            tick=stack.clock.advance,
            database=stack.base.database,
            contexts=authz.contexts,
            catalog=stack.catalog,
            outbox=world.stack.outbox,
            clock=stack.clock,
            cli_identity=(
                authz.sessions.migrated,
                authz.provider_organization_id,
                authz.operator_id,
            ),
            produce=produce,
            effects=effects,
            shape=shape,
            extra={"provider": provider},
        )


# --- export_worker --------------------------------------------------------------------------------


@contextlib.contextmanager
def _export_case(endpoint: PostgresEndpoint) -> Iterator[ConsumerCase]:
    from tests.export_support import SyntheticComposer, export_body, export_stack

    with export_stack(endpoint, "fs_export_worker") as stack:
        composer = SyntheticComposer()
        outage = Outage(stack.export_worker(composer=composer))
        stack.worker = outage  # type: ignore[assignment]
        exports: dict[uuid.UUID, uuid.UUID] = {}

        def produce(partition: int) -> OutboxEvent:
            site = stack.site(plants=1, zones=1)
            plant, zones = next(iter(site.plants.items()))
            response = stack.request_export(
                stack.member(site),
                export_body(plant, list(zones), start=stack.now() - timedelta(days=31)),
            )
            assert response.status_code == 202, response.text
            export_id = uuid.UUID(response.json()["export_id"])
            (row,) = stack.events(site.organization_id)
            event = stored_event(stack.fetch, uuid.UUID(str(row["event_id"])))
            exports[event.event_id] = export_id
            return event

        def effects() -> Any:
            rows = stack.fetch(
                "SELECT export_id, state, attempt_count FROM accreditation.export_request"
            )
            packages = stack.fetch(
                "SELECT export_id, archive_sha256 FROM accreditation.export_package"
            )
            jobs = Counter(str(job.export_id) for job in composer.jobs)
            return {"rows": _rows(rows), "packages": _rows(packages), "jobs": sorted(jobs.items())}

        def shape(event: OutboxEvent) -> Any:
            export_id = exports[event.event_id]
            row = stack.export_row(export_id)
            package = stack.package_row(export_id)
            jobs = sum(1 for job in composer.jobs if job.export_id == export_id)
            return (row["state"], row["attempt_count"], package is not None, jobs)

        authz = stack.authz
        yield ConsumerCase(
            name="export_worker",
            outage=outage,
            run=stack.run,
            fetch=stack.fetch,
            tick=stack.tick,
            database=stack.database,
            contexts=authz.contexts,
            catalog=stack.catalog,
            outbox=stack.outbox,
            clock=stack.clock,
            cli_identity=(
                authz.sessions.migrated,
                authz.provider_organization_id,
                authz.operator_id,
            ),
            produce=produce,
            effects=effects,
            shape=shape,
            extra={"composer": composer},
        )


# --- notification_engine y email_delivery_events --------------------------------------------------


@contextlib.contextmanager
def _notification_case(endpoint: PostgresEndpoint, name: str) -> Iterator[ConsumerCase]:
    from tests.notification_support import Senders, StubSender, notification_world
    from tests.properties.laz.test_pr_laz_58_bounces_complaints import (
        FakeSqsClient,
        _bounce_body,
        _consumer,
        _setup,
    )
    from vigia_platform.notifications.adapters.ses.delivery_events import (
        register_email_delivery_events,
    )
    from vigia_platform.notifications.engine import register_notification_engine
    from vigia_platform.shared.context import Role, ScopeLevel
    from vigia_platform.shared.outbox.publish import NewEvent, Outbox
    from vigia_platform.shared.outbox.registries import OutboxCatalog
    from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
    from vigia_platform.shared.runtime.units import registered_units
    from vigia_platform.shared.signing.keys import format_timestamp

    with notification_world(endpoint, f"fs_{name}") as world:
        catalog = OutboxCatalog()
        for unit in registered_units():
            unit.event_types(catalog.event_types)
        sqs = FakeSqsClient()
        if name == "notification_engine":
            outage = Outage(world.engine(senders=Senders(StubSender())))
            register_notification_engine(catalog.consumers, outage)  # type: ignore[arg-type]
        else:
            outage = Outage(_consumer(world, sqs))
            register_email_delivery_events(catalog.consumers, outage)  # type: ignore[arg-type]

        async def synchronize() -> None:
            async with world.database.transaction(
                world.contexts.provider_audit_context()
            ) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), world.clock)

        world.run(synchronize())
        outbox = Outbox(catalog, world.clock)
        sites = [world.new_organization(), world.new_organization()]
        if name == "notification_engine":
            for organization, _plant, zone in sites:
                world.add_member(organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone)
        messages: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, str]] = {}
        organizations: list[uuid.UUID] = [site[0] for site in sites]

        def publish(context: Any, event: NewEvent) -> OutboxEvent:
            async def run() -> uuid.UUID:
                async with world.database.transaction(context) as transaction:
                    publication = await outbox.publish(transaction, event)
                    return publication.event.event_id

            return stored_event(world.fetch, world.run(run()))

        def produce(partition: int) -> OutboxEvent:
            world.clock.advance(1)
            if name == "notification_engine":
                organization, plant, zone = sites[partition]
                context = world.event_context(world.event(organization, "review_overdue", {}))
                return publish(
                    context,
                    NewEvent(
                        event_name="review_overdue",
                        plant_id=plant,
                        payload={
                            "zone_id": str(zone),
                            "detection_id": str(uuid.uuid4()),
                            "review_due_at": format_timestamp(world.clock.now()),
                            "reminders_sent": 1,
                            "phase": "first",
                        },
                    ),
                )
            organization, user, provider_message_id = _setup(world)
            organizations.append(organization)
            sqs.enqueue(_bounce_body(provider_message_id, "Permanent"))
            event = publish(
                world.contexts.provider_audit_context(),
                NewEvent(event_name="email_delivery_events_poll", payload={}),
            )
            messages[event.event_id] = (organization, user, provider_message_id)
            return event

        def effects() -> Any:
            if name == "notification_engine":
                notifications = world.fetch(
                    "SELECT n.source_event_id, n.recipient_user_id, n.urgency, d.channel, d.status"
                    " FROM notifications.notification AS n"
                    " JOIN notifications.notification_delivery AS d"
                    " ON d.notification_id = n.notification_id"
                    " AND d.notification_created_at = n.created_at"
                    " WHERE n.organization_id = ANY($1::uuid[])",
                    organizations,
                )
                emails = world.fetch(
                    "SELECT to_user_id, kind, status FROM notifications.email_message"
                    " WHERE organization_id = ANY($1::uuid[])",
                    organizations,
                )
                return {"rows": _rows(notifications), "emails": _rows(emails)}
            emails = world.fetch(
                "SELECT message_id, status, bounce_kind, complaint_at FROM"
                " notifications.email_message WHERE organization_id = ANY($1::uuid[])",
                organizations,
            )
            preferences = world.fetch(
                "SELECT user_id, email_suppressed_hard_bounce, email_suppressed_complaint,"
                " email_suppressed_at FROM notifications.user_notification_preference"
                " WHERE organization_id = ANY($1::uuid[])",
                organizations,
            )
            return {"rows": _rows(emails), "preferences": _rows(preferences)}

        def shape(event: OutboxEvent) -> Any:
            if name == "notification_engine":
                return _rows(
                    world.fetch(
                        "SELECT n.urgency, d.channel, d.status FROM notifications.notification AS n"
                        " JOIN notifications.notification_delivery AS d"
                        " ON d.notification_id = n.notification_id"
                        " AND d.notification_created_at = n.created_at"
                        " WHERE n.source_event_id = $1",
                        event.event_id,
                    )
                )
            organization, user, provider_message_id = messages[event.event_id]
            (email,) = world.fetch(
                "SELECT e.status, e.bounce_kind FROM notifications.email_message AS e"
                " JOIN notifications.email_provider_message_key AS k"
                " ON k.message_id = e.message_id WHERE k.provider_message_id = $1",
                provider_message_id,
            )
            (preference,) = world.fetch(
                "SELECT email_suppressed_hard_bounce, email_suppressed_at IS NOT NULL AS marked"
                " FROM notifications.user_notification_preference"
                " WHERE organization_id = $1 AND user_id = $2",
                organization,
                user,
            )
            return (*email.values(), *preference.values())

        yield ConsumerCase(
            name=name,
            outage=outage,
            run=world.run,
            fetch=world.fetch,
            tick=world.clock.advance,
            database=world.database,
            contexts=world.contexts,
            catalog=catalog,
            outbox=outbox,
            clock=world.clock,
            cli_identity=(
                world.migrated,
                world.seed.provider_organization_id,
                world.seed.operator_id,
            ),
            produce=produce,
            effects=effects,
            shape=shape,
            extra={"sqs": sqs},
        )


@contextlib.contextmanager
def consumer_case(endpoint: PostgresEndpoint, name: str) -> Iterator[ConsumerCase]:
    """El caso de ``name`` (uno de ``CONSUMERS``) sobre su mundo real."""
    if name in ("loop_projection", "review_queue"):
        builder: Any = _loop_case(endpoint, name)
    elif name == "assistant_worker":
        builder = _assistant_case(endpoint)
    elif name == "export_worker":
        builder = _export_case(endpoint)
    elif name in ("notification_engine", "email_delivery_events"):
        builder = _notification_case(endpoint, name)
    else:
        raise ValueError(f"consumidor de U-04 desconocido: {name!r}")
    with builder as case:
        yield case


def dispatched(reports: list[DispatchReport], outcome: Outcome) -> int:
    return sum(report.count(outcome) for report in reports)
