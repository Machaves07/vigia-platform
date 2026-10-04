"""Entorno de prueba de ``identity.authz`` (TASK-125) contra PostgreSQL 16 real.

- ``authz_environment(endpoint, prefix)``: el entorno de sesiones de TASK-124 (base migrada y
  sembrada, ``shared.db`` como ``vigia_app``, reloj simulado) con los constructores de contexto
  reales (``ScopeContexts`` sobre ``PostgresContextStore``), el ``Authorizer`` con su auditoría y
  un contador de sentencias por motor.
- Altas directas como superusuario (datos generados, NFR-CTR-43): organizaciones con plantas y
  zonas, usuarios, asignaciones, sesiones y concesiones, con los instantes que pida la prueba.
- ``sealed_context``: contextos con ``allowed_scopes`` arbitrarios para las propiedades puras (el
  constructor privado, como ``tests/factories``).
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import event

from tests.factories import session_hash, uuid7
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import SessionEnvironment, session_environment
from vigia_platform.identity.adapters.authz_store import (
    PostgresAuthorizationAudit,
    PostgresContextStore,
)
from vigia_platform.identity.auth.sessions import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    SessionCookie,
    new_session_cookie,
)
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)
from vigia_platform.shared.db import Database

SYSTEM_ACTOR_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000125")
CLIENT_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.LINE_MANAGER,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.COPASST,
)
"""Los cinco roles asignables en una organización cliente (BR-NUC-11)."""


def sealed_context(
    organization_id: uuid.UUID,
    scopes: Sequence[AllowedScope],
    *,
    kind: ActorKind = ActorKind.USER,
    concession_id: uuid.UUID | None = None,
    origin: ContextOrigin = ContextOrigin.SESSION,
) -> ScopeContext:
    """Contexto con ``scopes`` tal cual (solo pruebas)."""
    actor = Actor(
        kind=kind,
        id=uuid.uuid4(),
        display_name_snapshot="Persona sintética",
        unit=ActorUnit.U02,
        concession_id=concession_id,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=origin,
        allowed_scopes=scopes,
        correlation_id=uuid7(),
        session_id_hash=session_hash() if origin is ContextOrigin.SESSION else None,
    )


@dataclass(frozen=True)
class Site:
    """Una organización cliente con plantas y sus zonas."""

    organization_id: uuid.UUID
    plants: dict[uuid.UUID, tuple[uuid.UUID, ...]]

    def zones(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return [(plant, zone) for plant, zones in self.plants.items() for zone in zones]


@dataclass
class StatementLog:
    """Sentencias que llegaron al servidor por el motor (``before_cursor_execute``)."""

    statements: list[str] = field(default_factory=list)

    def data_statements(self) -> list[str]:
        """Sin el ``set_config`` de las tres variables que fija la apertura de cada transacción."""
        return [s for s in self.statements if "set_config('vigia.organization_id'" not in s]


@dataclass
class AuthzEnvironment:
    sessions: SessionEnvironment
    contexts: ScopeContexts
    store: PostgresContextStore
    audit: PostgresAuthorizationAudit
    authorizer: Authorizer
    log: StatementLog

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return self.sessions.seed.provider_organization_id

    @property
    def installer_id(self) -> uuid.UUID:
        return self.sessions.seed.installer_id

    @property
    def operator_id(self) -> uuid.UUID:
        return self.sessions.seed.operator_id

    def run(self, awaitable: Any) -> Any:
        return self.sessions.run(awaitable)

    def now(self) -> datetime:
        return self.sessions.clock.now()

    def execute(self, sql: str, *args: Any) -> None:
        self.run(self.sessions.admin.execute(sql, *args))

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.sessions.fetch(sql, *args)

    # --- Altas ---------------------------------------------------------------------------------

    def add_site(self, plants: int = 2, zones_per_plant: int = 2) -> Site:
        organization_id = self.sessions.add_organization()
        layout: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
        for _ in range(plants):
            plant_id = uuid.uuid4()
            self.execute(
                "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
                " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
                " 'Planta sintética', 'CO', 'us-east-1', 'America/Bogota', $4, $5)",
                plant_id,
                organization_id,
                f"PL-{secrets.token_hex(8).upper()}",
                BASE_TIME,
                self.operator_id,
            )
            zones = tuple(uuid.uuid4() for _ in range(zones_per_plant))
            for zone_id in zones:
                self.add_zone(organization_id, plant_id, zone_id)
            layout[plant_id] = zones
        return Site(organization_id, layout)

    def add_zone(self, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID) -> None:
        self.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
            " created_at, created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            organization_id,
            plant_id,
            f"ZN-{secrets.token_hex(8).upper()}",
            BASE_TIME,
            self.operator_id,
        )

    def add_user(self, organization_id: uuid.UUID, *, status: str = "active") -> uuid.UUID:
        return self.sessions.add_user(organization_id, status=status).user_id

    def add_provider_user(self, role: Role = Role.PROVIDER_INSTALLER) -> uuid.UUID:
        user_id = self.add_user(self.provider_organization_id)
        self.assign(self.provider_organization_id, user_id, role)
        return user_id

    def assign(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        role: Role,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        assignment_id = uuid7()
        self.execute(
            "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
            " scope_level, scope_id, assigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            assignment_id,
            organization_id,
            user_id,
            role.value,
            level.value,
            organization_id if scope_id is None else scope_id,
            BASE_TIME,
            self.operator_id,
        )
        return assignment_id

    def remove_assignment(self, assignment_id: uuid.UUID) -> None:
        self.execute(
            "UPDATE identity.role_assignment SET removed_at = $2, removed_by = $3"
            " WHERE assignment_id = $1",
            assignment_id,
            max(self.now(), BASE_TIME),
            self.operator_id,
        )

    def open_session(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        at: datetime | None = None,
        verified: bool = True,
    ) -> SessionCookie:
        """Sesión nueva creada en ``at`` (por defecto, ahora) con la cookie correspondiente."""
        created = at or self.now()
        cookie = new_session_cookie(organization_id)
        self.execute(
            "INSERT INTO identity.session (session_id_hash, user_id, organization_id, created_at,"
            " last_seen_at, idle_expires_at, absolute_expires_at, second_factor_verified,"
            " origin_hash) VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8)",
            cookie.session_id_hash,
            user_id,
            organization_id,
            created,
            created + IDLE_TIMEOUT,
            created + ABSOLUTE_TIMEOUT,
            verified,
            session_hash(),
        )
        return cookie

    def add_concession(
        self,
        client_organization_id: uuid.UUID,
        provider_user_id: uuid.UUID,
        *,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
        granted_at: datetime | None = None,
        duration: timedelta = timedelta(days=7),
        status: str = "active",
        revoked_at: datetime | None = None,
    ) -> uuid.UUID:
        concession_id = uuid7()
        granted = granted_at or self.now() - timedelta(hours=1)
        revoked = status == "revoked"
        self.execute(
            "INSERT INTO identity.provider_concession (concession_id, organization_id,"
            " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
            " granted_at, expires_at, status, revoked_at, revoked_by, revoked_by_side)"
            " VALUES ($1, $2, $3, $4, $5, $6, 'Mantenimiento sintético del nodo', $7, $8, $9,"
            " $10, $11, $12)",
            concession_id,
            client_organization_id,
            provider_user_id,
            self.provider_organization_id,
            level.value,
            client_organization_id if scope_id is None else scope_id,
            granted,
            granted + duration,
            status,
            (revoked_at or granted) if revoked else None,
            self.operator_id if revoked else None,
            "client" if revoked else None,
        )
        return concession_id

    def revoke_concession(self, concession_id: uuid.UUID, at: datetime) -> None:
        self.execute(
            "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = $2,"
            " revoked_by = $3, revoked_by_side = 'client' WHERE concession_id = $1",
            concession_id,
            at,
            self.operator_id,
        )

    def set_user_status(self, user_id: uuid.UUID, status: str) -> None:
        self.execute(
            "UPDATE identity.user_account SET status = $2,"
            " deactivated_at = CASE WHEN $2 = 'deactivated' THEN $3::timestamptz END"
            " WHERE user_id = $1",
            user_id,
            status,
            BASE_TIME,
        )

    def set_organization_status(self, organization_id: uuid.UUID, status: str) -> None:
        self.execute(
            "UPDATE identity.organization SET status = $2 WHERE organization_id = $1",
            organization_id,
            status,
        )

    def close_session(self, cookie: SessionCookie) -> bool:
        closed: bool = self.run(self.sessions.sessions().close(cookie))
        return closed


def _log_statements(database: Database, log: StatementLog) -> None:
    for pool in database._pools.values():
        engine = getattr(pool, "engine", None)
        if engine is None:
            continue

        def before(*arguments: Any) -> None:
            log.statements.append(str(arguments[2]))

        event.listen(engine.sync_engine, "before_cursor_execute", before)


@contextlib.contextmanager
def authz_environment(endpoint: PostgresEndpoint, prefix: str) -> Iterator[AuthzEnvironment]:
    """``session_environment`` con los constructores reales y el ``Authorizer``."""
    with session_environment(endpoint, prefix) as sessions:
        # Los usuarios sembrados ya aceptaron el aviso vigente: sin él no hay contexto de sesión
        # (NFR-NUC-29); las pruebas del aviso crean sus propios usuarios sin aceptarlo.
        sessions.run(
            sessions.admin.execute(
                "UPDATE identity.user_account SET privacy_notice_version_accepted = $1"
                " WHERE privacy_notice_version_accepted IS NULL",
                CURRENT_PRIVACY_NOTICE_VERSION,
            )
        )
        store = PostgresContextStore(sessions.database)
        contexts = ScopeContexts(
            store=store,
            clock=sessions.clock,
            provider_organization_id=sessions.seed.provider_organization_id,
            system_actor_id=SYSTEM_ACTOR_ID,
        )
        audit = PostgresAuthorizationAudit(
            database=sessions.database,
            audit=sessions.audit,
            outbox=sessions.outbox,
            clock=sessions.clock,
        )
        authorizer = Authorizer(
            audit=audit, provider_organization_id=sessions.seed.provider_organization_id
        )
        log = StatementLog()
        _log_statements(sessions.database, log)
        yield AuthzEnvironment(sessions, contexts, store, audit, authorizer, log)
