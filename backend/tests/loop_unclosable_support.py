"""El estado incerrable de TASK-312 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``unclosable_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``: hallazgos
ingresados por las rutas del contrato de U-03 y su proyección), un ``EscritorExpediente`` con la
política de texto libre de **producción** para U-04 (los dos validadores de U-04 con sus cachés
sobre PostgreSQL, ``loop_free_text``), el ``UnclosableService`` real con la configuración real de
la planta (``loop.config``) y la bandeja de salida real, y la aplicación real (``create_app`` con
la cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` en ``app.state``.

**El puerto de la política** (``Gates``) delega en el real de U-03
(``catalog_query_ports(database).gates``, sobre ``catalog.plant_policy``) salvo que la prueba lo
mande: con ``override`` devuelve ese estado —sin firmar, revocada o cargada a medias— y con
``down = True`` no responde (el fallo cerrado de BR-LAZ-54).

``UnclosableStack`` da las personas, los hallazgos en cada estado, las peticiones y lo que quedó
escrito (registros, eventos, filas de ``loop.unclosable_declaration``, ``loop.unclosable_review``
y ``loop.corrective_action``, y la proyección).

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from unittest.mock import Mock

import httpx

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import (
    IngestedSubject,
    LoopOrganization,
    LoopWorld,
    PortUnavailable,
    loop_world,
)
from tests.loop_actions_support import (
    DESCRIPTION,
    EXPECTED,
    production_free_text,
)
from tests.loop_actions_support import REASON as CLASSIFICATION_REASON
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval, ZoneGateState
from vigia_platform.catalog.domain.ports import (
    CurrentAgreement,
    GateQueryPort,
    PlantPolicyState,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.http.unclosable_routes import DECLARE_PATH, REVIEW_PATH
from vigia_platform.loop.application.actions import ActionService
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.unclosable import (
    SCHEDULE,
    TASK_NAME,
    UnclosableService,
)
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.registration import loop_tasks
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.runtime.units import (
    UnitServices,
    free_text_registry,
    registered_units,
)
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.worker.leases import LeaseSettings, SqlLeaseStore
from vigia_platform.shared.worker.scheduler import PeriodicScheduler

__all__ = [
    "ALTERNATIVE",
    "COMPENSATORY",
    "DECISION_DATE",
    "LICENSE",
    "REASON",
    "REVIEW_NOTE",
    "Gates",
    "UnclosableStack",
    "unclosable_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
HOURS: Final = 3600
ALTERNATIVE: Final = "Reubicar la estación de carga al costado norte de la zona de prensa"
REASON: Final = "La reubicación deja el pasillo peatonal sin salida de emergencia"
COMPENSATORY: Final = "Vigilancia peatonal permanente durante el turno de carga de la zona"
DECISION_DATE: Final = "2026-10-01"
REVIEW_NOTE: Final = "La alternativa sigue descartada por el trazado actual de la zona"
LICENSE: Final = "LIC-SST-0001"

Who = tuple[SessionCookie, uuid.UUID | None]


class Gates:
    """El puerto de la política de planta con dos mandos para la prueba.

    Delega en el puerto real de U-03 salvo que ``override`` tenga un estado (lo devuelve tal cual)
    o ``down`` sea verdadero (no responde: ``PortUnavailable``). Las otras cinco operaciones del
    puerto no las usa el incerrable y se delegan sin más."""

    def __init__(self, real: GateQueryPort) -> None:
        self._real = real
        self.override: PlantPolicyState | None = None
        self.down = False

    def __repr__(self) -> str:
        return "Gates()"

    async def state(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneGateState:
        return await self._real.state(context, zone_id)

    async def states_by_plant(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> tuple[ZoneGateState, ...]:
        return await self._real.states_by_plant(context, plant_id)

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None:
        return await self._real.state_at(context, zone_id, gate, at)

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]:
        return await self._real.gate_history(context, zone_id, start, end)

    async def plant_policy(self, context: ScopeContext, plant_id: uuid.UUID) -> PlantPolicyState:
        if self.down:
            raise PortUnavailable("el puerto de la política no responde")
        if self.override is not None:
            return self.override
        return await self._real.plant_policy(context, plant_id)

    async def current_agreement(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> CurrentAgreement | None:
        return await self._real.current_agreement(context, zone_id)


@dataclass
class UnclosableStack:
    world: LoopWorld
    database: Database
    writer: EscritorExpediente
    service: UnclosableService
    classification: ClassificationService
    actions: ActionService
    gates: Gates
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.world.stack.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.world.stack.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.world.stack.execute(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.world.stack.tick(seconds)

    def now(self) -> datetime:
        moment: datetime = self.world.stack.now()
        return moment

    # --- personas -----------------------------------------------------------------------------

    def user(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> uuid.UUID:
        authz = self.world.stack.authz
        user = authz.add_user(organization.organization_id)
        authz.assign(organization.organization_id, user, role, level, scope)
        return user

    def member(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Who:
        user = self.user(organization, role, level, scope)
        return self.world.stack.authz.open_session(organization.organization_id, user), None

    def installer(self, organization: LoopOrganization) -> Who:
        """Instalador del proveedor con una concesión vigente sobre la organización."""
        authz = self.world.stack.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            organization.organization_id, installer, granted_at=authz.now() - timedelta(hours=1)
        )
        return authz.open_session(authz.provider_organization_id, installer), concession

    def context(self, who: Who) -> ScopeContext:
        cookie, concession = who
        authz = self.world.stack.authz
        scope = self.run(authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    def deactivate(self, user_id: uuid.UUID) -> None:
        """La persona deja de ser usuaria activa, como ``POST /users/{id}/deactivate`` de U-02."""
        self.execute(
            "UPDATE identity.user_account SET status = 'deactivated', deactivated_at = $2"
            " WHERE user_id = $1",
            user_id,
            self.now(),
        )

    # --- sujetos ------------------------------------------------------------------------------

    def received(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo ingresado y proyectado (``received``)."""
        finding = self.world.ingest_finding(organization or self.world.a)
        step = self.world.project(finding)
        assert step.applied and step.transition_id == "T01"
        return finding

    def in_review(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        detection = self.world.ingest_detection(organization or self.world.a)
        assert self.world.project(detection).transition_id == "T02"
        return detection

    def finding_state_id(self, subject: IngestedSubject) -> uuid.UUID:
        row = self.world.finding_state(subject)
        assert row is not None
        return uuid.UUID(str(row["finding_state_id"]))

    def classify(
        self,
        subject: IngestedSubject,
        outcome: str = "confirmed",
        category: str = "real_coexistence",
        *,
        reinforced: bool = False,
    ) -> None:
        """Clasifica por el servicio real (con un coordinador de la organización del sujeto)."""
        self.tick()
        context = self.context(self.member(subject.organization))
        self.run(
            self.classification.classify(
                context,
                self.finding_state_id(subject),
                outcome=outcome,
                reason_category=category,
                reason_text=CLASSIFICATION_REASON,
                reinforced_reason=reinforced,
            )
        )

    def opened(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo confirmado (``open``, T03)."""
        finding = self.received(organization)
        self.classify(finding)
        return finding

    def assigned(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo con su acción asignada (``action_assigned``, T07)."""
        subject = self.opened(organization)
        assignee = self.user(subject.organization)
        self.tick()
        context = self.context(self.member(subject.organization))
        self.run(
            self.actions.assign(
                context,
                self.finding_state_id(subject),
                action_type="engineering",
                action_description=DESCRIPTION,
                expected_outcome=EXPECTED,
                assignee_user_id=assignee,
            )
        )
        return subject

    def overdue(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo con su acción vencida (``overdue``, T07 + T08)."""
        finding = self.assigned(organization)
        (action,) = self.action_rows(finding)
        self.write_record(
            finding,
            "action_overdue",
            {
                "overdue_id": str(uuid.uuid4()),
                "action_id": str(action["action_id"]),
                "subject_kind": finding.subject_kind,
                "subject_id": str(finding.subject_id),
                "due_at": format_timestamp(action["due_at"]),
                "declared_at": format_timestamp(self.now()),
                "days_overdue": 1,
                "escalation_level": "coordinator",
                "assignee_user_id": str(action["assignee_user_id"]),
                "assignee_inactive": False,
            },
        )
        return finding

    def write_action(self, subject: IngestedSubject, assignee: uuid.UUID | None = None) -> None:
        """``action_assigned`` (T07) de ``subject`` con su responsable y su plazo."""
        responsible = assignee if assignee is not None else self.user(subject.organization)
        due_at = self.now() + timedelta(hours=24 * 30)
        self.write_record(
            subject,
            "action_assigned",
            {
                "action_id": str(self.world.stack.uuid7()),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "action_type": "engineering",
                "action_description": DESCRIPTION,
                "expected_outcome": EXPECTED,
                "assignee_user_id": str(responsible),
                "assignee_display_name": "Responsable sintético de la acción",
                "due_at": format_timestamp(due_at),
                "deadline_days": 30,
                "assigned_by": {
                    "user_id": str(self.world.stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "role_in_use": "coordinator_sst",
                },
                "assigned_at": format_timestamp(self.now()),
            },
        )

    def write_record(
        self, subject: IngestedSubject, record_type: str, content: Mapping[str, Any]
    ) -> None:
        """Un registro de U-04 por el escritor real y su proyección (debe aplicarse)."""
        self.tick()
        _receipt, step = self.world.write(subject.organization, record_type, dict(content))
        assert step is not None and step.applied, (record_type, step)

    def close(self, subject: IngestedSubject) -> None:
        """``closure`` (T09) firmado con licencia y una entrada de evidencia."""
        (action,) = self.action_rows(subject)
        self.write_record(
            subject,
            "closure",
            {
                "closure_id": str(self.world.stack.uuid7()),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "action_id": str(action["action_id"]),
                "action_type": "engineering",
                "closure_description": "Resguardo instalado y verificado en el acceso de la zona",
                "evidence_entry_ids": [str(self.world.stack.uuid7())],
                "attachment_digests": [],
                "referenced_clip_ids": [],
                "signer": {
                    "user_id": str(self.world.stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "professional_license": LICENSE,
                },
                "signed_at": format_timestamp(self.now()),
            },
        )

    # --- la tarea diaria, como la registra la unidad ------------------------------------------

    def unit_services(self) -> UnitServices:
        """La infraestructura que la raíz le entrega a la unidad (lo que usa la tarea, real)."""
        authz = self.world.stack.authz
        inert: Any = Mock()
        return UnitServices(
            clock=authz.sessions.clock,
            metrics=get_metrics(),
            provider_organization_id=authz.provider_organization_id,
            database=self.database,
            contexts=authz.contexts,
            authorizer=authz.authorizer,
            audit=authz.audit,
            outbox=self.world.stack.outbox,
            writer=self.writer,
            free_text=free_text_registry(registered_units()),
            # La tarea de plazos no firma, ni pide puntos de control, ni usa KMS: dobles inertes.
            signing=inert,
            checkpoints=inert,
            kms=inert,
        )

    def reach_review_date(self, subject: IngestedSubject, days: int = 1) -> None:
        """Avanza el reloj simulado hasta que la fecha de revisión quede atrás (un solo reloj)."""
        (row,) = self.fetch(
            "SELECT review_due_at FROM loop.unclosable_declaration WHERE organization_id = $1"
            " AND subject_id = $2 ORDER BY declared_at DESC LIMIT 1",
            subject.organization.organization_id,
            subject.subject_id,
        )
        ahead = (row["review_due_at"] - self.now()).total_seconds()
        self.tick(seconds=max(ahead, 0.0) + days)

    def task_due(self, *, owner: str | None = None) -> None:
        """Vence la tarea; con ``owner``, deja su arrendamiento a nombre de otro proceso y
        **vencido** (como el de una ejecución que murió a mitad)."""
        self.execute(
            "UPDATE shared.periodic_task SET next_run_at = $1, lease_owner = $2, lease_until = $3"
            " WHERE task_name = $4",
            self.now() - timedelta(seconds=1),
            owner,
            self.now() - timedelta(hours=1) if owner is not None else None,
            TASK_NAME,
        )

    def scheduler(self, owner: str) -> PeriodicScheduler:
        """El planificador real sobre la tarea ``unclosable_review_due`` de la unidad, con su fila
        ya sincronizada en ``shared.periodic_task`` (como la deja ``OutboxCatalog.synchronize``)."""
        registry = PeriodicTaskRegistry()
        loop_tasks(registry, self.unit_services())
        task = registry.get(TASK_NAME)
        assert task is not None and task.schedule == SCHEDULE
        self.execute(
            "INSERT INTO shared.periodic_task (task_name, unit, schedule, iterates_organizations,"
            " next_run_at) VALUES ($1, 'U-04', $2, true, $3)"
            " ON CONFLICT (task_name) DO UPDATE SET schedule = EXCLUDED.schedule,"
            " next_run_at = EXCLUDED.next_run_at",
            TASK_NAME,
            task.schedule.text,
            self.now() - timedelta(seconds=1),
        )
        return PeriodicScheduler(
            database=self.database,
            registry=registry,
            leases=SqlLeaseStore(
                database=self.database,
                contexts=self.world.stack.authz.contexts,
                settings=LeaseSettings(),
            ),
            contexts=self.world.stack.authz.contexts,
            clock=self.world.stack.authz.sessions.clock,
            owner=owner,
            metrics=get_metrics(),
        )

    def periodic_context(self, organization: LoopOrganization) -> ScopeContext:
        """El contexto que el planificador da a la tarea en ``organization`` (BR-NUC-03)."""
        registry = PeriodicTaskRegistry()
        loop_tasks(registry, self.unit_services())
        task = registry.get(TASK_NAME)
        assert task is not None
        context: ScopeContext = self.world.stack.authz.contexts.context_for_organization(
            task, organization.organization_id
        )
        return context

    def service_like(self, cls: type[UnclosableService]) -> UnclosableService:
        """Un servicio de la clase ``cls`` (una subclase con un gancho) con la misma infraestructura
        que ``service``."""
        return cls(
            database=self.database,
            writer=self.writer,
            authorizer=self.world.stack.authz.authorizer,
            clock=self.world.stack.authz.sessions.clock,
            gates=self.gates,
            configuration=PostgresLoopConfigurationRepository(self.database),
            outbox=self.world.stack.outbox,
        )

    async def mark_due_async(
        self, organization: LoopOrganization, service: UnclosableService | None = None
    ) -> int:
        """Una ejecución de ``unclosable_review_due`` en ``organization``, en su propia transacción
        (y su propia conexión), como la del planificador."""
        chosen = service if service is not None else self.service
        async with self.database.transaction(self.periodic_context(organization)) as transaction:
            return await chosen.mark_due(transaction)

    async def waiting_locks(self) -> int:
        """Candados pedidos y no concedidos en la base de la prueba (de cualquier tipo)."""
        rows = await self.world.stack.authz.sessions.admin.fetch(
            "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
            " AND (database IS NULL"
            " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
        )
        return int(rows[0]["n"])

    def subject_in(
        self, state: str, organization: LoopOrganization | None = None
    ) -> IngestedSubject:
        """Un hallazgo en el estado pedido de la máquina de §4.1."""
        if state == "received":
            return self.received(organization)
        if state == "in_review":
            return self.in_review(organization)
        if state == "open":
            return self.opened(organization)
        if state == "action_assigned":
            return self.assigned(organization)
        if state == "overdue":
            return self.overdue(organization)
        if state == "discarded":
            subject = self.received(organization)
            self.classify(subject, "false_positive", "no_person_present")
            return subject
        if state == "closed":
            subject = self.assigned(organization)
            self.close(subject)
            return subject
        raise ValueError(f"estado sin constructor: {state}")  # pragma: no cover - lista cerrada

    # --- peticiones ---------------------------------------------------------------------------

    def post(self, who: Who, path: str, body: Any) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(self.client.post(path, json=body, headers=headers))
        return response

    def declaration_body(self, responsible: uuid.UUID, **changes: Any) -> dict[str, Any]:
        """Los cinco campos, la fecha de revisión y la licencia (SUP-U05-12)."""
        body: dict[str, Any] = {
            "evaluated_alternative": ALTERNATIVE,
            "rejection_reason": REASON,
            "responsible_user_id": str(responsible),
            "decision_date": DECISION_DATE,
            "compensatory_measure": COMPENSATORY,
            "professional_license": LICENSE,
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def declare(
        self,
        who: Who,
        subject: IngestedSubject | uuid.UUID,
        body: Mapping[str, Any],
    ) -> httpx.Response:
        fsid = subject if isinstance(subject, uuid.UUID) else self.finding_state_id(subject)
        return self.post(who, DECLARE_PATH.format(finding_state_id=fsid), dict(body))

    def review_body(self, **changes: Any) -> dict[str, Any]:
        """Una revisión que renueva la declaración (T11) con su nota y su licencia."""
        body: dict[str, Any] = {
            "outcome": "renewed",
            "review_note": REVIEW_NOTE,
            "professional_license": LICENSE,
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def review(
        self, who: Who, declaration_id: uuid.UUID | str, body: Mapping[str, Any]
    ) -> httpx.Response:
        return self.post(who, REVIEW_PATH.format(declaration_id=declaration_id), dict(body))

    def action_body(self, assignee: uuid.UUID, **changes: Any) -> dict[str, Any]:
        """La acción de una conversión (T12): el cuerpo de TASK-311."""
        body: dict[str, Any] = {
            "action_type": "engineering",
            "action_description": DESCRIPTION,
            "expected_outcome": EXPECTED,
            "assignee_user_id": str(assignee),
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    # --- la política de planta ------------------------------------------------------------------

    def signed_policy(
        self, organization: LoopOrganization, *, version: int = 1, signed_at: datetime | None = None
    ) -> uuid.UUID:
        """Una política firmada de la planta en ``catalog.plant_policy`` (U-03, §1.2)."""
        policy_id = uuid.uuid4()
        moment = signed_at if signed_at is not None else self.now()
        self.execute(
            "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
            " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
            " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 'Firmante sintético', 'REF-SINTETICA', $6,"
            " 'Resumen sintético de criterios', $7, $5, $8)",
            policy_id,
            organization.organization_id,
            organization.plant_id,
            version,
            moment,
            json.dumps(
                {
                    "document_id": str(uuid.uuid4()),
                    "storage_key": f"documents/{uuid.uuid4()}",
                    "sha256": "b" * 64,
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                }
            ),
            self.world.stack.authz.operator_id,
            uuid.uuid4(),
        )
        return policy_id

    def policy_rows(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM catalog.plant_policy WHERE organization_id = $1 AND plant_id = $2"
                " ORDER BY version",
                organization.organization_id,
                organization.plant_id,
            )
        ]

    # --- lo que quedó escrito -------------------------------------------------------------------

    def subject_records(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        """Los registros de U-04 del sujeto, en el orden de la cadena, con su hash."""
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, record_type, chain_sequence, content_hash, record_hash,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE organization_id = $1 AND plant_id = $2"
                " AND ledger.vigia_bytes_to_jsonb(content) ->> 'subject_id' = $3"
                " ORDER BY chain_sequence",
                subject.organization.organization_id,
                subject.organization.plant_id,
                str(subject.subject_id),
            )
        ]

    def records_of(self, subject: IngestedSubject, record_type: str) -> list[dict[str, Any]]:
        return [r for r in self.subject_records(subject) if r["record_type"] == record_type]

    def events(self, organization: LoopOrganization, record_id: uuid.UUID) -> list[str]:
        """Los nombres de los eventos que citan ``record_id`` en su carga."""
        return [
            str(row["event_name"])
            for row in self.fetch(
                "SELECT event_name FROM shared.outbox_event WHERE organization_id = $1"
                " AND payload ->> 'record_id' = $2 ORDER BY event_name",
                organization.organization_id,
                str(record_id),
            )
        ]

    def due_events(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """Los eventos ``unclosable_review_due`` de la organización, por declaración."""
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self.fetch(
                "SELECT event_id, payload FROM shared.outbox_event WHERE organization_id = $1"
                " AND event_name = 'unclosable_review_due' ORDER BY created_at, event_id",
                organization.organization_id,
            )
        ]

    def declaration_rows(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.unclosable_declaration WHERE organization_id = $1"
                " AND subject_id = $2 ORDER BY declared_at, declaration_id",
                subject.organization.organization_id,
                subject.subject_id,
            )
        ]

    def review_rows(self, declaration_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.unclosable_review WHERE declaration_id = $1"
                " ORDER BY reviewed_at, review_id",
                uuid.UUID(str(declaration_id)),
            )
        ]

    def action_rows(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.corrective_action WHERE organization_id = $1"
                " AND subject_id = $2 ORDER BY assigned_at, action_id",
                subject.organization.organization_id,
                subject.subject_id,
            )
        ]

    def untouched(self, subject: IngestedSubject) -> tuple[Any, ...]:
        """Lo que un rechazo no puede cambiar: recuentos de la organización y la fila del sujeto."""
        (row,) = self.fetch(
            "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1) AS r,"
            " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS e,"
            " (SELECT count(*) FROM loop.unclosable_declaration WHERE organization_id = $1) AS d,"
            " (SELECT count(*) FROM loop.unclosable_review WHERE organization_id = $1) AS v,"
            " (SELECT count(*) FROM loop.corrective_action WHERE organization_id = $1) AS a,"
            " (SELECT last_applied_ledger_sequence FROM loop.loop_projection_checkpoint"
            " WHERE organization_id = $1 AND plant_id = $2) AS c",
            subject.organization.organization_id,
            subject.organization.plant_id,
        )
        return tuple(row), self.world.finding_state(subject)


@contextlib.contextmanager
def unclosable_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[UnclosableStack]:
    with loop_world(endpoint, prefix) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database(worker_pool_size=pool)
        writer = EscritorExpediente(
            database=database,
            registry=stack.registry,
            free_text=production_free_text(),
            evidence=EvidenceVerifier(stack.storage, clock),
            outbox=stack.outbox,
            clock=clock,
        )
        classification = ClassificationService(
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            clock=clock,
        )
        actions = ActionService(
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            clock=clock,
        )
        gates = Gates(catalog_query_ports(database).gates)
        service = UnclosableService(
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            clock=clock,
            gates=gates,
            configuration=PostgresLoopConfigurationRepository(database),
            outbox=stack.outbox,
        )
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=clock,
                ),
                "state": {LOOP_STATE_KEY: LoopHttp(unclosable=service)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = UnclosableStack(
            world, database, writer, service, classification, actions, gates, client
        )
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
