"""Entorno de las rutas del catálogo y la marca de regresión (TASK-209, LC-GOB-01 y LC-GOB-09).

``catalog_routes_world`` levanta sobre una base migrada propia (como ``vigia_app``) los servicios
reales: ``CatalogPublicationService`` con ``admission_for`` de LC-GOB-02 y el ``RegressionService``
real como marcador, ``EscritorExpediente`` con la bandeja sincronizada con los eventos del
catálogo, la política de texto libre con el validador mínimo de U-03 y el doble del puerto de
firma que delega en el ``SigningService`` real (``SignerDouble``). La aplicación es la real
(``World().app`` con la cadena fija de middleware y ``ContextAuthorizer``) con solo las rutas del
catálogo instaladas en ``app.state``.

Las vigencias que compara la base (concesiones) salen del mismo reloj simulado, que arranca en la
hora de la base (retro 14). Los topes de la base y de la firma son generosos (retro 15): ninguna
prueba de aquí trata de ellos.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import httpx
from vigia_contracts.models.enumerations import PredicateFamily

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.gates_support import SignerDouble
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.signing_support import bootstrapped_world
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.admission_repository import (
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.admission import AdmissionService
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.application.publication import CatalogPublicationService
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
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

__all__ = [
    "CATALOG_TYPES",
    "REASON",
    "CatalogRoutes",
    "camera",
    "catalog_routes_world",
    "first_standard_body",
    "standard_body",
]

CATALOG_TYPES: Final = (
    "standard_admission_test",
    "catalog_version_published",
    "catalog_standard_retired",
    "single_occupancy_declared",
    "walk_test_regression_marked",
)
REASON: Final = "Cambio sintético del catálogo de la zona"
PRESENCE: Final = {"presence": True}
ENERGY_ON: Final = {"signal_role": "energy", "value": "asserted"}
GUARD_ON: Final = {"signal_role": "guard", "value": "asserted"}
COEXISTENCE: Final = {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0}
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
TEST_LOCK_TIMEOUT_MS: Final = 60_000
TEST_SIGN_TIMEOUT_SECONDS: Final = 120.0
"""Topes generosos (retro 15) para las pruebas que no tratan de los topes."""
WEEK: Final = timedelta(days=7)


def camera(n: int, tag: str = "") -> dict[str, Any]:
    """Una ``ZoneCamera`` del cuerpo de ``PUT /cameras`` (o de ``zone_parameters``)."""
    return {
        "camera_id": str(uuid.UUID(int=30_000 + n, version=4)),
        "code": f"CM{tag}-{n}",
        "role_in_zone": "primary" if n == 0 else "redundant",
        "declared_min_fps": 5.0,
        "stream_reference": f"cam{tag.lower()}-{n}",
    }


def standard_body(
    family: str = "coexistence",
    predicate: Mapping[str, Any] | None = None,
    title: str = "Coexistencia en la celda",
) -> dict[str, Any]:
    return {
        "family": family,
        "title_es": title,
        "declared_text": "Nadie permanece en la celda mientras la máquina está energizada.",
        "predicate": dict(predicate or COEXISTENCE),
        "reason_es": REASON,
    }


def first_standard_body(**changes: Any) -> dict[str, Any]:
    """El primer estándar de la zona con sus parámetros (versión 1)."""
    cameras = [camera(0), camera(1)]
    parameters: dict[str, Any] = {
        "cameras": cameras,
        "minimum_coverage": {"required_count": 1, "required_camera_ids": [cameras[0]["camera_id"]]},
        "signals": [
            {
                "signal_id": str(uuid.UUID(int=40_000, version=4)),
                "code": "SG-1",
                "role": "energy",
                "asserted_level": "high",
                "source": {"reader": "plc-1", "channel": 1},
                "description_es": "Energía de la prensa",
            },
            {
                "signal_id": str(uuid.UUID(int=40_001, version=4)),
                "code": "SG-2",
                "role": "guard",
                "asserted_level": "high",
                "source": {"reader": "plc-1", "channel": 2},
                "description_es": "Resguardo frontal",
            },
        ],
        "thresholds": {"review": 0.4, "publication": 0.8},
        "clip_window": {"pre_seconds": 10, "post_seconds": 10},
        "episode": {"grouping_window_ms": 3000, "max_segment_ms": 900000},
    }
    parameters.update(changes)
    return {**standard_body(), "zone_parameters": parameters}


@dataclass
class CatalogRoutes:
    """La base, los servicios reales y el cliente HTTP de la aplicación."""

    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    signer: SignerDouble
    catalog_repository: PostgresCatalogRepository
    publication: CatalogPublicationService = field(init=False)
    regression: RegressionService = field(init=False)
    client: httpx.AsyncClient = field(init=False)

    def services(
        self, regression_repository: PostgresRegressionRepository | None = None
    ) -> tuple[CatalogPublicationService, RegressionService]:
        """Publicación y regresión reales (``regression_repository`` permite una sonda)."""
        sessions = self.authz.sessions
        regression = RegressionService(
            repository=regression_repository or PostgresRegressionRepository(self.database),
            catalog=self.catalog_repository,
            database=self.database,
            writer=self.writer,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            free_text=self.free_text,
            clock=sessions.clock,
        )
        publication = CatalogPublicationService(
            repository=self.catalog_repository,
            database=self.database,
            writer=self.writer,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            free_text=self.free_text,
            admissions=AdmissionService(
                repository=PostgresAdmissionRepository(self.database),
                database=self.database,
                writer=self.writer,
                authorizer=self.authz.authorizer,
                audit=sessions.audit,
                free_text=self.free_text,
                clock=sessions.clock,
            ),
            signer=self.signer,
            clock=sessions.clock,
            regression_marker=regression,
            sign_timeout_seconds=TEST_SIGN_TIMEOUT_SECONDS,
        )
        return publication, regression

    def install(self) -> None:
        self.publication, self.regression = self.services()
        authz = self.authz
        app = World(clock=authz.sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(self.writer),
                    clock=authz.sessions.clock,
                ),
                "state": {
                    CATALOG_STATE_KEY: CatalogHttp(
                        catalog=self.publication, regression=self.regression
                    )
                },
            },
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def tick(self, seconds: float = 1) -> None:
        self.authz.sessions.clock.advance(seconds)

    # --- Plantas, zonas y personas -------------------------------------------------------------

    def site(self, plants: int = 1, zones: int = 1) -> Site:
        """Un cliente con todas las familias admitidas en cada planta."""
        site = self.authz.add_site(plants=plants, zones_per_plant=zones)
        for plant in site.plants:
            for family in PredicateFamily:
                self.authz.execute(
                    "INSERT INTO catalog.family_admission (admission_id, organization_id,"
                    " plant_id, family, answers, result, evaluated_by, role_in_use,"
                    " evaluated_at, ledger_record_id) VALUES ($1, $2, $3, $4,"
                    ' \'{"standard": true, "remedy": true, "subject": true}\', \'admitted\','
                    " $5, 'administrator', $6, $7)",
                    uuid.uuid4(),
                    site.organization_id,
                    plant,
                    family.value,
                    self.authz.operator_id,
                    self.authz.now(),
                    uuid.uuid4(),
                )
        return site

    def productive(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> None:
        """Las dos compuertas aprobadas: la zona en ``productive`` (proyección de VIG-146)."""
        now = self.authz.now()
        approved = json.dumps({"status": "approved"})
        self.authz.execute(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until) VALUES ($1, $2, $3, $4,"
            " $4, 'productive', $5, '{}', $6)",
            zone_id,
            site.organization_id,
            plant_id,
            approved,
            now,
            now + WEEK,
        )

    def resulting_mode(self, zone_id: uuid.UUID) -> str:
        (row,) = self.fetch(
            "SELECT resulting_mode FROM catalog.zone_gate_state WHERE zone_id = $1", zone_id
        )
        mode: str = row["resulting_mode"]
        return mode

    def person(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, SessionCookie]:
        user_id = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user_id, role, level, scope_id)
        cookie: SessionCookie = self.authz.open_session(site.organization_id, user_id)
        return user_id, cookie

    def member(self, site: Site, *args: Any) -> SessionCookie:
        return self.person(site, *args)[1]

    def context(self, cookie: SessionCookie) -> ScopeContext:
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    @staticmethod
    def system_context(site: Site) -> ScopeContext:
        """Un contexto de U-03 del sistema con alcance de organización (como el del latido)."""
        return unit_context(site.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)

    # --- Peticiones ----------------------------------------------------------------------------

    @staticmethod
    def headers(cookie: SessionCookie) -> dict[str, str]:
        return {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}

    def request(
        self,
        method: str,
        path: str,
        cookie: SessionCookie,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        concession: uuid.UUID | None = None,
    ) -> httpx.Response:
        self.tick()
        headers = self.headers(cookie)
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(method, path, json=json_body, params=params, headers=headers)
        )
        return response

    def installer(
        self, site: Site, level: ScopeLevel = ScopeLevel.ORGANIZATION
    ) -> tuple[SessionCookie, uuid.UUID]:
        """Un instalador del proveedor con una concesión vigente sobre el cliente (la única
        columna con ``commissioning.run``)."""
        installer = self.authz.add_provider_user()
        concession = self.authz.add_concession(
            site.organization_id,
            installer,
            level=level,
            scope_id=None if level is ScopeLevel.ORGANIZATION else next(iter(site.plants)),
            granted_at=self.authz.now() - timedelta(hours=1),
        )
        cookie: SessionCookie = self.authz.open_session(
            self.authz.provider_organization_id, installer
        )
        return cookie, concession

    def audits(self, organization_id: uuid.UUID, concession: uuid.UUID) -> list[str]:
        return [
            row["operation"]
            for row in self.fetch(
                "SELECT operation FROM shared.audit_entry WHERE organization_id = $1"
                " AND actor_concession_id = $2 ORDER BY chain_sequence",
                organization_id,
                concession,
            )
        ]

    def configure(self, cookie: SessionCookie, zone_id: uuid.UUID, **changes: Any) -> Any:
        """La versión 1 de la zona por ``POST /standards`` con sus parámetros."""
        response = self.request(
            "POST", f"/zones/{zone_id}/standards", cookie, first_standard_body(**changes)
        )
        assert response.status_code == 201, response.text
        return response.json()

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def versions(self, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT catalog_version, issued_at, issued_by, reason_es, changed_fields,"
            " envelope::text AS envelope, superseded_at FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 ORDER BY catalog_version",
            zone_id,
        )

    def regression_row(self, zone_id: uuid.UUID) -> Any | None:
        rows = self.fetch(
            "SELECT state, marked_at, cause, catalog_version, model_version,"
            " affected_row_ids::text AS affected_row_ids, ledger_record_id"
            " FROM catalog.walk_test_regression WHERE zone_id = $1",
            zone_id,
        )
        return rows[0] if rows else None

    def records(self, zone_id: uuid.UUID, record_type: str | None = None) -> list[Any]:
        rows = self.fetch(
            "SELECT record_id, record_type, actor_id, actor_role_in_use, occurred_at,"
            " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
            " WHERE scope_zone_id = $1 ORDER BY chain_sequence",
            zone_id,
        )
        return [
            {**dict(r), "content": json.loads(r["content"])}
            for r in rows
            if record_type is None or r["record_type"] == record_type
        ]

    def events(self, zone_id: uuid.UUID, event_name: str | None = None) -> list[Any]:
        rows = self.fetch(
            "SELECT event_name, payload::text AS payload FROM shared.outbox_event"
            " WHERE payload ->> 'zone_id' = $1 ORDER BY created_at, event_id",
            str(zone_id),
        )
        return [r for r in rows if event_name is None or r["event_name"] == event_name]

    def written(self, zone_id: uuid.UUID) -> tuple[int, int, int, int]:
        """Versiones, registros, eventos y fila de regresión que dejó la zona."""
        return (
            len(self.versions(zone_id)),
            len(self.records(zone_id)),
            len(self.events(zone_id)),
            0 if self.regression_row(zone_id) is None else 1,
        )


@contextmanager
def catalog_routes_world(endpoint: PostgresEndpoint, prefix: str) -> Iterator[CatalogRoutes]:
    """El entorno de ``CatalogRoutes`` sobre una base migrada propia."""
    signing = asyncio.run(bootstrapped_world())
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in (
            *U02_RECORD_TYPES,
            *(d for d in CATALOG_RECORD_TYPES if d.record_type in CATALOG_TYPES),
        ):
            registry.register(definition)
        outbox_catalog = OutboxCatalog()
        register_u02_event_types(outbox_catalog.event_types)
        register_catalog_event_types(outbox_catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await outbox_catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        database = app_database(
            sessions.migrated, worker_pool_size=8, lock_timeout_ms=TEST_LOCK_TIMEOUT_MS
        )
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=Outbox(outbox_catalog, sessions.clock),
            clock=sessions.clock,
        )
        world = CatalogRoutes(
            authz,
            database,
            writer,
            free_text,
            SignerDouble(signing),
            PostgresCatalogRepository(database),
        )
        world.install()
        try:
            yield world
        finally:
            authz.run(world.client.aclose())
            authz.run(database.dispose())
