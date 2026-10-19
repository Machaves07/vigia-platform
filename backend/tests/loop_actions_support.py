"""Las acciones y la evidencia de TASK-311 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``actions_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``: hallazgos ingresados
por las rutas del contrato de U-03 y su proyección), un ``EscritorExpediente`` con la política de
texto libre de **producción** para U-04 (el validador de U-03 y los dos de U-04 con sus cachés
sobre PostgreSQL, ``loop_free_text``), los servicios reales (``ActionService``,
``EvidenceService``, ``ClassificationService`` para abrir el lazo y ``LoopConfigurationService``
para cambiar los plazos de la planta), el ``ClosureAttachmentPort`` real de TASK-310 sobre un
almacén en memoria (``MemoryStore``: se marca caído con ``down``) y la aplicación real
(``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` y el
puerto de adjuntos en ``app.state``.

``ActionsStack`` da las personas, los hallazgos en cada estado, las peticiones y lo que quedó
escrito (registros, eventos, filas de ``loop.corrective_action``, ``loop.action_evidence_entry`` y
``loop.closure_attachment``, y la proyección).

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.attachment_support import MemoryStore, RecordingAudit, png, sha256_hex
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.adapters.attachments.repository import PostgresAttachmentRepository
from vigia_platform.loop.adapters.attachments.rules import attachment_storage_key
from vigia_platform.loop.adapters.http.actions_routes import ACTIONS_PATH, EVIDENCE_PATH
from vigia_platform.loop.adapters.http.attachment_routes import ATTACHMENTS_STATE_KEY
from vigia_platform.loop.adapters.http.closure_routes import CLOSURE_PATH, REOPENING_PATH
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.actions import ActionService
from vigia_platform.loop.application.attachments import (
    ClosureAttachmentService,
    build_attachment_service,
)
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.closure import ClosureService
from vigia_platform.loop.application.evidence import EvidenceService
from vigia_platform.loop.application.reopening import ReopeningService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.loop.registration import loop_free_text
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLOSURE_DESCRIPTION",
    "DESCRIPTION",
    "EVIDENCE",
    "LICENSE",
    "REOPENING_REASON",
    "ROLE",
    "ActionsStack",
    "Who",
    "actions_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
HOUR: Final = timedelta(hours=1)
DESCRIPTION: Final = "Instalar un resguardo fijo en el acceso a la zona de carga"
EXPECTED: Final = "Sin paso peatonal por el frente de la prensa durante la operación"
EVIDENCE: Final = "Resguardo fijo instalado y anclado al piso en el acceso de la zona"
ROLE: Final = "Técnico de mantenimiento"
VERIFICATION: Final = "Se comprobó el anclaje y la señalización en la ronda de cierre"
REASON: Final = "Coexistencia observada entre la carretilla y el peatón en el pasillo"
CLOSURE_DESCRIPTION: Final = "Resguardo fijo verificado y anclado en el acceso de la zona"
LICENSE: Final = "LIC-SST-0001"
REOPENING_REASON: Final = "El resguardo se retiró durante el mantenimiento mayor"

Who = tuple[SessionCookie, uuid.UUID | None]


def production_free_text() -> FreeTextPolicyRegistry:
    """La política de U-03 y U-04 como la compone la raíz (los dos validadores reales)."""
    registry = FreeTextPolicyRegistry()
    register_u03_free_text_validator(registry)
    loop_free_text(registry)
    registry.seal()
    return registry


@dataclass
class ActionsStack:
    world: LoopWorld
    database: Database
    writer: EscritorExpediente
    actions: ActionService
    evidence: EvidenceService
    closure: ClosureService
    reopening: ReopeningService
    classification: ClassificationService
    configuration: LoopConfigurationService
    attachments: ClosureAttachmentService
    store: MemoryStore
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

    def now(self) -> Any:
        return self.world.stack.now()

    def service_like(self, cls: type[Any], **changes: Any) -> Any:
        """Otro servicio de acciones o de evidencia sobre la misma base (p. ej. una subclase)."""
        authz = self.world.stack.authz
        arguments: dict[str, Any] = {
            "database": self.database,
            "writer": self.writer,
            "authorizer": authz.authorizer,
            "clock": authz.sessions.clock,
        }
        arguments.update(changes)
        return cls(**arguments)

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
            organization.organization_id, installer, granted_at=authz.now() - HOUR
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
                reason_text=REASON,
                reinforced_reason=reinforced,
            )
        )

    def opened(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo confirmado (``open``, T03)."""
        finding = self.received(organization)
        self.classify(finding)
        return finding

    def assigned(
        self,
        coordinator: Who,
        assignee: uuid.UUID,
        organization: LoopOrganization | None = None,
    ) -> tuple[IngestedSubject, uuid.UUID]:
        """Un hallazgo con una acción vigente (``action_assigned``, T07) y su ``action_id``."""
        finding = self.opened(organization)
        response = self.assign(coordinator, finding, self.action_body(assignee))
        assert response.status_code == 201, response.text
        return finding, uuid.UUID(response.json()["action_id"])

    def with_evidence(
        self,
        coordinator: Who,
        assignee: uuid.UUID,
        organization: LoopOrganization | None = None,
    ) -> tuple[IngestedSubject, uuid.UUID]:
        """Un hallazgo ``action_assigned`` con al menos una entrada de evidencia (H-25)."""
        finding, action_id = self.assigned(coordinator, assignee, organization)
        response = self.add_evidence(coordinator, action_id, self.evidence_body())
        assert response.status_code == 201, response.text
        return finding, action_id

    def closed(
        self,
        coordinator: Who,
        assignee: uuid.UUID,
        organization: LoopOrganization | None = None,
    ) -> tuple[IngestedSubject, uuid.UUID]:
        """Un hallazgo cerrado (``closed``, T09) por la ruta real; su ``action_id``."""
        finding, action_id = self.with_evidence(coordinator, assignee, organization)
        response = self.close(coordinator, finding, self.closure_body())
        assert response.status_code == 201, response.text
        return finding, action_id

    # --- peticiones ---------------------------------------------------------------------------

    def post(self, who: Who, path: str, body: Any) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(self.client.post(path, json=body, headers=headers))
        return response

    def action_body(self, assignee: uuid.UUID, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "action_type": "engineering",
            "action_description": DESCRIPTION,
            "expected_outcome": EXPECTED,
            "assignee_user_id": str(assignee),
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def assign(
        self, who: Who, subject: IngestedSubject | uuid.UUID, body: Mapping[str, Any]
    ) -> httpx.Response:
        fsid = subject if isinstance(subject, uuid.UUID) else self.finding_state_id(subject)
        return self.post(who, ACTIONS_PATH.format(finding_state_id=fsid), dict(body))

    def evidence_body(self, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "description": EVIDENCE,
            "performed_at": format_timestamp(self.now() - timedelta(hours=2)),
            "performed_by_role": ROLE,
            "verification_note": VERIFICATION,
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def add_evidence(
        self, who: Who, action_id: uuid.UUID | str, body: Mapping[str, Any]
    ) -> httpx.Response:
        return self.post(who, EVIDENCE_PATH.format(action_id=action_id), dict(body))

    def closure_body(self, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "closure_description": CLOSURE_DESCRIPTION,
            "verification_note": VERIFICATION,
            "professional_license": LICENSE,
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def close(
        self, who: Who, subject: IngestedSubject | uuid.UUID, body: Mapping[str, Any]
    ) -> httpx.Response:
        fsid = subject if isinstance(subject, uuid.UUID) else self.finding_state_id(subject)
        return self.post(who, CLOSURE_PATH.format(finding_state_id=fsid), dict(body))

    def reopening_body(self, assignee: uuid.UUID, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "reason_text": REOPENING_REASON,
            "action_type": "engineering",
            "action_description": DESCRIPTION,
            "assignee_user_id": str(assignee),
        }
        body.update(changes)
        return {key: value for key, value in body.items() if value is not None}

    def reopen(
        self, who: Who, subject: IngestedSubject | uuid.UUID, body: Mapping[str, Any]
    ) -> httpx.Response:
        fsid = subject if isinstance(subject, uuid.UUID) else self.finding_state_id(subject)
        return self.post(who, REOPENING_PATH.format(finding_state_id=fsid), dict(body))

    def uploaded(
        self, subject: IngestedSubject, *, data: bytes | None = None, **changes: Any
    ) -> dict[str, Any]:
        """Un PNG sintético subido a su clave de cierre y la declaración que lo referencia."""
        attachment_id = self.world.stack.uuid7()  # como lo emite la concesión (TASK-310)
        unique = ((b"tEXt", b"Comment\x00" + uuid.uuid4().hex.encode("ascii")),)
        payload = data if data is not None else png(texts=unique)
        key = attachment_storage_key(
            organization_id=subject.organization.organization_id,
            plant_id=subject.organization.plant_id,
            zone_id=subject.organization.zone_id,
            subject_id=subject.subject_id,
            attachment_id=attachment_id,
            content_type=AttachmentContentType.IMAGE_PNG,
        )
        self.store.put(key, payload, "image/png")
        claim: dict[str, Any] = {
            "attachment_id": str(attachment_id),
            "content_type": "image/png",
            "size_bytes": len(payload),
            "sha256": sha256_hex(payload),
            "filename": "resguardo-instalado.png",
            "no_identifiable_person_declared": True,
        }
        claim.update(changes)
        return claim

    # --- registros de las operaciones que aún no tienen servicio (TASK-343 y TASK-313) ---------

    def write_record(
        self, subject: IngestedSubject, record_type: str, content: Mapping[str, Any]
    ) -> None:
        """Un registro de U-04 por el escritor real y su proyección (debe aplicarse)."""
        self.tick()
        receipt, step = self.world.write(subject.organization, record_type, content)
        assert isinstance(receipt, Receipt), receipt
        assert step is not None and step.applied, (record_type, step)

    def write_closure(self, subject: IngestedSubject, action_id: str) -> None:
        """``closure`` (T09) firmado con licencia y una entrada de evidencia."""
        stack = self.world.stack
        self.write_record(
            subject,
            "closure",
            {
                "closure_id": str(stack.uuid7()),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "action_id": action_id,
                "action_type": "engineering",
                "closure_description": "Resguardo instalado y verificado en el acceso de la zona",
                "evidence_entry_ids": [str(stack.uuid7())],
                "attachment_digests": [],
                "referenced_clip_ids": [],
                "signer": {
                    "user_id": str(stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "professional_license": "LIC-SST-0001",
                },
                "signed_at": format_timestamp(stack.now()),
            },
        )

    def write_unclosable(
        self, subject: IngestedSubject, *, declaration_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        """``unclosable_state_declared`` (T10) firmado con licencia (VIG-228 aún no la despacha:
        esta operación solo consume el tipo de registro para dejar el hallazgo en
        ``unclosable``, sin implementar la ruta de declaración)."""
        stack = self.world.stack
        chosen = declaration_id if declaration_id is not None else stack.uuid7()
        self.write_record(
            subject,
            "unclosable_state_declared",
            {
                "declaration_id": str(chosen),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "evaluated_alternative": "Se evaluó aislar la zona con una barrera temporal",
                "rejection_reason": "No hay espacio para una barrera sin bloquear el paso",
                "responsible_user_id": str(stack.authz.operator_id),
                "responsible_display_name": "Coordinación SST sintética",
                "decision_date": stack.now().date().isoformat(),
                "compensatory_measure": "Vigilancia permanente de un auxiliar en el acceso",
                "review_due_at": format_timestamp(stack.now() + timedelta(days=30)),
                "policy": {
                    "policy_id": str(stack.uuid7()),
                    "version": 1,
                    "signed_at": format_timestamp(stack.now() - timedelta(days=1)),
                    "document_sha256": "a" * 64,
                },
                "signer": {
                    "user_id": str(stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "professional_license": "LIC-SST-0001",
                },
                "declared_at": format_timestamp(stack.now()),
            },
        )
        return chosen

    def write_overdue(self, subject: IngestedSubject, action: Mapping[str, Any]) -> None:
        """``action_overdue`` (T08) de la acción vigente, como la tarea de plazos (TASK-313)."""
        stack = self.world.stack
        self.write_record(
            subject,
            "action_overdue",
            {
                "overdue_id": str(uuid.uuid4()),
                "action_id": str(action["action_id"]),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "due_at": format_timestamp(action["due_at"]),
                "declared_at": format_timestamp(stack.now()),
                "days_overdue": 1,
                "escalation_level": "coordinator",
                "assignee_user_id": str(action["assignee_user_id"]),
                "assignee_inactive": False,
            },
        )

    # --- lo que quedó escrito ------------------------------------------------------------------

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

    def entry_rows(self, action_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.action_evidence_entry WHERE action_id = $1"
                " ORDER BY added_at, evidence_entry_id",
                uuid.UUID(str(action_id)),
            )
        ]

    def attachment_rows(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.closure_attachment WHERE organization_id = $1"
                " AND subject_id = $2 ORDER BY attachment_id",
                subject.organization.organization_id,
                subject.subject_id,
            )
        ]

    def untouched(self, subject: IngestedSubject) -> tuple[Any, ...]:
        """Lo que un rechazo no puede cambiar: recuentos de la organización y la fila del sujeto."""
        (row,) = self.fetch(
            "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1) AS r,"
            " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS e,"
            " (SELECT count(*) FROM loop.corrective_action WHERE organization_id = $1) AS a,"
            " (SELECT count(*) FROM loop.action_evidence_entry WHERE organization_id = $1) AS v,"
            " (SELECT count(*) FROM loop.closure_attachment WHERE organization_id = $1) AS t,"
            " (SELECT last_applied_ledger_sequence FROM loop.loop_projection_checkpoint"
            " WHERE organization_id = $1 AND plant_id = $2) AS c",
            subject.organization.organization_id,
            subject.organization.plant_id,
        )
        return tuple(row), self.world.finding_state(subject)


@contextlib.contextmanager
def actions_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[ActionsStack]:
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
        common: dict[str, Any] = {
            "database": database,
            "writer": writer,
            "authorizer": authz.authorizer,
            "clock": clock,
        }
        actions = ActionService(**common)
        evidence = EvidenceService(**common)
        closure = ClosureService(**common)
        reopening = ReopeningService(actions=actions, **common)
        classification = ClassificationService(**common)
        configuration = LoopConfigurationService(
            repository=PostgresLoopConfigurationRepository(database),
            free_text=production_free_text(),
            **common,
        )
        memory = MemoryStore(clock)
        attachments = build_attachment_service(
            repository=PostgresAttachmentRepository(database),
            storage=memory,
            authorizer=authz.authorizer,
            audit=RecordingAudit(),
            clock=clock,
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
                "state": {
                    LOOP_STATE_KEY: LoopHttp(
                        configuration=configuration,
                        classification=classification,
                        actions=actions,
                        evidence=evidence,
                        closure=closure,
                        reopening=reopening,
                    ),
                    ATTACHMENTS_STATE_KEY: attachments,
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = ActionsStack(
            world,
            database,
            writer,
            actions,
            evidence,
            closure,
            reopening,
            classification,
            configuration,
            attachments,
            memory,
            client,
        )
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
