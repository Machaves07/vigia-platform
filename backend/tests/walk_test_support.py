"""Entorno de las pruebas de la sesión de walk-test (TASK-214, LC-GOB-06).

Sobre ``agreements_world`` (servicios reales de ``catalog.gates`` como ``vigia_app``, montaje con
un acta de alcance real) añade ``WalkTestService`` con ``HierarchyService`` real (nodo asignado y
responsables) y la aplicación real (``World().app`` con la cadena fija de middleware y
``ContextAuthorizer``) con las rutas del walk-test instaladas en ``app.state``.

``Mounted`` deja una zona lista para abrir la sesión: catálogo vigente con ``standards`` estándares
sintéticos (``derive_matrix`` lee su predicado), nodo asignado y montaje ``approved``, y el
instalador bajo concesión (``commissioning.run`` solo lo tiene ``provider_installer``) con su
contexto y su cookie. Cada pieza se puede omitir para probar su guarda.

La inactividad se simula moviendo hacia atrás las marcas de la sesión (``age``), no el reloj
compartido: así las sesiones de usuario y las concesiones siguen vigentes y ninguna marca se
compara con la hora real de la base. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import httpx

from tests.agreements_support import (
    CONCESSION_HEADER,
    SAME_ORIGIN,
    AgreementsWorld,
    agreements_world,
)
from tests.api_support import World
from tests.authz_support import Site
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.walk_test import WalkTestService
from vigia_platform.catalog.domain.steps import WalkTestStep
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.registry import RecordType
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ScopeContext, ScopeLevel

REASON: Final = "Reanudamos el walk-test tras la parada de mantenimiento"
CORRECTION: Final = "Se olvidó abrir el paso al llegar a la celda"
SIGNAL_ROLES: Final = ("energy", "guard", "mode", "door")


def standards(count: int) -> list[dict[str, Any]]:
    """``count`` estándares sintéticos con una combinación de condiciones distinta cada uno."""
    return [
        {
            "standard_id": str(uuid.UUID(int=70_000 + index, version=4)),
            "version": 1 + index % 3,
            "family": "coexistence",
            "predicate": {
                "all_of": [
                    {"presence": True},
                    {"signal_role": SIGNAL_ROLES[index % 4], "value": "asserted"},
                ]
            },
        }
        for index in range(count)
    ]


@dataclass
class Mounted:
    """Una zona lista para abrir la sesión y el instalador que la abre."""

    site: Site
    plant: uuid.UUID
    zone: uuid.UUID
    installer: ScopeContext
    cookie: SessionCookie
    concession: uuid.UUID
    catalog: dict[str, Any]


@dataclass
class WalkTestWorld:
    agreements_world: AgreementsWorld
    repository: PostgresWalkTestRepository = field(default_factory=PostgresWalkTestRepository)
    service: WalkTestService = field(init=False)
    client: httpx.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self.service = self.build()

    @property
    def a(self) -> AgreementsWorld:
        return self.agreements_world

    def build(self, **changes: Any) -> WalkTestService:
        g = self.a.g
        fields: dict[str, Any] = {
            "repository": self.repository,
            "catalog": PostgresCatalogRepository(g.database),
            "gates": g.gates,
            "nodes": g.hierarchy,
            "identity": g.hierarchy,
            "database": g.database,
            "writer": g.writer,
            "audit": g.authz.sessions.audit,
            "free_text": g.free_text,
            "clock": g.authz.sessions.clock,
        }
        fields.update(changes)
        return WalkTestService(**fields)

    def install(self) -> None:
        g = self.a.g
        authz = g.authz
        app = World(clock=authz.sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(g.writer),
                    clock=authz.sessions.clock,
                ),
                "state": {CATALOG_STATE_KEY: CatalogHttp(gates=g.gates, walk_tests=self.service)},
            },
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.a.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.a.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.a.execute(sql, *args)

    def advance(self, seconds: float = 1.0) -> None:
        self.a.g.advance(seconds)

    def request(
        self,
        method: str,
        path: str,
        mounted: Mounted,
        json_body: Any = None,
    ) -> httpx.Response:
        self.advance()
        headers = {
            **SAME_ORIGIN,
            "Cookie": f"{SESSION_COOKIE_NAME}={mounted.cookie.value}",
            CONCESSION_HEADER: str(mounted.concession),
        }
        response: httpx.Response = self.run(
            self.client.request(method, path, json=json_body, headers=headers)
        )
        return response

    # --- Zonas ---------------------------------------------------------------------------------

    def mounted(
        self,
        *,
        count: int = 2,
        mount: bool = True,
        node: bool = True,
        site: Site | None = None,
        zone_index: int = 0,
        installer_level: ScopeLevel = ScopeLevel.ORGANIZATION,
        installer_scope: uuid.UUID | None = None,
    ) -> Mounted:
        """Catálogo con ``count`` estándares, nodo y montaje aprobado (salvo lo omitido)."""
        g = self.a.g
        site = site or g.site()
        plant, zone = site.zones()[zone_index]
        catalog = {"standards": standards(count)}
        installer, cookie, concession = self.a.installer_session(
            site, installer_level, installer_scope
        )
        if mount:
            # El acta de alcance exige nodo: si se omite, se libera después del montaje.
            self.a.mount(site, plant, zone, installer, catalog)
            if not node:
                self.unassign(zone)
        else:
            g.equip(site, plant, zone, payload=catalog, node=node)
        return Mounted(site, plant, zone, installer, cookie, concession, catalog)

    def unassign(self, zone: uuid.UUID) -> None:
        self.execute(
            "UPDATE identity.zone_node_assignment SET unassigned_at = assigned_at"
            " + interval '1 millisecond' WHERE zone_id = $1 AND unassigned_at IS NULL",
            zone,
        )

    # --- Servicio ------------------------------------------------------------------------------

    def open(self, mounted: Mounted, passes: int = 3) -> WalkTestSession:
        self.advance()
        session: WalkTestSession = self.run(
            self.service.open(mounted.installer, mounted.zone, passes)
        )
        return session

    def start(self, mounted: Mounted, session_id: uuid.UUID, kind: str = "framing") -> WalkTestStep:
        self.advance()
        step: WalkTestStep = self.run(
            self.service.start_step(
                mounted.installer, session_id, kind, uuid.UUID(str(mounted.installer.actor.id))
            )
        )
        return step

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def age(self, session_id: uuid.UUID, by: timedelta) -> None:
        """Mueve hacia atrás las marcas de la sesión (inactividad simulada)."""
        self.execute(
            "UPDATE catalog.walk_test_session SET started_at = started_at - $2::interval,"
            " last_activity_at = last_activity_at - $2::interval WHERE session_id = $1",
            session_id,
            by,
        )

    def session_row(self, session_id: uuid.UUID) -> Any:
        (row,) = self.fetch(
            "SELECT status, last_activity_at, started_at, reopened_at, reopened_by,"
            " reopen_reason_es, catalog_version, node_id, passes_per_cell,"
            " jsonb_array_length(matrix_rows) AS rows FROM catalog.walk_test_session"
            " WHERE session_id = $1",
            session_id,
        )
        return row

    def open_sessions(self, zone: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT session_id, status FROM catalog.walk_test_session"
            " WHERE zone_id = $1 AND status IN ('in_progress', 'reopened')",
            zone,
        )

    def step_rows(self, step_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT started_at, ended_at, correction::text AS correction, responsible_user_id"
            " FROM catalog.walk_test_step WHERE step_id = $1",
            step_id,
        )

    def pass_rows(self, session_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT pass_id, row_id, result, evidence_ref, recorded_by FROM catalog.walk_test_pass"
            " WHERE session_id = $1 ORDER BY recorded_at, pass_id",
            session_id,
        )

    def step_records(self, zone: uuid.UUID, step_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(r), "content": json.loads(r["content"])}
            for r in self.a.g.records_of(zone)
            if r["record_type"] == "commissioning_step" and r["source_key"] == str(step_id)
        ]

    def reopen_trail(self, session_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT actor_id, scope_zone_id, convert_from(filters, 'UTF8') AS filters"
            " FROM shared.audit_entry"
            " WHERE operation = 'walk_test_reopened' AND resource_id = $1"
            " ORDER BY chain_sequence",
            session_id,
        )


@contextmanager
def walk_test_world(
    endpoint: PostgresEndpoint, prefix: str, extra_types: Sequence[RecordType] = ()
) -> Iterator[WalkTestWorld]:
    """El entorno de ``WalkTestWorld`` sobre una base migrada propia, con su cliente HTTP."""
    with agreements_world(endpoint, prefix, extra_types) as agreements:
        world = WalkTestWorld(agreements)
        world.install()
        try:
            yield world
        finally:
            agreements.run(world.client.aclose())
