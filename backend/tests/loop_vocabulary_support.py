"""La lista de vocabulario de TASK-350 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``loop_vocabulary_stack`` construye, sobre el ``authz_environment`` de una prueba, el
``EscritorExpediente`` con los tipos de U-02 y los del lazo (``vocabulary_list_changed`` en sus
versiones 1 y 2) y la **política de texto libre de U-04** (``loop_free_text``: los dos validadores
con sus cachés y preparadores sobre PostgreSQL), el ``VocabularyService`` real y la aplicación real
(``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` en
``app.state``. ``VocabularyStack`` da las personas, las peticiones y lo que quedó escrito
(registros ``vocabulary_list_changed``, eventos, la fila de la lista y los términos).

Solo datos generados (NFR-CTR-43). Las marcas salen del reloj simulado, que arranca en la hora de
la base truncada al milisegundo (retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.writer_support import save_record_types, unit_context
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.loop.registration import loop_free_text
from vigia_platform.loop.vocabulary.repository import PostgresVocabularyRepository
from vigia_platform.loop.vocabulary.service import VocabularyService
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "CHANGES",
    "CONCESSION_HEADER",
    "PATH",
    "REASON",
    "VocabularyStack",
    "Who",
    "loop_vocabulary_stack",
    "term",
    "u04_free_text",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
HOUR: Final = timedelta(hours=1)
LOCK_TIMEOUT_MS: Final = 60_000
REASON: Final = "Ajuste acordado en el comité de seguridad"
PATH: Final = "/loop/vocabulary"
CHANGES: Final = "/loop/vocabulary/changes"

Who = tuple[SessionCookie, uuid.UUID | None]


def term(
    value: str,
    *,
    form: str = "lemma",
    category: str = "absence_claim",
    suggestion_es: str = "Describa lo observado en la zona",
) -> dict[str, str]:
    """Un término del cuerpo de ``POST /loop/vocabulary/changes``."""
    return {"category": category, "form": form, "value": value, "suggestion_es": suggestion_es}


def u04_free_text() -> FreeTextPolicyRegistry:
    """La política de texto libre que compone U-04: los dos validadores sobre PostgreSQL."""
    registry = FreeTextPolicyRegistry()
    loop_free_text(registry)
    registry.seal()
    return registry


@dataclass
class VocabularyStack:
    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    service: VocabularyService
    app: Any
    client: httpx.AsyncClient

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.authz.sessions.clock.advance(seconds)

    def service_like(self, **changes: Any) -> VocabularyService:
        """Otro servicio sobre la misma base (p. ej. con otro repositorio)."""
        arguments: dict[str, Any] = {
            "repository": PostgresVocabularyRepository(self.database),
            "database": self.database,
            "writer": self.writer,
            "authorizer": self.authz.authorizer,
            "clock": self.authz.sessions.clock,
            **changes,
        }
        return VocabularyService(**arguments)

    def context(self, who: Who) -> ScopeContext:
        """El contexto de la sesión de ``who`` (y su concesión), como lo construye la cadena."""
        cookie, concession = who
        scope = self.run(self.authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    # --- Personas -------------------------------------------------------------------------------

    def site(self, plants: int = 1) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=1)

    def member(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Who:
        user = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user, role, level, scope)
        return self.authz.open_session(site.organization_id, user), None

    def installer(self, site: Site) -> Who:
        """Instalador del proveedor con una concesión vigente sobre la organización de ``site``."""
        authz = self.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            site.organization_id, installer, granted_at=authz.now() - HOUR
        )
        return authz.open_session(authz.provider_organization_id, installer), concession

    # --- Peticiones -----------------------------------------------------------------------------

    def send(self, who: Who, method: str, path: str, body: Any = None) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(method, path, json=body, headers=headers)
        )
        return response

    def get(self, who: Who) -> httpx.Response:
        return self.send(who, "GET", PATH)

    def post(self, who: Who, body: Any) -> httpx.Response:
        return self.send(who, "POST", CHANGES, body)

    # --- Lo que quedó escrito -------------------------------------------------------------------

    def records(self, organization: uuid.UUID) -> list[dict[str, Any]]:
        """Los ``vocabulary_list_changed`` de la organización, en el orden de la cadena."""
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, schema_version, plant_id, actor_role_in_use, actor_unit,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE record_type = 'vocabulary_list_changed' AND organization_id = $1"
                " ORDER BY chain_sequence",
                organization,
            )
        ]

    def events(self, organization: uuid.UUID) -> list[dict[str, Any]]:
        """Las cargas de los eventos ``vocabulary_list_changed`` de la organización."""
        return [
            json.loads(row["payload"])
            for row in self.fetch(
                "SELECT payload::text AS payload FROM shared.outbox_event"
                " WHERE event_name = 'vocabulary_list_changed' AND organization_id = $1"
                " ORDER BY created_at, event_id",
                organization,
            )
        ]

    def row(self, organization: uuid.UUID) -> Any:
        rows = self.fetch(
            "SELECT * FROM loop.blocked_vocabulary_list WHERE organization_id = $1", organization
        )
        return rows[0] if rows else None

    def terms(self, organization: uuid.UUID) -> list[Any]:
        """Todos los términos de la organización (vigentes y retirados)."""
        return self.fetch(
            "SELECT * FROM loop.blocked_term WHERE organization_id = $1"
            " ORDER BY added_in_version, term_id",
            organization,
        )


@contextlib.contextmanager
def loop_vocabulary_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[VocabularyStack]:
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(to_millisecond(now["now"]))
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        register_loop_record_types(registry)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        register_loop_event_types(catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = u04_free_text()
        database = app_database(
            sessions.migrated, worker_pool_size=pool, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=Outbox(catalog, sessions.clock),
            clock=sessions.clock,
        )
        service = VocabularyService(
            repository=PostgresVocabularyRepository(database),
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            clock=sessions.clock,
        )
        app = World(clock=sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=sessions.clock,
                ),
                "state": {LOOP_STATE_KEY: LoopHttp(vocabulary=service)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        stack = VocabularyStack(authz, database, writer, free_text, service, app, client)
        try:
            yield stack
        finally:
            authz.run(client.aclose())
            authz.run(database.dispose())
