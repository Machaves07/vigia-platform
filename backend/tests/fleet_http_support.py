"""La flota de TASK-218 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP (``create_app``).

``fleet_stack`` construye, sobre el ``authz_environment`` de una prueba, los servicios reales
(``NodeDeclarationService``, ``EnrollmentCodeService`` y ``NodeRevocationService`` con
``HierarchyService`` de U-02 como ``IdentityCommandPort``), el ``EscritorExpediente`` con los tipos
de U-02 y los cinco de la flota que escriben, la bandeja con los eventos ``node_revoked`` y
``node_decommissioned``, y la aplicación real con la cadena fija de middleware y
``ContextAuthorizer``. ``FleetStack`` da las personas (instalador del proveedor bajo concesión,
miembros del cliente), las peticiones y lo que quedó escrito. Desde TASK-224, también el inventario
(``FleetInventory``) y los umbrales por planta (``FleetThresholdsService``).

Solo datos generados (NFR-CTR-43). Las marcas salen del reloj simulado, que arranca en la hora de
la base (retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.fleet_support import RootObjects, root_bundle
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, issue
from tests.node_api_support import DAY, TestAuthority
from tests.outbox_support import app_database
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY, FleetHttp
from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.common import FleetDependencies
from vigia_platform.fleet.application.enrollment_codes import BundleRoots, EnrollmentCodeService
from vigia_platform.fleet.application.fleet_thresholds import FleetThresholdsService
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.application.node_declaration import NodeDeclarationService
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.domain.enrollment_attempt import SourceIpHasher
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.runtime.units import _fleet_event_types, _fleet_record_types

__all__ = ["CONCESSION_HEADER", "FleetStack", "fleet_stack"]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
HOUR: Final = timedelta(hours=1)
LOCK_TIMEOUT_MS: Final = 60_000
REASON: Final = "Equipo retirado por mantenimiento"


@dataclass
class FleetStack:
    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    deps: FleetDependencies
    roots: RootObjects
    hash_key: bytes
    app: Any
    client: httpx.AsyncClient
    services: FleetHttp
    logs: list[str] = field(default_factory=list)

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.authz.sessions.clock.advance(seconds)

    def context(self, who: tuple[SessionCookie, uuid.UUID | None]) -> ScopeContext:
        """El contexto de la sesión de ``who`` (y su concesión), como lo construye la cadena."""
        cookie, concession = who
        scope = self.run(self.authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    def enroll(
        self, node: uuid.UUID | str, plant: uuid.UUID, zone: uuid.UUID, **issue_args: Any
    ) -> tuple[Any, uuid.UUID]:
        """Simula el alta aceptada (VIG-151): nodo ``enrolled`` con una credencial ``active``
        de 365 días (``issue_args`` la cambia, p. ej. ``expires_at`` ya pasado)."""
        node_id = uuid.UUID(str(node))
        (row,) = self.fetch(
            "SELECT organization_id, declared_by FROM fleet.node_fleet_record WHERE node_id = $1",
            node_id,
        )
        now = self.authz.now()
        self.execute(
            "UPDATE identity.node_identity SET status = 'enrolled' WHERE node_id = $1", node_id
        )
        self.execute(
            "UPDATE fleet.node_fleet_record SET enrolled_at = $2 WHERE node_id = $1", node_id, now
        )
        db_node = DbNode(
            node_id,
            uuid.UUID(str(row["organization_id"])),
            plant,
            zone,
            uuid.UUID(str(row["declared_by"])),
        )
        certificate, credential = self.run(
            issue(self.authz.sessions.admin, TestAuthority(), db_node, now - DAY, **issue_args)
        )
        return certificate, credential

    def codes_service(self, **changes: Any) -> EnrollmentCodeService:
        """Otro ``EnrollmentCodeService`` sobre las mismas dependencias (otra instancia)."""
        deps = FleetDependencies(
            **{
                **{name: getattr(self.deps, name) for name in self.deps.__dataclass_fields__},
                **changes,
            }
        )
        return EnrollmentCodeService(
            deps, roots=BundleRoots(self.roots), source_hasher=SourceIpHasher(self.hash_key)
        )

    # --- Personas -------------------------------------------------------------------------------

    def site(self, plants: int = 2, zones: int = 3) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones)

    def installer(
        self,
        site: Site,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> tuple[SessionCookie, uuid.UUID]:
        """Instalador del proveedor con una concesión vigente sobre ``site`` (o una planta)."""
        authz = self.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            site.organization_id, installer, level=level, scope_id=scope,
            granted_at=authz.now() - HOUR,
        )  # fmt: skip
        return authz.open_session(authz.provider_organization_id, installer), concession

    def member(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> tuple[SessionCookie, None]:
        user = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user, role, level, scope)
        return self.authz.open_session(site.organization_id, user), None

    # --- Peticiones -----------------------------------------------------------------------------

    def send(
        self,
        who: tuple[SessionCookie, uuid.UUID | None],
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        # Un segundo entre peticiones: las marcas de dos operaciones nunca empatan (la retirada de
        # una zona exige ``unassigned_at > assigned_at``; los códigos se ordenan por emisión).
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(method, path, json=body, params=params, headers=headers)
        )
        return response

    def declare(
        self, who: Any, plant: uuid.UUID, zones: list[uuid.UUID], **extra: Any
    ) -> httpx.Response:
        body = {"code": new_code(), "zone_ids": [str(z) for z in zones], **extra}
        return self.send(who, "POST", f"/plants/{plant}/nodes", body)

    def issue(self, who: Any, node: uuid.UUID | str) -> httpx.Response:
        return self.send(who, "POST", f"/nodes/{node}/enrollment-codes")

    def revoke(self, who: Any, node: uuid.UUID | str, reason: str = REASON) -> httpx.Response:
        return self.send(who, "POST", f"/nodes/{node}/revocation", {"reason_es": reason})

    def decommission(self, who: Any, node: uuid.UUID | str, reason: str = REASON) -> httpx.Response:
        return self.send(who, "POST", f"/nodes/{node}/decommission", {"reason_es": reason})

    # --- Lo que quedó escrito -------------------------------------------------------------------

    def node_row(self, node: uuid.UUID | str) -> Any:
        rows = self.fetch(
            "SELECT n.status, f.revoked_at, f.revocation_reason_es, f.decommissioned_at,"
            " f.replaces_node_id, f.declared_by FROM identity.node_identity AS n"
            " LEFT JOIN fleet.node_fleet_record AS f ON f.node_id = n.node_id"
            " WHERE n.node_id = $1",
            uuid.UUID(str(node)),
        )
        return rows[0] if rows else None

    def codes(self, node: uuid.UUID | str) -> list[Any]:
        return self.fetch(
            "SELECT code_id, status, code_hash, code_salt, issued_at, expires_at, disclosed_at,"
            " ledger_record_id FROM fleet.enrollment_code WHERE node_id = $1"
            " ORDER BY issued_at, code_id",
            uuid.UUID(str(node)),
        )

    def records(self, record_type: str, organization_id: uuid.UUID) -> list[Any]:
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, plant_id, source_key, schema_version,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE record_type = $1 AND organization_id = $2 ORDER BY chain_sequence",
                record_type,
                organization_id,
            )
        ]

    def events(self, event_name: str, organization_id: uuid.UUID) -> list[Any]:
        return [
            json.loads(row["payload"])
            for row in self.fetch(
                "SELECT payload::text AS payload FROM shared.outbox_event"
                " WHERE event_name = $1 AND organization_id = $2 ORDER BY created_at, event_id",
                event_name,
                organization_id,
            )
        ]

    def audit(self, organization_id: uuid.UUID, operation: str) -> list[Any]:
        return self.fetch(
            "SELECT operation, outcome, actor_concession_id, scope_plant_id, resource_id"
            " FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2"
            " ORDER BY chain_sequence",
            organization_id,
            operation,
        )

    def revocation_state(self) -> Any:
        (row,) = self.fetch("SELECT * FROM fleet.revocation_list_state")
        return row


def new_code() -> str:
    return f"ND-{secrets.token_hex(4).upper()}"


@contextlib.contextmanager
def fleet_stack(endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8) -> Iterator[FleetStack]:
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        _fleet_record_types(registry)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        _fleet_event_types(catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        database = app_database(
            sessions.migrated, worker_pool_size=pool, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        outbox = Outbox(catalog, sessions.clock)
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=outbox,
            clock=sessions.clock,
        )
        identity = HierarchyService(
            IdentityDependencies(
                database=database,
                writer=writer,
                audit=sessions.audit,
                outbox=outbox,
                authorizer=authz.authorizer,
                free_text=free_text,
                clock=sessions.clock,
                provider_organization_id=authz.provider_organization_id,
            )
        )
        deps = FleetDependencies(
            database=database,
            writer=writer,
            audit=sessions.audit,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=sessions.clock,
            identity=identity,
            nodes=PostgresNodeFleetStore(database),
            enrollment=PostgresEnrollmentStore(),
        )
        roots = RootObjects(root_bundle(1))
        hash_key = secrets.token_bytes(32)
        services = FleetHttp(
            declarations=NodeDeclarationService(deps),
            enrollment_codes=EnrollmentCodeService(
                deps, roots=BundleRoots(roots), source_hasher=SourceIpHasher(hash_key)
            ),
            revocations=NodeRevocationService(deps),
            inventory=FleetInventory(
                database=database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=sessions.clock,
                provider_organization_id=authz.provider_organization_id,
            ),
            thresholds=FleetThresholdsService(
                database=database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=sessions.clock,
            ),
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
                "state": {FLEET_STATE_KEY: services},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        stack = FleetStack(
            authz, database, writer, free_text, deps, roots, hash_key, app, client, services
        )
        try:
            yield stack
        finally:
            authz.run(client.aclose())
            authz.run(database.dispose())
