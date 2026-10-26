"""La clasificación de TASK-308 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``classification_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``: hallazgos y
detecciones ingresados por las rutas del contrato de U-03 y su proyección), un
``EscritorExpediente`` con la política de texto libre de producción para U-04 (el validador de
U-03, ``blocked_vocabulary`` real con su caché sobre PostgreSQL y un **doble** de
``person_attribution``, que llega con TASK-340), el ``ClassificationService`` real y la aplicación
real (``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` en
``app.state``.

``ClassificationStack`` da las personas (coordinador SST y otros roles, instalador bajo
concesión), las peticiones, los registros de las otras operaciones del ciclo de vida que aún no
tienen servicio (acción, cierre, declaración de incerrable y tramo tardío, escritos por el
escritor real con su proyección en la misma transacción) y lo que quedó escrito.

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
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextHint,
    FreeTextPolicyRegistry,
    FreeTextRejected,
)
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.projection.fold import Step
from vigia_platform.loop.registration import register_blocked_vocabulary
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ATTRIBUTION_MARKER",
    "ATTRIBUTION_REJECTION",
    "ATTRIBUTION_SUGGESTION",
    "BLOCKED_REASON",
    "BLOCKED_REJECTION",
    "BLOCKED_SUGGESTION",
    "BLOCKED_TERM",
    "CONCESSION_HEADER",
    "PATH",
    "REASON",
    "REINFORCED_REASON",
    "ClassificationStack",
    "Who",
    "classification_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
HOUR: Final = timedelta(hours=1)
PATH: Final = "/loop/findings/{finding_state_id}/classifications"
REASON: Final = "Coexistencia observada entre la carretilla y el peatón en el pasillo"
REINFORCED_REASON: Final = "Revisado el clip completo con mantenimiento: la intervención constaba"
BLOCKED_REASON: Final = "La zona quedó garantizada sin ningún riesgo tras la revisión del clip"
"""Contiene ``garantizar`` (lista inicial de vocabulario bloqueado, BLM §14.2)."""
BLOCKED_TERM: Final = "garantizada"
"""La flexión de ``garantizar`` que cae en ``BLOCKED_REASON``: el ``rejection.term`` (VIG-344)."""
BLOCKED_SUGGESTION: Final = (
    "Cita la cifra medida, con su periodo y su fuente, en lugar de prometerla."
)
"""La alternativa de ``garantizar`` en la lista inicial (§14.2): el ``rejection.suggestion``."""
ATTRIBUTION_MARKER: Final = "lo provocó el auxiliar"
"""El doble de ``person_attribution`` rechaza un texto que lo contiene (TASK-340 trae el real)."""
ATTRIBUTION_SUGGESTION: Final = "Sugerencia sintética del doble: describa la condición del proceso."
"""La alternativa que da el doble; como el real, nunca da el término (P3)."""
BLOCKED_REJECTION: Final = {
    "code": "blocked_vocabulary_term",
    "term": BLOCKED_TERM,
    "suggestion": BLOCKED_SUGGESTION,
}
"""El ``rejection`` exacto de la respuesta a ``BLOCKED_REASON`` (VIG-344)."""
ATTRIBUTION_REJECTION: Final = {
    "code": "person_attribution_detected",
    "suggestion": ATTRIBUTION_SUGGESTION,
}
"""El del doble de atribución: la alternativa sin el término (P3)."""
PERSON_ATTRIBUTION: Final = "person_attribution"

Who = tuple[SessionCookie, uuid.UUID | None]


def _person_attribution_double(candidate: FreeTextCandidate, field: FreeTextField) -> None:
    """Doble del validador ``person_attribution`` (BLM §14.5): el mismo contrato de rechazo, con
    la pista sin término del real (VIG-344)."""
    if ATTRIBUTION_MARKER in candidate.text.casefold():
        raise FreeTextRejected(
            "person_attribution_detected",
            field,
            "el texto atribuye la causa a una persona",
            detail_code=LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
            hint=FreeTextHint(
                code="person_attribution_detected", suggestion_es=ATTRIBUTION_SUGGESTION
            ),
        )


def classification_free_text() -> FreeTextPolicyRegistry:
    """La política con los validadores de U-03 y U-04 (``person_attribution`` como doble)."""
    registry = FreeTextPolicyRegistry()
    register_u03_free_text_validator(registry)
    register_blocked_vocabulary(registry)
    registry.register(PERSON_ATTRIBUTION, _person_attribution_double)
    registry.seal()
    return registry


@dataclass
class ClassificationStack:
    world: LoopWorld
    database: Database
    writer: EscritorExpediente
    service: ClassificationService
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.world.stack.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.world.stack.fetch(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.world.stack.tick(seconds)

    def service_like(self, **changes: Any) -> ClassificationService:
        """Otro servicio sobre la misma base (p. ej. una subclase de prueba)."""
        authz = self.world.stack.authz
        arguments: dict[str, Any] = {
            "database": self.database,
            "writer": self.writer,
            "authorizer": authz.authorizer,
            "clock": authz.sessions.clock,
        }
        cls = changes.pop("cls", ClassificationService)
        arguments.update(changes)
        service: ClassificationService = cls(**arguments)
        return service

    # --- personas -----------------------------------------------------------------------------

    def member(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Who:
        authz = self.world.stack.authz
        user = authz.add_user(organization.organization_id)
        authz.assign(organization.organization_id, user, role, level, scope)
        return authz.open_session(organization.organization_id, user), None

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

    # --- peticiones ---------------------------------------------------------------------------

    def send(self, who: Who, finding_state_id: uuid.UUID | str, body: Any) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.post(
                PATH.format(finding_state_id=finding_state_id), json=body, headers=headers
            )
        )
        return response

    def classify(
        self,
        who: Who,
        subject: IngestedSubject,
        outcome: str = "confirmed",
        reason_category: str = "real_coexistence",
        reason_text: str | None = REASON,
        *,
        reinforced: bool = False,
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "outcome": outcome,
            "reason_category": reason_category,
            "reinforced_reason": reinforced,
        }
        if reason_text is not None:
            body["reason_text"] = reason_text
        return self.send(who, self.finding_state_id(subject), body)

    # --- otras operaciones del ciclo de vida (registro real, sin su servicio todavía) ---------

    def _signer(self) -> dict[str, Any]:
        return {
            "user_id": str(self.world.stack.authz.operator_id),
            "display_name": "Coordinación SST sintética",
            "professional_license": "LIC-SST-0001",
        }

    def _write(
        self, subject: IngestedSubject, record_type: str, content: Mapping[str, Any]
    ) -> Step:
        self.tick()
        receipt, step = self.world.write(subject.organization, record_type, content)
        assert isinstance(receipt, Receipt), receipt
        assert step is not None and step.applied, (record_type, step)
        return step

    def assign_action(self, subject: IngestedSubject, *, due_in: timedelta | None = None) -> str:
        """``action_assigned`` (T07) con plazo dentro de 30 días; devuelve ``action_id``."""
        stack = self.world.stack
        now = stack.now()
        action_id = str(stack.uuid7())
        self._write(
            subject,
            "action_assigned",
            {
                "action_id": action_id,
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "action_type": "engineering",
                "action_description": "Instalar un resguardo fijo en el acceso a la zona de carga",
                "assignee_user_id": str(stack.authz.operator_id),
                "assignee_display_name": "Responsable sintético",
                "due_at": format_timestamp(now + (due_in or timedelta(days=30))),
                "deadline_days": 30,
                "assigned_by": {
                    "user_id": str(stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "role_in_use": "coordinator_sst",
                },
                "assigned_at": format_timestamp(now),
            },
        )
        return action_id

    def close(self, subject: IngestedSubject, action_id: str) -> None:
        """``closure`` (T09) firmado con licencia y una entrada de evidencia."""
        stack = self.world.stack
        self._write(
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
                "signer": self._signer(),
                "signed_at": format_timestamp(stack.now()),
            },
        )

    def declare_unclosable(self, subject: IngestedSubject) -> None:
        """``unclosable_state_declared`` (T10) con los cinco campos y la política cargada."""
        stack = self.world.stack
        now = stack.now()
        self._write(
            subject,
            "unclosable_state_declared",
            {
                "declaration_id": str(stack.uuid7()),
                "subject_kind": subject.subject_kind,
                "subject_id": str(subject.subject_id),
                "anchor_record_id": str(subject.record_id),
                "evaluated_alternative": "Reubicar la estación de carga fuera del pasillo común",
                "rejection_reason": "La estructura del edificio no admite el cambio este año",
                "responsible_user_id": str(stack.authz.operator_id),
                "responsible_display_name": "Responsable sintético",
                "decision_date": now.date().isoformat(),
                "compensatory_measure": "Señalización y velocidad limitada en el pasillo común",
                "review_due_at": format_timestamp(now + timedelta(days=90)),
                "policy": {
                    "policy_id": str(uuid.uuid4()),
                    "version": 1,
                    "signed_at": format_timestamp(now - timedelta(days=30)),
                    "document_sha256": "a" * 64,
                },
                "signer": self._signer(),
                "declared_at": format_timestamp(now),
            },
        )

    def late_segment_content(self, subject: IngestedSubject, state: str) -> dict[str, Any]:
        """``late_segment_attached`` (T21): la vía de eventos lo escribe en cualquier estado."""
        stack = self.world.stack
        return {
            "segment_record_id": str(uuid.uuid4()),
            "episode_id": str(subject.subject_id),
            "segment_index": 1,
            "received_at": format_timestamp(stack.now()),
            "episode_state_at_arrival": state,
            "reopened_loop": False,
            "delay_ms": 1000,
        }

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

    def classifications(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [r for r in self.subject_records(subject) if r["record_type"] == "classification"]

    def labels(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        return [
            {**dict(row), "labeled_by": json.loads(row["labeled_by"])}
            for row in self.fetch(
                "SELECT label_id, organization_id, plant_id, zone_id, source_record_id,"
                " subject_record_id, family, outcome, reason_category, evidence_ids, labeled_at,"
                " labeled_by::text AS labeled_by FROM ledger.label WHERE organization_id = $1",
                organization.organization_id,
            )
        ]

    def history(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        """Las filas de ``loop.classification`` del sujeto, por secuencia."""
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.classification WHERE organization_id = $1 AND subject_id = $2"
                " ORDER BY sequence_in_subject",
                subject.organization.organization_id,
                subject.subject_id,
            )
        ]

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

    def counts(self, organization: LoopOrganization) -> tuple[int, int, int, int]:
        """Registros, etiquetas, eventos y filas de historial de la organización."""
        (row,) = self.fetch(
            "SELECT (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1) AS r,"
            " (SELECT count(*) FROM ledger.label WHERE organization_id = $1) AS l,"
            " (SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1) AS e,"
            " (SELECT count(*) FROM loop.classification WHERE organization_id = $1) AS h",
            organization.organization_id,
        )
        return int(row["r"]), int(row["l"]), int(row["e"]), int(row["h"])


@contextlib.contextmanager
def classification_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[ClassificationStack]:
    with loop_world(endpoint, prefix) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database(worker_pool_size=pool)
        writer = EscritorExpediente(
            database=database,
            registry=stack.registry,
            free_text=classification_free_text(),
            evidence=EvidenceVerifier(stack.storage, clock),
            outbox=stack.outbox,
            clock=clock,
        )
        service = ClassificationService(
            database=database, writer=writer, authorizer=authz.authorizer, clock=clock
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
                "state": {LOOP_STATE_KEY: LoopHttp(classification=service)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = ClassificationStack(world, database, writer, service, client)
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
