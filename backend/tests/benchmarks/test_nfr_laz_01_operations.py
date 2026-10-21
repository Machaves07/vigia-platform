"""Las filas de NFR-LAZ-01 sobre servicios reales; absolutos = tendencia (A-65).

Los otros dos bancos son validadores y evaluate_deadlines, reutilizados de sus
tareas de origen. Ningún banco cronometra una respuesta idempotente vacía.
Decisión del dueño (21-10): 20 rondas y densidad acotada en ci y nightly;
318 hallazgos semanales en 3 de 20 zonas, año de 366 días. El volumen completo
y la exportación máxima completa se miden en VIG-246, no en estos bancos.
El mundo anual usa ledger.bench_now() en el disparador y los DEFAULT de
recepción/auditoría: lectura SQL SECURITY DEFINER por fila incluida en la medida.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import settings

from tests.benchmarks.conftest import Measure
from tests.benchmarks.laz_support import LazBench, TimedPort, Traces, measured
from tests.export_stream_support import RecordingSink
from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from tests.loop_detail_support import detail_stack
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.timeline import zone_timeline
from vigia_platform.accreditation.monthly.repository import ZoneRef
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.loop.application.inbox import InboxFilters, InboxService
from vigia_platform.loop.application.review_queue import ReviewQueueService
from vigia_platform.loop.calendar import period_of
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.loop.http.client_factory import EgressAllowlist, HttpClientFactory
from vigia_platform.loop.projection.rebuild import ProjectionRebuilder
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration


def test_laz_inbox_200(laz: LazBench, measure: Measure) -> None:
    """Fila 1. [objetivo propio] p95 ≤ 700 ms.

    Composición: una página de 200 desde FindingState, 318 hallazgos,
    3 zonas y 366 días, densidad acotada, 20 rondas;
    cero puertos de U-02/U-03. Tramos: total → projection_page.
    """
    service = InboxService(
        database=laz.actions.database, authorizer=laz.world.stack.authz.authorizer
    )
    context = laz.world.context(laz.organization)
    filters = InboxFilters()
    if settings.get_current_profile_name() == "nightly":
        filters = InboxFilters(zone_ids=laz.zones[:3], period_from=laz.start, period_to=laz.end)

    def operation(trace: Traces) -> None:
        result = trace.call(
            "projection_page",
            lambda: laz.run(service.page(context, filters=filters, limit=200)),
        )
        assert len(result.items) == 200

    measured(
        measure, "laz_inbox_200", 700, operation, details={**laz.dimensions(), "selected_zones": 3}
    )


def test_laz_detail(laz: LazBench, postgres_endpoint: PostgresEndpoint, measure: Measure) -> None:
    """Fila 2. [objetivo propio] p95 ≤ 1 200 ms.

    Composición: expediente auditado ≤300 ms + estándar/compuerta/nodo 3 por 5 ms
    + estado_en ≤50 ms + URL ≤100 ms. Un expediente con 2 clips, pila aparte
    casi vacía, 20 rondas; no representa el año de RNF-DES-05 (VIG-246).
    Tramos: total → audited_detail.
    """
    with detail_stack(postgres_endpoint, "bench_laz_detail") as stack:
        ingest = stack.world.stack
        original_clip = ingest._clip
        storage = S3Storage(laz.storage._settings, ingest.authz.sessions.clock)

        def clip(*args: Any) -> dict[str, Any]:
            document = original_clip(*args)
            payload = b"clip sintetico sin personas " * 4
            document.update(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
            stack.run(
                storage.put_object(
                    document["storage_key"],
                    payload,
                    "video/mp4",
                    metadata={"vigia-anonymized": "1"},
                )
            )
            return document

        with patch.object(ingest, "_clip", clip):
            subject = stack.received_with_two_clips()
        stack.detail_service._evidence = EvidenceService(
            database=stack.database, audit=ingest.authz.sessions.audit, storage=storage
        )
        stack.declare_standard(stack.world.a)
        who = stack.member(stack.world.a)

        def operation(trace: Traces) -> None:
            with trace.instrument(
                stack.detail_service,
                ("_reader", "_catalog", "_gates", "_fleet", "_coverage", "_evidence"),
            ):
                response = trace.call(
                    "audited_detail", lambda: stack.detail(who, stack.finding_state_id(subject))
                )
            assert response.status_code == 200, response.text

        measured(
            measure,
            "laz_detail",
            1_200,
            operation,
            details={"findings": 1, "clips": 2, "full_density_task": "VIG-246"},
        )


def test_laz_writes(laz: LazBench, measure: Measure) -> None:
    """Fila 3. [objetivo propio] p95 ≤ 250 ms por operación, no por suma.

    Composición: escritura U-02 ≤150 ms + ambos validadores + proyección
    atómica. Se recorren clasificar/asignar/reabrir y resolver cola.
    20 rondas; ledger.bench_now() añade una lectura SQL por fila.
    Tramos: total → classification/action/reopening/review_resolution.
    """
    stack = laz.actions
    org = laz.world.organization()
    who = stack.member(org)
    context = stack.context(who)
    assignee = stack.user(org, Role.LINE_MANAGER)
    prepared: dict[str, Any] = {}
    index = 0
    consumer, repository = laz.world.review_queue_consumer(database=stack.database)
    review = ReviewQueueService(
        database=stack.database,
        writer=stack.writer,
        authorizer=laz.world.stack.authz.authorizer,
        repository=repository,
        clock=laz.world.stack.authz.sessions.clock,
    )

    def prepare() -> None:
        nonlocal index
        operation = ("classification", "action", "reopening", "review_resolution")[index % 4]
        index += 1
        if operation == "classification":
            subject = stack.received(org)
        elif operation == "action":
            subject = stack.opened(org)
        elif operation == "reopening":
            subject, _ = stack.closed(who, assignee, org)
        else:
            subject = stack.in_review(org)
            laz.world.deliver_review_queue(
                consumer, laz.world.review_queue_event(subject), database=stack.database
            )
        prepared.update(operation=operation, subject=subject)

    def operation(trace: Traces) -> None:
        subject = prepared["subject"]
        step = prepared["operation"]
        if step == "review_resolution":
            response = trace.call(
                step,
                lambda: stack.run(
                    review.resolve(
                        context,
                        subject.subject_id,
                        outcome="review_confirmed",
                        reason_category="real_coexistence",
                        reason_text="Motivo sintético de revisión del resguardo",
                    )
                ),
            )
            assert response is not None
        elif step == "classification":
            response = trace.call(
                step,
                lambda: stack.run(
                    stack.classification.classify(
                        context,
                        stack.finding_state_id(subject),
                        outcome="confirmed",
                        reason_category="real_coexistence",
                        reason_text="Motivo sintético de revisión del resguardo",
                        reinforced_reason=False,
                    )
                ),
            )
            assert response is not None
        elif step == "action":
            response = trace.call(
                step, lambda: stack.assign(who, subject, stack.action_body(assignee))
            )
            assert response.status_code == 201, response.text
        else:
            response = trace.call(
                step, lambda: stack.reopen(who, subject, stack.reopening_body(assignee))
            )
            assert response.status_code == 201, response.text

    measured(measure, "laz_writes", 250, operation, setup=prepare)


def test_laz_close_two_attachments(laz: LazBench, measure: Measure) -> None:
    """Fila 4. [objetivo propio] p95 ≤ 500 ms.

    Composición: entrada con 2 adjuntos ya subidos, verificación real del
    almacén y firma de cierre; U-02 ≤400 ms + validadores + proyección.
    20 rondas; ledger.bench_now() añade una lectura SQL por fila.
    Tramos: total → evidence_two_attachments → signed_closure.
    """
    stack = laz.actions
    org = laz.world.organization()
    who = stack.member(org)
    assignee = stack.user(org, Role.LINE_MANAGER)
    prepared: dict[str, Any] = {}

    def prepare() -> None:
        subject, action = stack.assigned(who, assignee, org)
        # Las concesiones y la subida están fuera de la medida; el adaptador S3
        # se usa para la verificación, no un HEAD de diccionario.
        claims = [stack.uploaded(subject) for _ in range(2)]
        for key, value in stack.store.objects.items():
            if key not in prepared.get("uploaded", set()):
                laz.run(laz.storage.put_object(key, value[0], "image/png"))
        prepared.update(
            subject=subject, action=action, claims=claims, uploaded=set(stack.store.objects)
        )

    def operation(trace: Traces) -> None:
        subject = prepared["subject"]
        response = trace.call(
            "evidence_two_attachments",
            lambda: stack.add_evidence(
                who, prepared["action"], stack.evidence_body(attachments=prepared["claims"])
            ),
        )
        assert response.status_code == 201, response.text
        response = trace.call(
            "signed_closure", lambda: stack.close(who, subject, stack.closure_body())
        )
        assert response.status_code == 201, response.text

    original = stack.attachments.verifier
    stack.attachments.verifier = dataclasses.replace(original, storage=laz.storage)
    try:
        measured(measure, "laz_close_two_attachments", 500, operation, setup=prepare)
    finally:
        stack.attachments.verifier = original


def test_laz_metrics_materialize(laz: LazBench, measure: Measure) -> None:
    """Fila 7. [objetivo propio] p95 ≤ 5 000 ms.

    Composición: lectura del expediente + cálculo de siete métricas y exposición
    + persistencia de ProgramMetricSnapshot con inputs_digest; no hit de caché.
    Año de 318 hallazgos en 3 de 20 zonas, densidad acotada, 20 rondas.
    Recepción/auditoría con ledger.bench_now(), lectura SQL por fila incluida.
    Tramos: total → metrics_compute_and_store.
    """
    period = period_of(laz.start + timedelta(days=40), "America/Bogota")

    def prepare() -> None:
        # Proyección reconstruible, no expediente: fuerza cálculo en cada ronda.
        laz.actions.execute(
            "DELETE FROM accreditation.program_metric_snapshot WHERE zone_id=$1",
            laz.organization.zone_id,
        )

    def operation(trace: Traces) -> None:
        result = trace.call(
            "metrics_compute_and_store",
            lambda: laz.run(laz.metrics.snapshot(laz.context, laz.organization.zone_id, period)),
        )
        assert len(result.values) == 7 and len(result.inputs_digest) == 64

    measured(
        measure,
        "laz_metrics_materialize",
        5_000,
        operation,
        setup=prepare,
        details=laz.dimensions(),
    )


def test_laz_exposure(laz: LazBench, measure: Measure) -> None:
    """Fila 8. [objetivo propio] p95 ≤ 3 000 ms.

    Composición: episodios confirmados + historia de compuertas + cobertura del
    mes + indicador persistido. Tramos: total → exposure_with_timeline.
    Año de 318 hallazgos en 3 de 20 zonas, densidad acotada, 20 rondas;
    auditoría con ledger.bench_now(), lectura SQL por fila incluida.
    """
    period = period_of(laz.start + timedelta(days=40), "America/Bogota")

    def prepare() -> None:
        laz.actions.execute(
            "DELETE FROM accreditation.exposure_indicator WHERE zone_id=$1",
            laz.organization.zone_id,
        )

    def operation(trace: Traces) -> None:
        result = trace.call(
            "exposure_with_timeline",
            lambda: laz.run(
                laz.exposure.indicator(laz.context, zone_id=laz.organization.zone_id, period=period)
            ),
        )
        assert result is not None

    measured(measure, "laz_exposure", 3_000, operation, setup=prepare, details=laz.dimensions())


def test_laz_metrics_panel(laz: LazBench, measure: Measure) -> None:
    """Fila 9. [objetivo propio] p95 ≤ 1 500 ms.

    Composición: 20 zonas configuradas por 12 meses materializados, más la zona
    auxiliar vacía del sitio de ingesta (medición conservadora); sin recálculo.
    Año de 318 hallazgos, densidad acotada, 20 rondas; auditoría con
    ledger.bench_now(), lectura SQL por fila incluida.
    Tramos: total → materialized_month (12 peticiones en proceso).
    """
    periods = sorted(
        {period_of(laz.start + timedelta(days=31 * i), "America/Bogota") for i in range(12)}
    )
    for period in periods:
        for zone in laz.zones:
            laz.run(laz.metrics.snapshot(laz.context, zone, period))

    def operation(trace: Traces) -> None:
        for period in periods:
            result = trace.call(
                "materialized_month",
                lambda p=period: laz.run(
                    laz.metrics.read(laz.context, plant_id=laz.organization.plant_id, period=p)
                ),
            )
            assert len(result.zones) >= 20
            assert all(s.recomputed_at is None for s in result.zones)

    measured(
        measure,
        "laz_metrics_panel",
        1_500,
        operation,
        details={**laz.dimensions(), "auxiliary_empty_zones": 1, "months": len(periods)},
    )


def test_laz_timeline_366(laz: LazBench, measure: Measure) -> None:
    """Fila 10. [objetivo propio] p95 ≤ 8 000 ms.

    Composición: 12 lecturas de cobertura de ≤31 días, concatenación y fusión;
    solo worker. Tramos: total → coverage_366_days.
    Año de 318 hallazgos en 3 de 20 zonas, densidad acotada, 20 rondas;
    auditoría con ledger.bench_now(), lectura SQL por fila incluida.
    """

    def operation(trace: Traces) -> None:
        result = trace.call(
            "coverage_366_days",
            lambda: laz.run(
                zone_timeline(
                    laz.coverage, laz.context, laz.organization.zone_id, laz.start, laz.end, laz.end
                )
            ),
        )
        assert result is not None

    measured(
        measure, "laz_timeline_366", 8_000, operation, details={**laz.dimensions(), "segments": 12}
    )


def test_laz_monthly_package(laz: LazBench, measure: Measure) -> None:
    """Fila 11. [objetivo propio] p95 ≤ 90 000 ms.

    Composición: checkpoint previo + muestra estratificada + siete métricas y
    exposición + documento/manifiesto + S3 + sellado del expediente.
    Tramos: total → monthly_compose_and_seal. Zona nueva por ronda, no no-op.
    Un hallazgo por organización nueva, 20 rondas: no representa el año de
    RNF-DES-05 (VIG-246). ledger.bench_now() añade una lectura SQL por fila.
    """
    composer = laz.monthly()
    prepared: dict[str, Any] = {}
    period = period_of(laz.start + timedelta(days=40), "America/Bogota")

    def prepare() -> None:
        clock = laz.world.stack.authz.sessions.clock
        current = clock.now()
        clock.set(laz.start + timedelta(days=45))
        laz.actions.execute("UPDATE ledger.bench_clock SET at=$1", clock.now())
        org = laz.world.organization()
        laz.actions.member(org)
        subject = laz.actions.received(org)
        laz.actions.classify(subject, outcome="false_positive", category="reflection_or_glare")
        laz.actions.classify(subject, reinforced=True)
        clock.set(current)
        laz.actions.execute("UPDATE ledger.bench_clock SET at=$1", clock.now())
        prepared["org"] = org

    def operation(trace: Traces) -> None:
        org = prepared["org"]
        with trace.instrument(
            composer,
            (
                "_reader",
                "_checkpoints",
                "_evidence",
                "_writer",
                "_signer",
                "_program_metrics",
                "_repository",
            ),
        ):
            result = trace.call(
                "monthly_compose_and_seal",
                lambda: laz.run(
                    composer.compose_and_seal(
                        laz.world.event_context(org),
                        ZoneRef(
                            organization_id=org.organization_id,
                            plant_id=org.plant_id,
                            zone_id=org.zone_id,
                        ),
                        period,
                        "America/Bogota",
                        sample_percent=10,
                        sample_min=1,
                        sample_max=200,
                    )
                ),
            )
        assert result is not None

    measured(
        measure,
        "laz_monthly_package",
        90_000,
        operation,
        setup=prepare,
        details={
            "findings_per_organization": 1,
            "full_density_task": "VIG-246",
            "database_clock": "ledger.bench_now(): lectura SQL por fila",
        },
    )


def export_bench(laz: LazBench, measure: Measure, *, maximum: bool) -> None:
    composer = laz.composer()
    prepared: dict[str, Any] = {}
    zones = laz.zones[:3] if maximum else laz.zones[:1]
    end = laz.start + timedelta(days=31)

    def prepare() -> None:
        checkpoints = laz.run(
            laz.checkpoints.write_checkpoints_now(
                laz.context,
                (CheckpointChain.plant(laz.organization.plant_id), CheckpointChain.audit()),
            )
        )
        ids = tuple(c.checkpoint.entry_id for c in checkpoints if c.checkpoint is not None)
        assert ids
        prepared["job"] = ExportJob(
            export_id=uuid.uuid4(),
            organization_id=laz.organization.organization_id,
            plant_id=laz.organization.plant_id,
            zone_ids=zones,
            period_from=laz.start,
            period_to=end,
            package_form=PackageForm.WITHOUT_CLIPS,
            requested_by={
                "user_id": str(uuid.uuid4()),
                "display_name": "Coordinación sintética",
                "role_in_use": None,
            },
            correlation_id=uuid.uuid4(),
            attempt=1,
            generated_at=laz.actions.now(),
            checkpoint_record_ids=ids,
            context=laz.context,
        )

    def operation(trace: Traces) -> None:
        sink = RecordingSink()
        composer._stream._tracer = trace.tracer
        original_ports = composer._ports
        composer._ports = dataclasses.replace(
            original_ports,
            **{
                name: TimedPort(getattr(original_ports, name), trace, name)
                for name in (
                    "reader",
                    "catalog",
                    "gates",
                    "fleet",
                    "coverage",
                    "markers",
                    "checkpoints",
                    "places",
                    "blobs",
                    "episodes",
                    "signer",
                )
            },
        )
        try:
            result = trace.call(
                "compose_artifacts_and_upload",
                lambda: laz.run(composer.compose(prepared["job"], sink)),
            )
        finally:
            composer._ports = original_ports
        assert result.size_bytes > 0 and result.artifact_count > 0
        assert result.manifest["finding_count"] > 0

    measured(
        measure,
        "laz_export_maximum" if maximum else "laz_export_month",
        3_600_000 if maximum else 300_000,
        operation,
        setup=prepare,
        details={
            **laz.dimensions(),
            "zones": len(zones),
            "days": (end - laz.start).days,
            "maximum_scope_bounded": maximum,
            "median_objective_ms": None if maximum else 120_000,
        },
    )


def test_laz_export_month(laz: LazBench, measure: Measure) -> None:
    """Fila 12. [objetivo propio] mediana ≤120 s y p95 ≤300 s.

    Composición: checkpoint + expediente + catálogos por lote + cobertura +
    artefactos/documento/manifiesto firmados + multipart S3, 1 zona/31 días sin clips.
    20 rondas sobre el año acotado de 318 hallazgos; auditoría con
    ledger.bench_now(), lectura SQL por fila incluida.
    Tramos: total → compose_artifacts_and_upload (progreso por artefacto).
    """
    export_bench(laz, measure, maximum=False)


def test_laz_export_maximum(laz: LazBench, measure: Measure) -> None:
    """Fila 13. [objetivo propio] ≤60 min, progreso cada 30 s.

    Composición: mismo flujo real que el mensual para 3 zonas/31 días en
    ci y nightly; año acotado de 318 hallazgos, 20 rondas, sin clips (R5).
    El máximo completo y los bytes son de VIG-246; no se acreditan aquí.
    Auditoría con ledger.bench_now(), lectura SQL por fila incluida.
    Tramos: total → compose_artifacts_and_upload; sin compositor sintético.
    """
    export_bench(laz, measure, maximum=True)


@pytest.fixture(scope="module")
def assistant(postgres_endpoint: PostgresEndpoint) -> Any:
    with assistant_stack(postgres_endpoint, "bench_laz_assistant") as stack:
        yield stack


def test_laz_assistant(assistant: AssistantStack, measure: Measure) -> None:
    """Fila 14. [objetivo propio] p95 ≤60 s; tope funcional 120 s.

    Composición: requested → contexto permitido → proveedor doble → validadores
    → ready persistido. Tramos: total → assistant_requested_to_ready.
    20 rondas, un borrador nuevo con el proveedor doble por ronda.
    """
    prepared: dict[str, Any] = {}
    assistant.use(assistant.provider())

    def prepare() -> None:
        # Cada solicitud respeta los límites exactos sin relajar la guarda.
        assistant.clock.advance(3_601)
        who = assistant.coordinator()
        prepared["id"] = assistant.requested(who)

    def operation(trace: Traces) -> None:
        trace.call("assistant_requested_to_ready", lambda: assistant.deliver(prepared["id"]))
        assert assistant.request_row(prepared["id"])["state"] == "ready"

    measured(
        measure, "laz_assistant", 60_000, operation, setup=prepare, details={"provider": "double"}
    )


@pytest.fixture(scope="module")
def notifications(postgres_endpoint: PostgresEndpoint) -> Any:
    with notification_world(postgres_endpoint, "bench_laz_notifications") as world:
        yield world


def test_laz_notification(notifications: NotificationWorld, measure: Measure) -> None:
    """Fila 15. [objetivo propio] p95 ≤10 s.

    Composición: entrega outbox (objetivo U-02 ≤5 s) + identidad + plantilla +
    Notification y Delivery persistidos. Tramos: total → event_to_notification.
    El reloj de pared mide el motor; la espera de entrega se declara, no se simula durmiendo.
    20 rondas, un evento nuevo por ronda; densidad acotada.
    """
    world = notifications
    org, plant, zone = world.new_organization()
    world.add_member(org, Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION, org)
    engine = world.engine()
    prepared: dict[str, Any] = {}

    def prepare() -> None:
        prepared["event"] = world.event(
            org,
            "finding_received",
            {
                "zone_id": str(zone),
                "finding_id": str(uuid.uuid4()),
                "platform_record_id": str(uuid.uuid4()),
                "family": "coexistence",
                "tier": "tier_1",
            },
            plant_id=plant,
        )

    def operation(trace: Traces) -> None:
        count = trace.call(
            "event_to_notification", lambda: world.deliver(engine, prepared["event"])
        )
        assert count > 0 and world.notifications(prepared["event"].event_id)

    measured(
        measure,
        "laz_notification",
        10_000,
        operation,
        setup=prepare,
        details={"outbox_delivery_budget_ms": 5_000},
    )


class AcceptingSes:
    def send_email(self, **kwargs: Any) -> dict[str, str]:
        return {"MessageId": str(uuid.uuid4())}


def test_laz_immediate_email(notifications: NotificationWorld, measure: Measure) -> None:
    """Fila 16. [objetivo propio] p95 ≤60 s.

    Composición: admisión queued + cliente SES doble aceptando + sent persistido;
    sin red al proveedor real. Tramos: total → queued_to_provider_accepted.
    20 rondas con un correo nuevo por ronda; densidad acotada.
    """
    world = notifications
    org, _, _ = world.new_organization()
    user = world.add_member(org, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, org)
    context = world.event_context(world.event(org, "dummy", {}))
    sender = SesEmailSender(
        database=world.database,
        http_clients=HttpClientFactory(
            EgressAllowlist(
                assistant_host="api.anthropic.com", email_host="email.us-east-1.amazonaws.com"
            )
        ),
        region_name="us-east-1",
        sender_email="no-responder@mail.vigia.test",
        configuration_set_name="vigia-email-test",
        clock=world.clock,
    )
    sender._client = AcceptingSes()
    message = InvitationEmail(
        organization_id=org,
        to_user_id=user.user_id,
        to_email="sintetico@example.test",
        subject_es="Aviso inmediato",
        body_es="Verificar el resguardo declarado de la zona.",
        related_ids=(uuid.uuid4(),),
    )

    def operation(trace: Traces) -> None:
        result = trace.call(
            "queued_to_provider_accepted", lambda: world.run(sender.send(context, message))
        )
        assert result == "queued"

    measured(
        measure,
        "laz_immediate_email",
        60_000,
        operation,
        setup=lambda: world.clock.advance(3_601),
        details={"provider": "double"},
    )


def test_laz_rebuild_year(laz: LazBench, measure: Measure) -> None:
    """Fila 17. [objetivo propio] ≤30 min.

    Composición: arrendamiento y aviso rebuilding + lectura del año de expediente
    + pliegue y enriquecimiento reales + reemplazo de proyección + healthy.
    Tramos: total → rebuild_plant. El expediente no se borra ni altera.
    Año de 318 hallazgos en 3 de 20 zonas, densidad acotada, 20 rondas;
    auditoría con ledger.bench_now(), lectura SQL por fila incluida.
    """
    rebuilder = ProjectionRebuilder(
        database=laz.actions.database,
        catalog=laz.catalog.catalog,
        gates=laz.catalog.gates,
        coverage=laz.coverage,
        markers=EvidenceService(
            database=laz.actions.database,
            audit=laz.world.stack.authz.audit,
            storage=laz.world.stack.storage,
        ),
        settings=PostgresLoopConfigurationRepository(laz.actions.database),
        clock=laz.world.stack.authz.sessions.clock,
    )

    def operation(trace: Traces) -> None:
        result = trace.call(
            "rebuild_plant",
            lambda: laz.run(rebuilder.rebuild_plant(laz.context, laz.organization.plant_id)),
        )
        assert result.findings_written > 0
        assert laz.world.checkpoint(laz.organization)["state"] == "healthy"

    measured(measure, "laz_rebuild_year", 1_800_000, operation, details=laz.dimensions())
