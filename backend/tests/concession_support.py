"""Entorno de prueba de ``identity.concessions`` (TASK-127) contra PostgreSQL 16 real.

``concession_environment(endpoint, prefix)``: el entorno de ``identity.authz`` (TASK-125: base
migrada y sembrada, ``shared.db`` como ``vigia_app``, constructores de contexto reales y
``Authorizer``) con los tipos de U-02 registrados en ``ledger.record_type``, el
``EscritorExpediente`` real, ``PostgresConcessionStore`` y ``ConcessionService``.

El reloj simulado arranca en el ``now()`` de la base: la RLS de ``nuc_0009`` compara la
vigencia con la hora de la base al dar de alta una concesión, así que la prueba no depende del
día en que corra.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import build_registry, save_record_types
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.application.concessions import ConcessionService
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier, ObjectHead
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel

REASON: Final = "Mantenimiento sintético del nodo de la línea 2"


class NoStorage:
    """Las concesiones no llevan evidencias: el escritor nunca debe consultar el almacén."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise AssertionError("las concesiones no tienen evidencias")


class PeriodicTaskProbe:
    """``OrganizationTask`` de ``context_for_organization``."""

    task_name = "expire_concessions"
    unit = ActorUnit.U02


@dataclass
class ConcessionEnvironment:
    authz: AuthzEnvironment
    writer: EscritorExpediente
    store: PostgresConcessionStore
    service: ConcessionService
    provider_queries: LedgerProviderQueryLedger

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def now(self) -> datetime:
        return self.authz.now()

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return self.authz.provider_organization_id

    def database_now(self) -> datetime:
        (row,) = self.fetch("SELECT now() AS now")
        moment: datetime = row["now"]
        return moment

    # --- Personas y sesiones -------------------------------------------------------------------

    def provider_user(self, role: Role = Role.PROVIDER_INSTALLER) -> uuid.UUID:
        return self.authz.add_provider_user(role)

    def client_user(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        user_id = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user_id, role, level, scope_id)
        return user_id

    def session_context(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, concession_id: uuid.UUID | None = None
    ) -> ScopeContext:
        """El contexto de una sesión nueva de ``user_id`` (bajo concesión si se da)."""
        cookie = self.authz.open_session(organization_id, user_id)
        scope = self.run(
            self.authz.contexts.context_from_session(cookie, concession_id=concession_id)
        )
        context: ScopeContext = scope.context
        return context

    def provider_context(self, user_id: uuid.UUID) -> ScopeContext:
        return self.session_context(self.provider_organization_id, user_id)

    def iteration_context(self, organization_id: uuid.UUID) -> ScopeContext:
        return self.authz.contexts.context_for_organization(PeriodicTaskProbe(), organization_id)

    def set_limits(self, organization_id: uuid.UUID, max_days: int, default_days: int) -> None:
        self.execute(
            "UPDATE identity.organization SET concession_default_days = 1,"
            " concession_max_days = $2 WHERE organization_id = $1",
            organization_id,
            max_days,
        )
        self.execute(
            "UPDATE identity.organization SET concession_default_days = $2"
            " WHERE organization_id = $1",
            organization_id,
            default_days,
        )

    def expire_due(self, organization_id: uuid.UUID) -> int:
        async def run() -> int:
            context = self.iteration_context(organization_id)
            async with self.authz.sessions.database.transaction(context) as transaction:
                closed: int = await self.service.expire_due(transaction)
            return closed

        result: int = self.run(run())
        return result


@contextlib.contextmanager
def concession_environment(
    endpoint: PostgresEndpoint, prefix: str
) -> Iterator[ConcessionEnvironment]:
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        registry = build_registry()

        async def synchronize() -> None:
            async with sessions.database.transaction(
                make_context(kind=ActorKind.SYSTEM)
            ) as transaction:
                await save_record_types(transaction, registry)

        authz.run(synchronize())
        registry.seal()
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=FreeTextPolicyRegistry(),
            evidence=EvidenceVerifier(NoStorage(), sessions.clock),
            outbox=sessions.outbox,
            clock=sessions.clock,
        )
        store = PostgresConcessionStore(database=sessions.database, audit=sessions.audit)
        service = ConcessionService(
            store=store,
            writer=writer,
            authorizer=authz.authorizer,
            contexts=authz.contexts,
            clock=sessions.clock,
        )
        environment = ConcessionEnvironment(
            authz, writer, store, service, LedgerProviderQueryLedger(writer)
        )
        sessions.clock.set(environment.database_now())
        yield environment
