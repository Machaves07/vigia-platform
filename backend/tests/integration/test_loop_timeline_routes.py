"""La ficha de vida del hallazgo: ``GET /loop/findings/{finding_state_id}/timeline`` (TASK-585;
O-11 y O-12; BR-LAZ-01, 06, 09, 12, 39, 46 y 112).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real: la pila de acciones
(``tests/loop_actions_support``: clasificación, acción, evidencia, cierre y reapertura por sus
rutas y servicios reales, con la política de texto libre de producción) más el detalle con los
puertos reales de U-03 contados (``DetailPorts``) y la ficha de vida con ``LectorExpediente``.

- **Ciclo completo**: recibido, clasificación, acción, dos evidencias, vencimiento, cierre
  firmado, causa raíz y reapertura (con su acción nueva): un hito por registro del sujeto, en
  orden de expediente, con su ``record_id`` (oráculo: el expediente leído como superusuario).
- **Una sola auditoría**: cada petición deja exactamente **una** entrada de auditoría
  (``ledger_detail_read``, con el hallazgo como recurso), también con 50 evidencias; y a la vez,
  cada una de cinco peticiones simultáneas deja la suya.
- **Alcance**: zona hermana, otra planta, otra organización, sin ``findings.read``, el
  instalador bajo concesión y una zona sin compuerta de uso aprobada en el instante del hecho:
  ``not_found`` igual que un hallazgo inexistente, sin leer el expediente ni auditar una
  lectura; sin compuerta legible, el 503 del detalle. Con alcance de la zona o de la planta, 200.
- **Unipersonal**: todas las horas y los tiempos entre hitos salen de las horas agregadas.
- **Plazo de clasificación** (el caso del 22-11): la configuración creada **después** de la
  clasificación no cambia el plazo; ``met`` coincide con el numerador de la métrica de BR-LAZ-112
  sobre el mismo expediente.

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14); topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, RealPorts
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from tests.loop_classification_support import SAME_ORIGIN
from tests.loop_detail_support import DetailEvidenceStore, DetailPorts
from vigia_platform.accreditation.metrics.compute import compute_metrics
from vigia_platform.accreditation.metrics.model import MetricValue
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService as LedgerEvidence
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.loop.adapters.http.attachment_routes import ATTACHMENTS_STATE_KEY
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.http.timeline_routes import TIMELINE_PATH
from vigia_platform.loop.application.detail import FindingDetailService
from vigia_platform.loop.application.timeline import FindingTimelineService
from vigia_platform.loop.calendar import month_bounds, period_of
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import MetricKey, MetricStatus, MetricUnit
from vigia_platform.loop.domain.time_aggregation import GRID_ORIGIN
from vigia_platform.shared.api.errors import DEFAULT_RETRY_AFTER_SECONDS
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

_WINDOW: Final = 60
_STRIP: Final = frozenset({"correlation_id"})
_CONFIGURATION_REASON: Final = "Ajuste del plazo acordado en el comité de seguridad de la planta"
_ANALYSIS: Final = {
    "root_cause_category": "engineering",
    "root_cause_statement": "El trazado de la zona mezcla los flujos de carga y de paso",
    "conclusion": "Separar físicamente los flujos con un resguardo fijo en el acceso",
}


@dataclass
class TimelineStack:
    actions: ActionsStack
    ports: DetailPorts
    client: httpx.AsyncClient

    def get(self, who: Who, finding_state_id: uuid.UUID | str) -> httpx.Response:
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        path = TIMELINE_PATH.format(finding_state_id=finding_state_id)
        response: httpx.Response = self.actions.run(self.client.get(path, headers=headers))
        return response

    def audit_entries(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """**Todas** las entradas de auditoría de la organización, en orden de la cadena."""
        return [
            dict(row)
            for row in self.actions.fetch(
                "SELECT operation, outcome, actor_id, scope_plant_id, scope_zone_id,"
                " resource_kind, resource_id, result_count FROM shared.audit_entry"
                " WHERE organization_id = $1 ORDER BY chain_sequence",
                organization.organization_id,
            )
        ]

    def occupancy(self, finding_state_id: uuid.UUID, single: bool) -> None:
        self.actions.execute(
            "UPDATE loop.finding_state SET single_occupancy_zone = $2,"
            " aggregation_window_minutes = $3 WHERE finding_state_id = $1",
            finding_state_id,
            single,
            _WINDOW,
        )


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[TimelineStack]:
    with actions_stack(postgres_endpoint, "loop_timeline_routes") as built:
        world = built.world
        authz = world.stack.authz
        clock = authz.sessions.clock
        audit = authz.sessions.audit
        database = built.database
        queries = catalog_query_ports(database)
        fleet = FleetInventory(
            database=database,
            authorizer=authz.authorizer,
            audit=audit,
            clock=clock,
            provider_organization_id=authz.provider_organization_id,
        )
        coverage = CoverageService(database=database, audit=audit, clock=clock)
        ports = DetailPorts(
            wrapped=RealPorts(
                catalog=queries.catalog, gates=queries.gates, coverage=coverage, fleet=fleet
            )
        )
        detail = FindingDetailService(
            database=database,
            authorizer=authz.authorizer,
            reader=LectorExpediente(database=database, audit=audit, clock=clock),
            evidence=LedgerEvidence(
                database=database,
                audit=audit,
                storage=DetailEvidenceStore(world.stack.storage, clock),
            ),
            catalog=ports,
            gates=ports,
            fleet=ports,
            coverage=ports,
        )
        timeline = FindingTimelineService(
            subjects=detail,
            reader=LectorExpediente(database=database, audit=audit, clock=clock),
            timezones=PostgresLoopConfigurationRepository(database),
        )
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(built.writer),
                    clock=clock,
                ),
                "state": {
                    LOOP_STATE_KEY: LoopHttp(
                        configuration=built.configuration,
                        classification=built.classification,
                        actions=built.actions,
                        evidence=built.evidence,
                        closure=built.closure,
                        reopening=built.reopening,
                        detail=detail,
                        timeline=timeline,
                    ),
                    ATTACHMENTS_STATE_KEY: built.attachments,
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        # Las peticiones de la pila de acciones van también por esta aplicación.
        actions = dataclasses.replace(built, client=client)
        try:
            yield TimelineStack(actions, ports, client)
        finally:
            built.run(client.aclose())


@pytest.fixture(autouse=True)
def _healthy(stack: TimelineStack) -> Iterator[None]:
    stack.ports.reset()
    yield
    stack.ports.reset()


@pytest.fixture
def coordinator(stack: TimelineStack) -> Who:
    return stack.actions.member(stack.actions.world.a)


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _without(response: httpx.Response) -> tuple[int, dict[str, Any]]:
    return response.status_code, {k: v for k, v in response.json().items() if k not in _STRIP}


def _oracle(stack: TimelineStack, subject: IngestedSubject) -> list[tuple[str, str]]:
    """``(record_id, record_type)`` de cada registro del sujeto, en orden de cadena."""
    records = [
        (str(subject.record_id), "finding_received"),
        *[
            (str(r["record_id"]), str(r["record_type"]))
            for r in stack.actions.subject_records(subject)
        ],
    ]
    return records


def _analysis(stack: TimelineStack, subject: IngestedSubject) -> None:
    """``root_cause_analysis`` firmado por el escritor real (la ruta guiada la prueba TASK-315)."""
    world = stack.actions.world.stack
    stack.actions.write_record(
        subject,
        "root_cause_analysis",
        {
            "analysis_id": str(world.uuid7()),
            "subject_kind": subject.subject_kind,
            "subject_id": str(subject.subject_id),
            "anchor_record_id": str(subject.record_id),
            "why_steps": [
                {
                    "step_index": 1,
                    "statement": "La carretilla cruzó el pasillo peatonal con la estación activa",
                    "evidence_refs": [],
                }
            ],
            "cause_effect_branches": [],
            **_ANALYSIS,
            "state": "signed",
            "assistant": {"used": False},
            "signer": {
                "user_id": str(world.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "professional_license": "LIC-SST-0001",
            },
            "signed_at": format_timestamp(world.now()),
        },
    )


def _full_cycle(stack: TimelineStack, coordinator: Who) -> tuple[IngestedSubject, uuid.UUID]:
    """Recibido → clasificación → acción → 2 evidencias → vencimiento → cierre firmado → causa
    raíz → reapertura (con su acción nueva), todo por los caminos reales."""
    actions = stack.actions
    assignee = actions.user(actions.world.a)
    finding, action_id = actions.with_evidence(coordinator, assignee)
    second = actions.add_evidence(coordinator, action_id, actions.evidence_body())
    assert second.status_code == 201, second.text
    (row,) = actions.action_rows(finding)
    actions.write_overdue(finding, row)
    closed = actions.close(coordinator, finding, actions.closure_body())
    assert closed.status_code == 201, closed.text
    _analysis(stack, finding)
    reopened = actions.reopen(coordinator, finding, actions.reopening_body(assignee))
    assert reopened.status_code == 201, reopened.text
    return finding, action_id


# --- el ciclo completo ---------------------------------------------------------------------------


def test_the_full_cycle_has_one_milestone_per_record_in_ledger_order(
    stack: TimelineStack, coordinator: Who
) -> None:
    finding, action_id = _full_cycle(stack, coordinator)
    finding_state_id = stack.actions.finding_state_id(finding)
    stack.occupancy(finding_state_id, False)
    body = _ok(stack.get(coordinator, finding_state_id))
    milestones = body["milestones"]
    assert [(m["record_id"], m["record_type"]) for m in milestones] == _oracle(stack, finding)
    assert [m["kind"] for m in milestones] == [
        "finding_received",
        "classification",
        "action_assigned",
        "action_evidence_added",
        "action_evidence_added",
        "action_overdue",
        "closure",
        "root_cause_analysis",
        "reopening",
        "action_assigned",
    ]
    sequences = [m["chain_sequence"] for m in milestones]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert body["finding_state_id"] == str(finding_state_id)
    assert body["lifecycle_state"] == "action_assigned"
    assert body["truncated"] is False
    # Plazos: la acción vencida antes del cierre, fuera de plazo; la nueva, abierta.
    actions = [m for m in milestones if m["kind"] == "action_assigned"]
    assert [a["deadline"]["met"] for a in actions] == [False, None]
    (first,) = [m for m in milestones if m["kind"] == "classification"]
    # El valor depende del reloj compartido del módulo; el resumen dice lo mismo que el hito (la
    # regla y su borde los prueban tests/unit/test_loop_timeline.py y los dos casos del 22-11).
    assert isinstance(first["deadline"]["met"], bool)
    assert body["summary"]["classification_met"] is first["deadline"]["met"]
    # Quién: el rol en uso de quien clasificó, asignó y reabrió; nadie en lo del nodo y la tarea.
    by_kind = {m["kind"]: m for m in milestones}
    assert by_kind["finding_received"]["actor"] is None
    assert by_kind["action_overdue"]["actor"] is None
    assert by_kind["classification"]["actor"]["role_in_use"] == "coordinator_sst"
    assert by_kind["closure"]["details"]["professional_license"] == "LIC-SST-0001"
    assert by_kind["root_cause_analysis"]["details"]["root_cause_category"] == "engineering"
    assert actions[0]["details"]["action_type"] == "engineering"
    assert by_kind["reopening"]["details"]["from_state"] == "closed"
    assert body["summary"]["closed_at"] is None  # reabierto
    assert str(action_id) not in {a["record_id"] for a in actions}


# --- una sola auditoría por petición -------------------------------------------------------------


def _evidence(stack: TimelineStack, subject: IngestedSubject, action_id: uuid.UUID) -> None:
    """Una entrada ``action_evidence_added`` por el escritor real (la ruta limita a 20 por acción;
    aquí se prueba la lectura con muchas)."""
    world = stack.actions.world.stack
    stack.actions.tick()
    # Una entrada de evidencia no mueve el estado (no se «aplica»): basta con que se escriba.
    receipt, _ = stack.actions.world.write(
        subject.organization,
        "action_evidence_added",
        {
            "evidence_entry_id": str(world.uuid7()),
            "action_id": str(action_id),
            "subject_id": str(subject.subject_id),
            "description": "Resguardo fijo revisado y anclado en el acceso de la zona",
            "performed_at": format_timestamp(world.now() - timedelta(hours=1)),
            "performed_by_role": "Técnico de mantenimiento",
            "attachments": [],
            "added_by": {
                "user_id": str(world.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "role_in_use": "coordinator_sst",
            },
            "added_at": format_timestamp(world.now()),
        },
    )
    assert isinstance(receipt, Receipt), receipt


def _one_entry(
    stack: TimelineStack, who: Who, subject: IngestedSubject, expected_count: int
) -> dict[str, Any]:
    organization = subject.organization
    finding_state_id = stack.actions.finding_state_id(subject)
    before = stack.audit_entries(organization)
    body = _ok(stack.get(who, finding_state_id))
    after = stack.audit_entries(organization)
    (entry,) = after[len(before) :]
    assert entry["operation"] == "ledger_detail_read"
    assert entry["outcome"] == "success"
    assert (entry["resource_kind"], entry["resource_id"]) == ("finding", finding_state_id)
    assert entry["scope_zone_id"] == organization.zone_id
    assert entry["result_count"] == expected_count == len(body["milestones"])
    assert entry["actor_id"] == stack.actions.context(who).actor.id
    return body


def test_each_request_leaves_exactly_one_audit_entry_also_with_fifty_evidences(
    stack: TimelineStack, coordinator: Who
) -> None:
    actions = stack.actions
    finding, action_id = actions.assigned(coordinator, actions.user(actions.world.a))
    _one_entry(stack, coordinator, finding, 3)
    for _ in range(50):
        _evidence(stack, finding, action_id)
    body = _one_entry(stack, coordinator, finding, 53)
    assert [m["kind"] for m in body["milestones"]].count("action_evidence_added") == 50


def test_concurrent_requests_leave_one_audit_entry_each(
    stack: TimelineStack, coordinator: Who
) -> None:
    finding, _ = stack.actions.assigned(coordinator, stack.actions.user(stack.actions.world.a))
    finding_state_id = stack.actions.finding_state_id(finding)
    organization = finding.organization
    before = len(stack.audit_entries(organization))
    cookie, _ = coordinator
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    path = TIMELINE_PATH.format(finding_state_id=finding_state_id)

    async def together() -> list[httpx.Response]:
        return list(
            await asyncio.gather(*(stack.client.get(path, headers=headers) for _ in range(5)))
        )

    responses = stack.actions.run(together())
    assert [r.status_code for r in responses] == [200] * 5
    bodies = [r.json()["milestones"] for r in responses]
    assert all(b == bodies[0] for b in bodies)
    added = stack.audit_entries(organization)[before:]
    assert [(e["operation"], e["resource_id"]) for e in added] == [
        ("ledger_detail_read", finding_state_id)
    ] * 5


# --- alcance -------------------------------------------------------------------------------------


def _denied(stack: TimelineStack, who: Who, finding: IngestedSubject) -> None:
    finding_state_id = stack.actions.finding_state_id(finding)
    organization = finding.organization
    reads = [
        e
        for e in stack.audit_entries(organization)
        if e["operation"] == "ledger_detail_read" and e["resource_id"] == finding_state_id
    ]
    stack.ports.reset()
    response = stack.get(who, finding_state_id)
    missing = stack.get(who, uuid.uuid4())
    assert _without(response) == _without(missing)
    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert stack.ports.calls == {}
    assert [
        e
        for e in stack.audit_entries(organization)
        if e["operation"] == "ledger_detail_read" and e["resource_id"] == finding_state_id
    ] == reads


def _member(stack: TimelineStack, role: Role, level: ScopeLevel, scope: uuid.UUID) -> Who:
    return stack.actions.member(stack.actions.world.a, role, level, scope)


def test_findings_read_on_the_sibling_zone_does_not_see_the_timeline(stack: TimelineStack) -> None:
    finding = stack.actions.received()
    site = finding.organization.site
    who = _member(stack, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id)
    _denied(stack, who, finding)


def test_findings_read_on_another_plant_does_not_see_the_timeline(stack: TimelineStack) -> None:
    finding = stack.actions.received()
    plant = stack.actions.world.add_plant(finding.organization)
    _denied(stack, _member(stack, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant), finding)


def test_another_organization_without_the_key_or_under_concession_does_not_see_it(
    stack: TimelineStack,
) -> None:
    actions = stack.actions
    finding = actions.received()
    _denied(stack, actions.member(actions.world.b), finding)
    _denied(stack, actions.member(actions.world.a, Role.LINE_MANAGER), finding)
    _denied(stack, actions.member(actions.world.a, Role.ADMINISTRATOR), finding)
    _denied(stack, actions.installer(actions.world.a), finding)


def test_a_zone_without_an_approved_usage_gate_at_the_event_does_not_show_it(
    stack: TimelineStack, coordinator: Who
) -> None:
    finding = stack.actions.received()
    finding_state_id = stack.actions.finding_state_id(finding)
    for status in ("pending", "revoked", None):
        stack.ports.reset()
        stack.ports.gate_status = status
        before = len(stack.audit_entries(finding.organization))
        response = stack.get(coordinator, finding_state_id)
        missing = stack.get(coordinator, uuid.uuid4())
        assert _without(response) == _without(missing)
        assert response.status_code == 404
        assert len(stack.audit_entries(finding.organization)) == before


def test_without_a_readable_gate_the_timeline_fails_closed_with_the_detail_503(
    stack: TimelineStack, coordinator: Who
) -> None:
    finding = stack.actions.received()
    stack.ports.down.add("state_at")
    before = len(stack.audit_entries(finding.organization))
    response = stack.get(coordinator, stack.actions.finding_state_id(finding))
    assert response.status_code == 503, response.text
    body = response.json()
    assert body == {
        "code": "temporarily_unavailable",
        "detail_code": "loop_gate_state_unavailable",
        "message_es": PlatformLabels.load().label("api_error_code", "temporarily_unavailable"),
        "correlation_id": body["correlation_id"],
        "retry_after_seconds": DEFAULT_RETRY_AFTER_SECONDS,
    }
    assert dict(stack.ports.calls) == {"state_at": 1}
    assert len(stack.audit_entries(finding.organization)) == before


def test_findings_read_on_the_zone_or_the_plant_of_the_finding_sees_it(
    stack: TimelineStack,
) -> None:
    finding = stack.actions.received()
    site = finding.organization.site
    for who in (
        _member(stack, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id),
        _member(stack, Role.PLANT_MANAGER, ScopeLevel.PLANT, site.plant_id),
    ):
        body = _ok(stack.get(who, stack.actions.finding_state_id(finding)))
        assert [m["record_id"] for m in body["milestones"]] == [str(finding.record_id)]


def test_an_unknown_query_parameter_is_invalid(stack: TimelineStack, coordinator: Who) -> None:
    finding = stack.actions.received()
    path = TIMELINE_PATH.format(finding_state_id=stack.actions.finding_state_id(finding))
    cookie, _ = coordinator
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    response = stack.actions.run(
        stack.client.get(f"{path}?zone_id={uuid.uuid4()}", headers=headers)
    )
    assert (response.status_code, response.json()["code"]) == (400, "invalid_request")


def test_the_response_is_not_stored(stack: TimelineStack, coordinator: Who) -> None:
    finding = stack.actions.received()
    response = stack.get(coordinator, stack.actions.finding_state_id(finding))
    assert response.headers["cache-control"] == "no-store"


# --- unipersonal ---------------------------------------------------------------------------------


def _on_grid(stamp: str) -> bool:
    moment = datetime.fromisoformat(stamp)
    return (moment - GRID_ORIGIN) % timedelta(minutes=_WINDOW) == timedelta(0)


def test_in_a_single_occupancy_zone_every_time_and_gap_is_aggregated(
    stack: TimelineStack, coordinator: Who
) -> None:
    finding, _ = _full_cycle(stack, coordinator)
    finding_state_id = stack.actions.finding_state_id(finding)
    stack.occupancy(finding_state_id, True)
    body = _ok(stack.get(coordinator, finding_state_id))
    assert (body["time_aggregated"], body["aggregation_window_minutes"]) == (True, _WINDOW)
    milestones = body["milestones"]
    marks = [
        *[m["at"] for m in milestones],
        *[m["deadline"]["due_at"] for m in milestones if m["deadline"] is not None],
        *[
            m["details"][key]
            for m in milestones
            for key in ("due_at", "performed_at", "review_due_at", "detected_at")
            if m["details"][key] is not None
        ],
        *[v for v in (body["summary"]["received_at"], body["summary"]["closed_at"]) if v],
    ]
    assert len(marks) > len(milestones)
    assert all(_on_grid(mark) for mark in marks), marks
    window_ms = _WINDOW * 60_000
    gaps = [m["elapsed_ms_since_previous"] for m in milestones[1:]]
    assert all(gap is not None and gap % window_ms == 0 for gap in gaps), gaps


# --- el plazo de clasificación: el caso del 22-11 ------------------------------------------------


def _metric_numerator(stack: TimelineStack, organization: LoopOrganization) -> tuple[int, int]:
    """El numerador y el denominador de BR-LAZ-112 sobre el expediente real de la planta."""
    records = stack.actions.world.ledger_records(organization)
    (reception,) = [r for r in records if r.record_type == "finding_received"]
    (row,) = stack.actions.fetch(
        "SELECT timezone FROM identity.plant WHERE plant_id = $1", organization.plant_id
    )
    timezone = str(row["timezone"])
    start, end = month_bounds(timezone, period_of(reception.received_at, timezone))
    later = max(r.received_at for r in records) + timedelta(days=60)
    previous = start - timedelta(days=31)
    values = compute_metrics(
        records,
        organization_id=organization.organization_id,
        plant_id=organization.plant_id,
        zone_id=organization.zone_id,
        start=start,
        end=max(end, later),
        previous_start=previous,
        cutoff=max(end, later),
        timezone=timezone,
        approved=True,
        adoption_count=0,
        exposure=MetricValue(
            key=MetricKey.EXPOSURE,
            status=MetricStatus.COMPUTED,
            unit=MetricUnit.MINUTES,
            value=0.0,
            excluded_unclosable_count=0,
        ),
    )
    (metric,) = [v for v in values if v.key is MetricKey.CLASSIFIED_IN_TIME]
    assert metric.numerator is not None and metric.denominator is not None
    return metric.numerator, metric.denominator


def test_a_configuration_created_after_the_classification_never_moves_its_deadline(
    stack: TimelineStack,
) -> None:
    """El 22-11: la configuración del lazo de la planta nació dos minutos después de la
    clasificación. Aquí la clasificación llega ocho días después (fuera de los 3 días hábiles de
    fábrica) y la configuración, creada después, da 30: la ficha sigue diciendo «fuera de plazo»
    y coincide con la métrica."""
    actions = stack.actions
    organization = actions.world.organization()  # una planta sin configuración del lazo
    finding = actions.received(organization)
    actions.tick(8 * 24 * 3600)
    actions.classify(finding)
    actions.tick(167)
    actions.run(
        actions.configuration.change(
            actions.context(actions.member(organization, Role.PLANT_MANAGER)),
            organization.plant_id,
            integers={"classification_deadline_business_days": 30},
            business_calendar=None,
            reason=_CONFIGURATION_REASON,
        )
    )
    who = actions.member(organization)
    body = _ok(stack.get(who, actions.finding_state_id(finding)))
    (classification,) = [m for m in body["milestones"] if m["kind"] == "classification"]
    assert classification["deadline"]["met"] is False
    assert body["summary"]["classification_met"] is False
    # Los dos cambios de configuración (la versión 1 de fábrica y la 2) son posteriores y de la
    # planta: no son hitos del hallazgo.
    assert [m["kind"] for m in body["milestones"]] == ["finding_received", "classification"]
    assert _metric_numerator(stack, organization) == (0, 1)
    # La columna de la proyección sigue nula: ningún código la rellena (O-12); la ficha no la usa.
    row = actions.world.finding_state(finding)
    assert row is not None and row["classified_within_deadline"] is None


def test_a_configuration_before_the_reception_sets_the_deadline(stack: TimelineStack) -> None:
    actions = stack.actions
    organization = actions.world.organization()
    actions.run(
        actions.configuration.change(
            actions.context(actions.member(organization, Role.PLANT_MANAGER)),
            organization.plant_id,
            integers={"classification_deadline_business_days": 30},
            business_calendar=None,
            reason=_CONFIGURATION_REASON,
        )
    )
    actions.tick()
    finding = actions.received(organization)
    actions.tick(8 * 24 * 3600)
    actions.classify(finding)
    # Un coordinador con alcance solo de zona ve la regla de su planta (la de 30 días).
    who = actions.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, organization.site.zone_id
    )
    body = _ok(stack.get(who, actions.finding_state_id(finding)))
    (classification,) = [m for m in body["milestones"] if m["kind"] == "classification"]
    assert classification["deadline"]["met"] is True
    assert _metric_numerator(stack, organization) == (1, 1)
    due = datetime.fromisoformat(classification["deadline"]["due_at"])
    reception = datetime.fromisoformat(body["summary"]["received_at"])
    assert due - reception > timedelta(days=30)
    assert due.tzinfo is not None and due.utcoffset() == timedelta(0)
    assert reception.astimezone(UTC) == reception
