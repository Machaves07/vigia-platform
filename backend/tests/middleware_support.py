"""Entorno de prueba de la cadena de middleware (TASK-134): aplicación real con dobles.

- La sesión pasa por los constructores reales (``ScopeContexts.context_from_session``) sobre un
  almacén en memoria (``FakeContextStore``): el ``correlation_id`` del contexto es el que la cadena
  le entrega, igual que en producción.
- La autorización por ruta es la real (``ContextAuthorizer`` con la matriz de ``identity.authz``)
  salvo en las rutas de nodos, que U-03 autentica con su propio mecanismo (``NodeAwareAuthorizer``).
- Las rutas de la unidad de prueba cubren lo que la cadena distingue: pública con cuerpo
  (``POST /auth/login``), con permiso y cuerpo estricto (``POST /users``, ``PATCH /users/{id}``),
  lectura con permiso (``GET /me``), aceptación del aviso, y rutas de nodos.

Solo datos generados (NFR-CTR-43): ningún dato de personas reales.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from vigia_contracts.clock import SimulatedClock

from tests.api_support import World
from tests.signing_support import START
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.identity.authz.authorize import Resource
from vigia_platform.identity.authz.context import (
    ConcessionRow,
    ScopeContexts,
    SessionRow,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.app import UnitRegistration, platform_units
from vigia_platform.shared.api.declarations import (
    UnauthenticatedRoute,
    body_limit,
    requires,
    unauthenticated,
)
from vigia_platform.shared.api.middleware import (
    ContextAuthorizer,
    CsrfRejection,
    request_context,
    request_session,
)
from vigia_platform.shared.api.request_state import request_state
from vigia_platform.shared.context import AllowedScope, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import RouteClass, current_route_class
from vigia_platform.shared.ratelimit import Allowed, Budget, Limited, RateLimiter

__all__ = [
    "CLIENT_ORG",
    "NOTICE",
    "ORIGIN",
    "PERMISSIONS",
    "PROVIDER_ORG",
    "SAME_ORIGIN",
    "STORE_ORIGIN",
    "Harness",
    "RecordingAuthzAudit",
    "RecordingCsrfAudit",
    "UnlimitedRateLimiter",
    "chain_units",
    "cookie_header",
]

PROVIDER_ORG = uuid.UUID("0192f0c4-0000-7000-8000-000000000001")
CLIENT_ORG = uuid.UUID("0192f0c4-0000-7000-8000-000000000002")
SYSTEM_ACTOR = uuid.UUID("0192f0c4-0000-7000-8000-0000000000ff")
NOTICE = "v1-prueba"
ORIGIN = "https://app.vigia.test"
"""Origen configurado de la aplicación (``VIGIA_PUBLIC_ORIGIN``); el cliente de prueba no envía
``Origin`` salvo que la prueba lo ponga."""
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
STORE_ORIGIN = "https://vigia-evidence-000000000000-us-east-1.s3.us-east-1.amazonaws.com"
PERMISSIONS = frozenset(key.value for key in PermissionKey)


def chain_units() -> tuple[UnitRegistration, ...]:
    """Las unidades de la plataforma salvo las rutas reales de ``identity`` (TASK-135) y de
    ``ledger`` (TASK-137): la unidad de prueba de este arnés declara sus propias ``/auth/login``,
    ``/me`` y aceptación del aviso, con dobles, para probar la cadena sin base. Las rutas de
    ``ledger`` (y su cuerpo, PR-NUC-38) las prueba ``tests/examples/test_ledger_routes.py``."""
    return tuple(unit for unit in platform_units() if unit.name not in ("identity", "ledger"))


def cookie_header(cookie: SessionCookie) -> dict[str, str]:
    return {"Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}


def _cookie(organization_id: uuid.UUID, seed: str) -> SessionCookie:
    token = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()).decode().rstrip("=")
    return SessionCookie(organization_id, token)


@dataclass
class FakeContextStore:
    """``ContextStore`` en memoria: una fila por ``session_id_hash``."""

    rows: dict[str, SessionRow] = field(default_factory=dict)
    lookups: list[uuid.UUID] = field(default_factory=list)
    """``correlation_id`` del contexto de búsqueda de cada consulta."""

    async def session_row(
        self,
        lookup: ScopeContext,
        session_id_hash: str,
        now: datetime,
        concession_id: uuid.UUID | None,
    ) -> SessionRow | None:
        self.lookups.append(lookup.correlation_id)
        return self.rows.get(session_id_hash)

    async def operator_row(self, lookup: ScopeContext, user_id: uuid.UUID) -> None:
        return None


@dataclass
class RecordingCsrfAudit:
    """``CsrfAuditPort`` que recuerda cada rechazo (y puede fallar como una base caída)."""

    with_session: list[tuple[ScopeContext, CsrfRejection]] = field(default_factory=list)
    without_session: list[CsrfRejection] = field(default_factory=list)
    fail: bool = False

    async def csrf_rejected(self, context: ScopeContext, rejection: CsrfRejection) -> None:
        if self.fail:
            raise ConnectionError("base caída")
        self.with_session.append((context, rejection))

    async def csrf_rejected_without_session(self, rejection: CsrfRejection) -> None:
        if self.fail:
            raise ConnectionError("base caída")
        self.without_session.append(rejection)

    @property
    def total(self) -> int:
        return len(self.with_session) + len(self.without_session)


@dataclass
class RecordingAuthzAudit:
    """``AuthorizationAudit`` que recuerda cada denegación (y puede fallar)."""

    denied: list[tuple[ScopeContext, PermissionKey, Resource]] = field(default_factory=list)
    fail: bool = False

    async def authorization_denied(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> None:
        if self.fail:
            raise ConnectionError("base caída")
        self.denied.append((context, key, resource))


class UnlimitedRateLimiter(RateLimiter):
    """Limitador que siempre deja pasar: para las pruebas cuyo asunto no es la tasa."""

    def __init__(self) -> None:
        super().__init__(SimulatedClock(START))

    def check(self, key: str, budget: Budget) -> Allowed | Limited:
        return Allowed()


class NodeAwareAuthorizer:
    """El autorizador real en rutas de personas; en las de nodos (U-03, mTLS) deja pasar."""

    def __init__(self, inner: ContextAuthorizer) -> None:
        self._inner = inner

    async def authorize(self, request: Request, permission: str) -> None:
        if request_state(request.scope).route_class is RouteClass.NODE:
            return
        await self._inner.authorize(request, permission)


# Cuerpos como los de las rutas reales: sin campos extra y sin coerción de tipos primitivos;
# los UUID y las enumeraciones llegan como texto JSON (FastAPI valida el JSON ya decodificado).


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=254)
    role: Role
    plant_id: uuid.UUID


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: StrictStr = Field(min_length=1, max_length=120)
    active: StrictBool


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=254)
    password: StrictStr = Field(min_length=8, max_length=128)


class NodeHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: StrictInt = Field(ge=0, le=2**31 - 1)


@dataclass
class Observed:
    """Lo que vieron los manejadores (para comprobar contexto, pool y correlación)."""

    route_classes: list[RouteClass] = field(default_factory=list)
    correlation_ids: list[uuid.UUID] = field(default_factory=list)
    handled: list[str] = field(default_factory=list)


def _unit(observed: Observed) -> UnitRegistration:
    router = APIRouter()

    def seen(name: str, request: Request) -> None:
        observed.handled.append(name)
        observed.route_classes.append(current_route_class())
        session = request_session(request)
        if session is not None:
            observed.correlation_ids.append(session.context.correlation_id)

    @router.get("/me", dependencies=[requires(PermissionKey.HIERARCHY_READ.value)])
    async def me(request: Request) -> dict[str, str]:
        seen("me", request)
        context = request_context(request)
        return {"organization_id": str(context.organization_id)}

    @router.post("/users", dependencies=[requires(PermissionKey.USERS_MANAGE.value)])
    async def create_user(body: NewUser, request: Request) -> dict[str, str]:
        seen("create_user", request)
        return {"role": body.role.value}

    @router.patch("/users/{user_id}", dependencies=[requires(PermissionKey.USERS_MANAGE.value)])
    async def change_user(user_id: uuid.UUID, body: UserChange, request: Request) -> dict[str, str]:
        seen("change_user", request)
        return {"user_id": str(user_id)}

    @router.post(
        "/privacy-notice/accept", dependencies=[requires(PermissionKey.HIERARCHY_READ.value)]
    )
    async def accept_notice(request: Request) -> dict[str, str]:
        seen("accept_notice", request)
        return {"accepted": "true"}

    @router.post("/auth/login", dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_LOGIN)])
    async def login(body: Credentials, request: Request) -> dict[str, str]:
        seen("login", request)
        return {"ok": "true"}

    @router.get("/api/nodes/x", dependencies=[requires(PermissionKey.FLEET_READ.value)])
    async def node_read(request: Request) -> dict[str, str]:
        seen("node_read", request)
        return {"ok": "node"}

    @router.post(
        "/api/nodes/heartbeat",
        dependencies=[requires(PermissionKey.FLEET_READ.value), body_limit(4096)],
    )
    async def node_heartbeat(body: NodeHeartbeat, request: Request) -> dict[str, int]:
        seen("node_heartbeat", request)
        return {"sequence": body.sequence}

    return UnitRegistration("prueba", routers=(router,))


@dataclass
class Harness:
    """Una aplicación ``vigia-api`` completa con la cadena real y dobles en los puertos."""

    world: World = field(default_factory=World)
    store: FakeContextStore = field(default_factory=FakeContextStore)
    csrf_audit: RecordingCsrfAudit = field(default_factory=RecordingCsrfAudit)
    authz_audit: RecordingAuthzAudit = field(default_factory=RecordingAuthzAudit)
    observed: Observed = field(default_factory=Observed)
    privacy_notice_version: str | None = NOTICE
    public_origin: str | None = ORIGIN

    @property
    def contexts(self) -> ScopeContexts:
        return ScopeContexts(
            store=self.store,
            clock=self.world.clock,
            provider_organization_id=PROVIDER_ORG,
            system_actor_id=SYSTEM_ACTOR,
        )

    def session(
        self,
        seed: str,
        *,
        role: Role = Role.ADMINISTRATOR,
        organization_id: uuid.UUID = CLIENT_ORG,
        accepted: str | None = NOTICE,
        concession: ConcessionRow | None = None,
    ) -> SessionCookie:
        """Una sesión válida con una asignación ``role`` sobre su organización."""
        cookie = _cookie(organization_id, seed)
        self.store.rows[cookie.session_id_hash] = SessionRow(
            user_id=uuid.uuid5(uuid.NAMESPACE_URL, seed),
            organization_id=organization_id,
            organization_kind="provider" if organization_id == PROVIDER_ORG else "client",
            display_name="Persona sintética",
            privacy_notice_version_accepted=accepted,
            assignments=(AllowedScope(ScopeLevel.ORGANIZATION, organization_id, role),),
            concession=concession,
        )
        return cookie

    def app(self, **runtime: Any) -> Any:
        authorizer = NodeAwareAuthorizer(
            ContextAuthorizer(audit=self.authz_audit, provider_organization_id=PROVIDER_ORG)
        )
        values: dict[str, Any] = {
            "sessions": self.contexts,
            "csrf_audit": self.csrf_audit,
            "origin_secret": b"k" * 32,
            "privacy_notice_version": self.privacy_notice_version,
            "authorizer": authorizer,
        }
        values.update(runtime)
        return self.world.app(
            units=(*chain_units(), _unit(self.observed)),
            permissions=PERMISSIONS,
            runtime=values,
            public_origin=self.public_origin,
            csp_store_origins=(STORE_ORIGIN,),
        )
