"""La cola de revisión de TASK-309 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``review_queue_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``), el
``ReviewQueueConsumer`` real (sin pasar por el despachador: las pruebas lo entregan a mano, como
``loop_projection``), el ``ReviewQueueService`` real y la aplicación real (``create_app`` con la
cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` en ``app.state``.

``ReviewQueueStack`` da las personas, la detección ya entregada al consumidor (``in_review``, con
su fila de ``loop.review_queue_item`` y su proyección ``in_review``), las peticiones HTTP y lo que
quedó escrito.

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextCandidate, FreeTextField, FreeTextPolicyRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.postgres.review_queue_repository import ReviewQueueRepository
from vigia_platform.loop.application.review_queue import ReviewQueueConsumer, ReviewQueueService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.registration import register_blocked_vocabulary
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database

__all__ = [
    "PATH",
    "REASON",
    "ReviewQueueStack",
    "Who",
    "review_queue_free_text",
    "review_queue_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
PATH: Final = "/loop/review-queue/{detection_id}/resolution"
LIST_PATH: Final = "/loop/review-queue"
REASON: Final = "Lámina plástica colgante confundida con una persona en el borde de la zona"
PERSON_ATTRIBUTION: Final = "person_attribution"

Who = tuple[SessionCookie, uuid.UUID | None]


def _no_attribution(candidate: FreeTextCandidate, field: FreeTextField) -> None:
    """Doble del validador ``person_attribution``: nunca rechaza (TASK-340 trae el real)."""


def review_queue_free_text() -> FreeTextPolicyRegistry:
    registry = FreeTextPolicyRegistry()
    register_u03_free_text_validator(registry)
    register_blocked_vocabulary(registry)
    registry.register(PERSON_ATTRIBUTION, _no_attribution)
    registry.seal()
    return registry


@dataclass
class ReviewQueueStack:
    world: LoopWorld
    database: Database
    writer: EscritorExpediente
    consumer: ReviewQueueConsumer
    repository: ReviewQueueRepository
    service: ReviewQueueService
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.world.stack.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.world.stack.fetch(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.world.stack.tick(seconds)

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

    def context(self, who: Who) -> ScopeContext:
        cookie, concession = who
        authz = self.world.stack.authz
        scope = self.run(authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    # --- sujetos --------------------------------------------------------------------------------

    def in_review(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Una detección ingresada y entregada al consumidor real: cola e ``in_review``."""
        detection = self.world.ingest_detection(organization or self.world.a)
        event = self.world.review_queue_event(detection)
        inserted = self.world.deliver_review_queue(self.consumer, event)
        assert inserted, "el consumidor no creó el elemento de la cola"
        row = self.queue_row(detection)
        assert row is not None and row["state"] == "pending"
        projected = self.world.finding_state(detection)
        assert projected is not None and projected["lifecycle_state"] == "in_review"
        return detection

    # --- peticiones -------------------------------------------------------------------------

    def send(self, who: Who, detection_id: uuid.UUID | str, body: Any) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        response: httpx.Response = self.run(
            self.client.post(PATH.format(detection_id=detection_id), json=body, headers=headers)
        )
        return response

    def resolve(
        self,
        who: Who,
        subject: IngestedSubject,
        outcome: str = "review_confirmed",
        reason_category: str = "real_coexistence",
        reason_text: str | None = REASON,
    ) -> httpx.Response:
        body: dict[str, Any] = {"outcome": outcome, "reason_category": reason_category}
        if reason_text is not None:
            body["reason_text"] = reason_text
        return self.send(who, subject.subject_id, body)

    def list_queue(
        self, who: Who, *, cursor: str | None = None, limit: int | None = None
    ) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = str(limit)
        response: httpx.Response = self.run(
            self.client.get(LIST_PATH, params=params, headers=headers)
        )
        return response

    # --- lo que quedó escrito ------------------------------------------------------------------

    def queue_row(self, subject: IngestedSubject) -> dict[str, Any] | None:
        rows = self.fetch(
            "SELECT * FROM loop.review_queue_item WHERE organization_id = $1 AND detection_id = $2",
            subject.organization.organization_id,
            subject.subject_id,
        )
        return dict(rows[0]) if rows else None

    def resolutions(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM loop.review_resolution"
                " WHERE organization_id = $1 AND detection_id = $2 ORDER BY resolved_at",
                subject.organization.organization_id,
                subject.subject_id,
            )
        ]

    def subject_records(self, subject: IngestedSubject) -> list[dict[str, Any]]:
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, record_type, chain_sequence, content_hash, record_hash,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE organization_id = $1 AND plant_id = $2"
                " ORDER BY chain_sequence",
                subject.organization.organization_id,
                subject.organization.plant_id,
            )
        ]

    def labels(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT label_id, source_record_id, outcome, reason_category, family"
                " FROM ledger.label WHERE organization_id = $1",
                organization.organization_id,
            )
        ]

    def events(self, organization: LoopOrganization, record_id: uuid.UUID) -> list[str]:
        return [
            str(row["event_name"])
            for row in self.fetch(
                "SELECT event_name FROM shared.outbox_event WHERE organization_id = $1"
                " AND payload ->> 'record_id' = $2 ORDER BY event_name",
                organization.organization_id,
                str(record_id),
            )
        ]

    def overdue_events(self, organization: LoopOrganization, detection_id: uuid.UUID) -> list[Any]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT payload::text AS payload, created_at FROM shared.outbox_event"
                " WHERE organization_id = $1 AND event_name = 'review_overdue'"
                " AND payload ->> 'detection_id' = $2 ORDER BY created_at",
                organization.organization_id,
                str(detection_id),
            )
        ]

    def counts(self, organization: LoopOrganization) -> tuple[int, int, int]:
        """Elementos de la cola, registros ``review_resolution`` e historial, de la organización."""
        (row,) = self.fetch(
            "SELECT (SELECT count(*) FROM loop.review_queue_item WHERE organization_id = $1) AS q,"
            " (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1"
            "  AND record_type = 'review_resolution') AS r,"
            " (SELECT count(*) FROM loop.review_resolution WHERE organization_id = $1) AS h",
            organization.organization_id,
        )
        return int(row["q"]), int(row["r"]), int(row["h"])


@contextlib.contextmanager
def review_queue_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[ReviewQueueStack]:
    with loop_world(endpoint, prefix) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database(worker_pool_size=pool)
        writer = EscritorExpediente(
            database=database,
            registry=stack.registry,
            free_text=review_queue_free_text(),
            evidence=EvidenceVerifier(stack.storage, clock),
            outbox=stack.outbox,
            clock=clock,
        )
        repository = ReviewQueueRepository(database)
        consumer = ReviewQueueConsumer(
            store=world.store,
            reader=LectorExpediente(database=database, audit=authz.sessions.audit, clock=clock),
            repository=repository,
            configuration=PostgresLoopConfigurationRepository(database),
        )
        service = ReviewQueueService(
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            repository=repository,
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
                "state": {LOOP_STATE_KEY: LoopHttp(review_queue=service)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = ReviewQueueStack(world, database, writer, consumer, repository, service, client)
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
