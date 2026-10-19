"""La bandeja de TASK-307 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``inbox_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``: hallazgos ingresados
por las rutas del contrato de U-03 y proyectados), el ``InboxService`` real y la aplicación real
(``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``) con ``LoopHttp`` en
``app.state``. ``InboxStack`` añade:

- personas con ``findings.read`` en la organización, en una planta o en una zona;
- ``finding`` (un hallazgo ingresado y proyectado) y ``row`` (otra fila de ``finding_state``
  copiada de esa, como superusuario, con las columnas que la prueba fija);
- ``zone_in`` (otra zona de una planta), ``node_state`` (el ``communication_state`` que U-03
  proyecta) y ``checkpoint_state`` (el control de la proyección de la planta);
- ``counting`` (las sentencias que la base de la aplicación envía al servidor) y
  ``counting_ports`` (las llamadas a los puertos reales de U-03 y de U-02).

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14). Topes de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import inspect
import secrets
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import event

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.adapters.postgres.assignment_queries import PostgresAssignmentQueries
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.inbox import InboxService
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Database

__all__ = [
    "PATH",
    "InboxStack",
    "StatementLog",
    "Who",
    "counting_ports",
    "inbox_stack",
    "port_classes",
]

PATH: Final = "/loop/findings"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
HOUR: Final = timedelta(hours=1)

Who = SessionCookie


@dataclass
class StatementLog:
    statements: list[str] = field(default_factory=list)

    @property
    def queries(self) -> list[str]:
        """Sin el ``set_config`` con que cada transacción abre su contexto."""
        return [s for s in self.statements if "set_config" not in s]


@dataclass
class InboxStack:
    world: LoopWorld
    database: Database
    service: InboxService
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.world.stack.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.world.stack.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.world.stack.execute(sql, *args)

    def now(self) -> datetime:
        return self.world.stack.now()

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
        cookie: SessionCookie = authz.open_session(organization.organization_id, user)
        return cookie

    # --- siembra ------------------------------------------------------------------------------

    def finding(self, organization: LoopOrganization | None = None) -> IngestedSubject:
        """Un hallazgo ingresado por la ruta del nodo y proyectado (``received``)."""
        finding = self.world.ingest_finding(organization or self.world.a)
        step = self.world.project(finding)
        assert step.applied and step.transition_id == "T01"
        return finding

    def row(self, template: IngestedSubject, **columns: Any) -> uuid.UUID:
        """Otra fila de ``finding_state`` copiada de ``template``; devuelve su identificador."""
        finding_state_id = uuid.uuid4()
        self.world.clone_row(template, finding_state_id=finding_state_id, **columns)
        return finding_state_id

    def zone_in(self, organization: LoopOrganization, plant_id: uuid.UUID) -> uuid.UUID:
        """Otra zona (sin nodo) de ``plant_id``."""
        zone_id = uuid.uuid4()
        self.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            organization.organization_id,
            plant_id,
            f"ZN-{secrets.token_hex(6).upper()}",
            self.now(),
            self.world.stack.authz.operator_id,
        )
        return zone_id

    def node_state(self, organization: LoopOrganization, state: str) -> None:
        """El ``communication_state`` que U-03 proyecta del nodo del sitio."""
        site = organization.site
        self.execute(
            "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id,"
            " software_version, contract_version, model_version, contract_notice,"
            " last_heartbeat_at, communication_state, local_queue, clock, signal_reader,"
            " uptime_seconds, updated_at) VALUES ($1, $2, $3, '1.0.0', '1.0.0', 'modelo-1',"
            " '{\"result\": \"accepted\"}'::jsonb, $4, $5, '{}'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, 1, $4) ON CONFLICT (node_id) DO UPDATE"
            " SET communication_state = EXCLUDED.communication_state",
            site.node_id,
            site.organization_id,
            site.plant_id,
            self.now(),
            state,
        )

    def checkpoint_state(self, organization: LoopOrganization, state: str) -> None:
        self.execute(
            "INSERT INTO loop.loop_projection_checkpoint (organization_id, plant_id, state)"
            " VALUES ($1, $2, $3) ON CONFLICT (organization_id, plant_id)"
            " DO UPDATE SET state = EXCLUDED.state",
            organization.organization_id,
            organization.plant_id,
            state,
        )

    # --- peticiones ---------------------------------------------------------------------------

    def get(self, who: Who, params: Any = None) -> httpx.Response:
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={who.value}"}
        response: httpx.Response = self.run(self.client.get(PATH, params=params, headers=headers))
        return response

    def ids(self, who: Who, params: Any = None) -> list[str]:
        """Los ``finding_state_id`` de todas las páginas, en orden."""
        seen: list[str] = []
        query = dict(params or {})
        while True:
            response = self.get(who, query)
            assert response.status_code == 200, response.text
            body = response.json()
            seen += [item["finding_state_id"] for item in body["items"]]
            if body["next_cursor"] is None:
                return seen
            query["cursor"] = body["next_cursor"]

    # --- instrumentos -------------------------------------------------------------------------

    @contextlib.contextmanager
    def counting(self) -> Iterator[StatementLog]:
        """Las sentencias que la base de la aplicación envía al servidor dentro del bloque."""
        log = StatementLog()
        engines = [
            pool.engine.sync_engine
            for pool in self.database._pools.values()
            if getattr(pool, "engine", None) is not None
        ]

        def before(*arguments: Any) -> None:
            log.statements.append(str(arguments[2]))

        for engine in engines:
            event.listen(engine, "before_cursor_execute", before)
        try:
            yield log
        finally:
            for engine in engines:
                event.remove(engine, "before_cursor_execute", before)


def port_classes(database: Any) -> tuple[type, ...]:
    """Las clases de los puertos reales de U-03 (catálogo, compuertas, flota: ``FleetQueryPort``
    y sus lecturas por zona) y de cobertura de U-02."""
    ports = catalog_query_ports(database)
    return (
        type(ports.catalog),
        type(ports.gates),
        FleetInventory,
        PostgresAssignmentQueries,
        CoverageService,
    )


@contextlib.contextmanager
def counting_ports(monkeypatch: pytest.MonkeyPatch, database: Any) -> Iterator[Counter[str]]:
    """Cuenta cada llamada a un método público asíncrono de los puertos reales."""
    calls: Counter[str] = Counter()

    def counted(label: str, member: Any) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            calls[label] += 1
            return await member(*args, **kwargs)

        return call

    for cls in port_classes(database):
        for name, member in list(vars(cls).items()):
            if not name.startswith("_") and inspect.iscoroutinefunction(member):
                monkeypatch.setattr(cls, name, counted(f"{cls.__name__}.{name}", member))
    yield calls


@contextlib.contextmanager
def inbox_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[InboxStack]:
    with loop_world(endpoint, prefix) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database()
        service = InboxService(database=database, authorizer=authz.authorizer)
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(stack.primary.writer),
                    clock=clock,
                ),
                "state": {LOOP_STATE_KEY: LoopHttp(inbox=service)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = InboxStack(world, database, service, client)
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
