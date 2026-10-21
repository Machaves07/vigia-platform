"""Inyección selectiva de pérdidas en la entrega a la proyección (LC-LAZ-42).

No borra eventos ni modifica el expediente: omite únicamente la entrega elegida al
consumidor, como un defecto en el transporte. Solo se utiliza desde pruebas.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from tests.laz_support import IngestedSubject, LoopWorld
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.projection.consumer import LoopProjectionConsumer
from vigia_platform.loop.projection.fold import FindingStateRow
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.shared.outbox.publish import OutboxEvent

# El caso pequeño corre siempre; -m nightly selecciona también los tamaños nocturnos.
PROFILES = [False, pytest.param(True, marks=pytest.mark.nightly, id="nightly")]


def resilience_storage(endpoint: Any, bucket: str, clock: Any) -> Any:
    """S3 real con topes de fixture de 60 s: la pared no decide estos escenarios."""
    from vigia_platform.shared.storage import S3Storage

    settings = replace(
        endpoint.storage_settings(bucket), connect_timeout_seconds=60, read_timeout_seconds=60
    )
    return S3Storage(settings, clock)


@contextmanager
def started_api(stack: Any) -> Iterator[None]:
    """Ejecuta el arranque y la parada reales de la API ASGI de la fixture."""
    app = stack.client._transport.app
    lifespan = app.router.lifespan_context(app)
    stack.run(lifespan.__aenter__())
    try:
        yield
    finally:
        stack.run(lifespan.__aexit__(None, None, None))


def rebuilder(world: LoopWorld, ports: Any, **changes: Any) -> ProjectionRebuilder:
    """Reconstrucción real, con ganchos de fase y topes heredados de LoopWorld (60 s)."""
    database = changes.pop("database", world.stack.primary.database)
    sessions = world.stack.authz.sessions
    return ProjectionRebuilder(
        database=database,
        catalog=ports,
        gates=ports,
        coverage=ports,
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        settings=PostgresLoopConfigurationRepository(database),
        clock=sessions.clock,
        batch_size=1,
        **changes,
    )


def mutate_projection(world: LoopWorld, subject: IngestedSubject) -> tuple[str, str]:
    """Inyecta una fila falsa con digest consistente; devuelve (falso, original)."""
    row = world.finding_state(subject)
    assert row is not None
    original = row["projection_digest"]
    world.stack.execute(
        "UPDATE loop.finding_state SET evidence_flagged = NOT evidence_flagged"
        " WHERE organization_id = $1 AND finding_state_id = $2",
        subject.organization.organization_id,
        row["finding_state_id"],
    )
    changed = world.finding_state(subject)
    assert changed is not None
    digest = FindingStateRow(**changed).computed_digest()
    world.stack.execute(
        "UPDATE loop.finding_state SET projection_digest = $1"
        " WHERE organization_id = $2 AND finding_state_id = $3",
        digest,
        subject.organization.organization_id,
        row["finding_state_id"],
    )
    return digest, original


def expire_export(stack: Any, export_id: Any) -> None:
    """Vence artificialmente el lease con el mismo reloj del worker; no usa now() de SQL."""
    from datetime import timedelta

    stack.execute(
        "UPDATE accreditation.export_request SET lease_until = $1 WHERE export_id = $2",
        stack.now() - timedelta(milliseconds=1),
        export_id,
    )


def run_worker_case(spec: dict[str, Any]) -> None:
    """Entrada del proceso de pruebas; nunca se carga desde vigia-worker de producción."""
    import json
    from pathlib import Path

    match spec["case"]:
        case "assistant":
            result = assistant_outage(spec)
        case "email":
            result = email_outage(spec)
        case "export":
            result = export_attempt(spec)
        case _:
            raise ValueError("caso de resiliencia no reconocido")
    Path(spec["result"]).write_text(json.dumps(result), encoding="utf-8")


def assistant_outage(spec: dict[str, Any]) -> dict[str, Any]:
    """API ASGI y consumidor reales en el proceso hijo, con una caída de tres minutos."""
    from datetime import timedelta

    from tests.integration.conftest import PostgresEndpoint
    from tests.loop_assistant_support import assistant_stack
    from tests.loop_root_cause_support import STATEMENTS, complete_signature
    from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider
    from vigia_platform.loop.assistant.port import AssistantFailure, FailureKind
    from vigia_platform.shared.outbox.dispatcher import Outcome

    with assistant_stack(PostgresEndpoint(**spec["endpoint"]), "fs_laz_01") as stack:
        clock = stack.clock
        started = clock.now()

        class TimedProvider:
            configured = True

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.probes: list[str] = []
                self.delegate = FakeAssistantProvider(
                    clock=clock, sleep=stack.sleep, latency=timedelta(0)
                )

            async def complete(self, context: Any, *, timeout: Any) -> Any:
                self.calls.append(clock.now().isoformat())
                if clock.now() < started + timedelta(minutes=3):
                    # El adaptador traduce el rechazo de conexión a fallo transitorio.
                    return AssistantFailure(FailureKind.TRANSIENT)
                return await self.delegate.complete(context, timeout=timeout)

            async def probe(self, *, timeout: Any) -> Any:
                """La sonda del cortacircuito (VIG-272): sin contexto ni fichas, misma ventana."""
                self.probes.append(clock.now().isoformat())
                if clock.now() < started + timedelta(minutes=3):
                    return AssistantFailure(FailureKind.TRANSIENT)
                return None

        provider = TimedProvider()
        stack.use(provider)
        who = stack.coordinator()
        analysis = stack.analysis(who)
        request = stack.requested(who, analysis)
        assert stack.dispatch().count(Outcome.DEPENDENCY_DOWN) == 1
        state, opened = stack.circuit()
        assert state == "open" and opened is not None
        calls, waits = len(provider.calls), len(stack.sleep.waits)
        denied = stack.post(who, analysis)
        assert denied.status_code == 503
        assert denied.json()["detail_code"] == "loop_assistant_unavailable"
        # La espera que anuncia la interfaz es la de la sonda (60 s): el circuito abierto responde
        # de inmediato y sin llamada externa, no es el tope de fichas ni un tiempo agotado.
        assert denied.headers["retry-after"] == "60"
        assert len(provider.calls) == calls and len(stack.sleep.waits) == waits
        for index, statement in enumerate(STATEMENTS, 1):
            response = stack.base.step(
                who, analysis, index, {"statement": statement, "evidence_refs": []}
            )
            assert response.status_code == 200, response.text
        signed = stack.base.signature(who, analysis, complete_signature())
        assert signed.status_code == 201, signed.text
        states, probes = [], []
        for seconds in (59.999, 60, 120, 180):
            clock.set(opened + timedelta(seconds=seconds))
            report = stack.dispatch()
            states.append(stack.circuit()[0])
            probes.append(report.probed)
        assert stack.request_row(request)["state"] == "unavailable"
        dead = stack.fetch(
            "SELECT count(*) AS n FROM shared.outbox_delivery"
            " WHERE consumer_name = 'assistant_worker' AND status = 'dead_letter'"
        )
        recovered = stack.requested(who)
        stack.dispatch()
        assert stack.request_row(recovered)["state"] == "ready"
        return {
            "circuit_states": states,
            "probes": probes,
            "probe_calls": len(provider.probes),
            "calls": provider.calls,
            "dead_letters": dead[0]["n"],
            "signature_status": signed.status_code,
            "outage_seconds": 180,
            "probe_seconds": 60,
            "request_after_recovery": "ready",
        }


def email_outage(spec: dict[str, Any]) -> dict[str, Any]:
    """Correo y motor real, reloj inyectado, con el mismo doble en todo el proceso."""
    import uuid
    from datetime import timedelta

    from tests.integration.conftest import PostgresEndpoint
    from tests.notification_support import Senders, notification_world
    from vigia_platform.identity.application.invitations import InvitationEmail
    from vigia_platform.notifications.adapters.ses.fake import FakeEmailSender
    from vigia_platform.notifications.reader import NotificationReader
    from vigia_platform.shared.context import Role, ScopeLevel

    with notification_world(PostgresEndpoint(**spec["endpoint"]), "fs_laz_02") as world:
        org, plant, zone = world.new_organization()
        member = world.add_member(org, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone)
        world.add_member(org, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, org)
        sender = FakeEmailSender(clock=world.clock)
        sender.fail_for(timedelta(minutes=5))
        event = world.event(
            org,
            "review_overdue",
            {
                "zone_id": str(zone),
                "detection_id": str(uuid.uuid4()),
                "review_due_at": world.clock.now().isoformat(),
                "reminders_sent": 1,
                "phase": "first",
            },
            plant_id=plant,
        )
        message = InvitationEmail(
            org, member.user_id, "sintetico@example.test", "Prueba", "Contenido sintético", ()
        )
        assert world.run(sender.send(world.event_context(event), message)) == "unavailable"
        assert sender.circuit_state() == "open"
        engine = world.engine(senders=Senders(sender))
        assert world.deliver(engine, event) == 1
        assert world.deliver(engine, event) == 0
        deliveries = world.deliveries(event.event_id)
        assert len(deliveries) == 2
        assert {(d["channel"], d["status"]) for d in deliveries} == {
            ("in_app", "sent"),
            ("email", "degraded_app_only"),
        }
        reader = NotificationReader(
            database=world.database,
            authorizer=world.directory._deps.authorizer,
            clock=world.clock,
            provider_organization_id=world.provider_id,
        )
        page = world.run(reader.list_own(world.member_context(member)))
        assert len(page.items) == 1 and page.items[0].email_status == "degraded_app_only"
        integrity = world.event(
            org,
            "integrity_compromised",
            {
                "chain_kind": "ledger",
                "first_failed_sequence": 1,
                "verification_mode": "full",
                "detected_at": world.clock.now().isoformat(),
            },
        )
        world.deliver(engine, integrity)
        emails = world.emails(integrity.event_id)
        assert emails and all(row["status"] == "queued" for row in emails)
        assert sender.circuit_state() == "open"
        world.clock.advance(300)
        assert world.run(sender.send(world.event_context(event), message)) == "queued"
        return {
            "outage_seconds": 300,
            "notifications": len(world.notifications(event.event_id)),
            "email_status": page.items[0].email_status,
            "integrity_queued": len(emails),
            "circuit_after_recovery": sender.circuit_state(),
        }


def export_attempt(spec: dict[str, Any]) -> dict[str, Any]:
    """Un intento en otro proceso: worker/lease/ZIP/S3 reales y muerte no capturable por fase."""
    import asyncio
    import os
    import signal
    import uuid
    from datetime import datetime

    from tests.export_stream_support import (
        MemoryExportStore,
        RecordingSink,
        StreamingComposer,
        SyntheticArtifact,
        SyntheticScope,
    )
    from tests.export_support import CountingCheckpoints, RecordingAlarm
    from tests.integration.conftest import LocalStackEndpoint
    from tests.resilience.processes import _append
    from tests.writer_support import unit_context
    from vigia_platform.accreditation.export.lease import SqlExportLeaseStore
    from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
    from vigia_platform.accreditation.export.worker import ExportBusy, ExportWorker
    from vigia_platform.shared.clock import SimulatedClock
    from vigia_platform.shared.context import ActorKind, ActorUnit
    from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode

    clock = SimulatedClock(datetime.fromisoformat(spec["now"]))
    scope = SyntheticScope(
        tuple(
            SyntheticArtifact(f"data/{index}.json", "findings_json", spec["artifact_bytes"])
            for index in range(4)
        )
    )
    storage = resilience_storage(LocalStackEndpoint(spec["s3_url"]), spec["bucket"], clock)

    class Sink:
        def __init__(self, real: Any) -> None:
            self.real = real

        async def upload_started(self, upload_id: str) -> None:
            await self.real.upload_started(upload_id)

        async def artifact_started(self, artifact: str) -> None:
            await self.real.artifact_started(artifact)
            _append(spec["progress_log"], {"event": "started", "artifact": artifact})

        async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
            await self.real.artifact_finished(artifact, done, total)
            percent = done * 100 // total
            _append(spec["progress_log"], {"event": "progress", "percent": percent})
            if percent == spec.get("kill_percent"):
                os.kill(os.getpid(), signal.SIGKILL)

    class Composer(StreamingComposer):
        async def compose(self, job: Any, sink: Any) -> Any:
            return await super().compose(job, Sink(sink))

    class Stream(ExportStream):
        async def abort_previous_upload(self, location: Any, upload_id: str | None) -> None:
            await super().abort_previous_upload(location, upload_id)
            _append(spec["progress_log"], {"event": "abort", "upload_id": upload_id})

    async def execute() -> dict[str, Any]:
        database = Database.create(
            DatabaseSettings(
                url=spec["database_url"],
                process=ProcessKind.WORKER,
                sslmode=SslMode.DISABLE,
                lock_timeout_ms=60_000,
            )
        )
        try:
            settings = ExportStreamSettings(part_size=5 * 1024 * 1024)
            stream = Stream(store=storage, settings=settings)
            composer = Composer(stream, scope)
            worker = ExportWorker(
                leases=SqlExportLeaseStore(database=database),
                checkpoints=CountingCheckpoints(),
                uploads=stream,
                composer=composer,
                alarm=RecordingAlarm(),
                clock=clock,
            )
            context = unit_context(
                uuid.UUID(spec["organization_id"]), ActorUnit.U04, kind=ActorKind.SYSTEM
            )
            try:
                await worker.run(context, uuid.UUID(spec["export_id"]))
            except ExportBusy:
                return {"busy": True}
            if not composer.jobs:
                return {"busy": False, "already_ready": True}
            reference = StreamingComposer(
                ExportStream(store=MemoryExportStore(), settings=settings), scope
            )
            await reference.compose(composer.jobs[0], RecordingSink())
            return {
                "busy": False,
                "archive_sha256": composer.results[0].archive_sha256,
                "reference_sha256": reference.results[0].archive_sha256,
                "attempt": composer.jobs[0].attempt,
            }
        finally:
            await database.dispose()

    return asyncio.run(execute())


@dataclass
class SelectiveProjectionDelivery:
    """Entrega real, salvo los nombres de evento que el escenario decide perder."""

    world: LoopWorld
    consumer: LoopProjectionConsumer
    dropped_names: frozenset[str]
    dropped: list[OutboxEvent] = field(default_factory=list)

    def deliver(self, event: OutboxEvent) -> int:
        if event.event_name in self.dropped_names:
            self.dropped.append(event)
            return 0
        return self.world.deliver(self.consumer, event)
