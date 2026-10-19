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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from vigia_contracts.clock import SimulatedClock
from vigia_contracts.models.enumerations import RejectionCode

from tests.api_support import World
from tests.attachment_support import attachment_world
from tests.signing_support import START
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.context import (
    ConcessionRow,
    ScopeContexts,
    SessionRow,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.adapters.http.attachment_routes import ATTACHMENTS_STATE_KEY
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application import actions as actions_module
from vigia_platform.loop.application import classification as classification_module
from vigia_platform.loop.application import closure as closure_module
from vigia_platform.loop.application import reopening as reopening_module
from vigia_platform.loop.application import review_queue as review_queue_module
from vigia_platform.loop.application import root_cause as root_cause_module
from vigia_platform.loop.config.parameters import (
    INITIAL_DEFAULTS_REASON,
    LoopSettings,
    PlantLoopConfiguration,
    check_values,
)
from vigia_platform.loop.config.service import validated_change
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain import root_cause as root_cause_domain
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.shared.api.app import UnitRegistration, platform_units
from vigia_platform.shared.api.declarations import (
    NODE_GATE_STATE_KEY,
    NodeRoute,
    UnauthenticatedRoute,
    node_route,
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
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "CLIENT_ORG",
    "NODE_HEARTBEAT_PATH",
    "NODE_READ_PATH",
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
    ``ledger`` (y su cuerpo, PR-NUC-38) las prueba ``tests/examples/test_ledger_routes.py``; las
    de ``catalog`` (cuerpo, CSRF y servicios reales), ``test_catalog_admissions.py`` y
    ``test_catalog_documents_localstack.py`` de ``tests/integration/``; las de ``fleet`` (VIG-147),
    ``test_fleet_node_identity.py`` y el aislamiento de ``tests/isolation``. Tampoco las de
    ``node_api``: desde TASK-223 publica el latido y el catálogo por zona, y este arnés declara sus
    propias rutas del contrato con ``HarnessNodeGate`` (las reales las prueban
    ``test_fleet_heartbeat.py`` y ``test_fleet_zone_catalog_route.py``).

    Las de ``loop`` sí entran, con sus dobles en ``app.state`` (``loop_state``): los adjuntos de
    cierre (TASK-310, el puerto real sobre memoria), la configuración del lazo (TASK-304,
    ``LoopConfigurationDouble``, sin base), la clasificación (TASK-308, ``ClassificationDouble``)
    y la causa raíz (TASK-315, ``RootCauseDouble``). Así los catálogos de CSRF y de cuerpos mutados
    (PR-NUC-38) cubren todas las rutas de ``loop`` registradas."""
    return tuple(
        unit
        for unit in platform_units()
        if unit.name not in ("identity", "ledger", "catalog", "fleet", "node_api")
    )


class LoopConfigurationDouble:
    """Doble de ``LoopConfigurationService`` para el arnés, sin base.

    Hace las comprobaciones del servicio que no miran la base, con el mismo código
    (``validated_change`` y ``check_values``): motivo, rangos, calendario y reglas entre
    parámetros. Un cambio válido devuelve la versión 2 sobre los valores por defecto, sin escribir
    nada. La lectura devuelve los valores por defecto (versión 1).
    """

    def __init__(self, clock: Any) -> None:
        self._clock = clock
        self._free_text = FreeTextPolicyRegistry()
        self._free_text.seal()

    def _configuration(
        self, context: ScopeContext, plant_id: uuid.UUID, settings: LoopSettings, version: int
    ) -> PlantLoopConfiguration:
        role = context.actor.role_in_use or Role.ADMINISTRATOR
        return PlantLoopConfiguration(
            plant_id=plant_id,
            organization_id=context.organization_id,
            settings=settings,
            timezone="America/Bogota",
            version=version,
            changed_at=to_millisecond(self._clock.now()),
            changed_by={
                "user_id": str(context.actor.id),
                "display_name": context.actor.display_name_snapshot,
                "role_in_use": role.value,
            },
            change_reason=INITIAL_DEFAULTS_REASON,
        )

    async def configuration(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> PlantLoopConfiguration:
        return self._configuration(context, plant_id, LoopSettings.defaults(), 1)

    async def change(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        integers: Mapping[str, object],
        business_calendar: object | None,
        reason: object,
    ) -> PlantLoopConfiguration:
        request = validated_change(
            self._free_text, integers=integers, business_calendar=business_calendar, reason=reason
        )
        settings = check_values(
            LoopSettings.defaults().replaced(request.integers, request.business_calendar)
        )
        return self._configuration(context, plant_id, settings, 2)


class ClassificationDouble:
    """Doble de ``ClassificationService`` para el arnés, sin base (TASK-308).

    Hace las comprobaciones del servicio que no miran la base, con el mismo código (salida,
    categoría de su lista y motivo); un cuerpo válido no encuentra ningún hallazgo sintético
    (``ResourceNotFound``), como el puerto de adjuntos con la acción."""

    async def classify(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        outcome: object,
        reason_category: object,
        reason_text: object,
        reinforced_reason: bool,
    ) -> Any:
        chosen = classification_module._outcome(outcome)
        classification_module._reason(chosen, reason_category, reason_text)
        raise ResourceNotFound()


class ActionsDouble:
    """Doble de ``ActionService`` y ``EvidenceService`` para el arnés, sin base (TASK-311).

    Hace las comprobaciones que no miran la base, con el mismo código (tipo de la lista, textos
    sin acción sobre una persona); un cuerpo válido no encuentra ningún hallazgo ni acción
    sintéticos (``ResourceNotFound``)."""

    async def assign(
        self, context: ScopeContext, finding_state_id: uuid.UUID, **request: Any
    ) -> Any:
        actions_module.parse_action_type(request["action_type"])
        actions_module.action_text(
            request["action_description"], actions_module.DESCRIPTION_FIELD, required=True
        )
        raise ResourceNotFound()

    async def add(self, context: ScopeContext, action_id: uuid.UUID, **request: Any) -> Any:
        raise ResourceNotFound()


class ClosureDouble:
    """Doble de ``ClosureService`` para el arnés, sin base (VIG-233).

    Hace las comprobaciones de forma que no miran la base (los textos y la licencia); un cuerpo
    válido no encuentra ningún hallazgo sintético (``ResourceNotFound``)."""

    async def close(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        closure_description: object,
        verification_note: object = None,
        professional_license: object = None,
    ) -> Any:
        module = closure_module
        module._text(closure_description, module._DESCRIPTION_FIELD, required=True)
        module._text(verification_note, module._NOTE_FIELD, required=False)
        module._license(professional_license)
        raise ResourceNotFound()


class ReopeningDouble:
    """Doble de ``ReopeningService`` para el arnés, sin base (VIG-233).

    Hace las comprobaciones de forma que no miran la base (el motivo y la acción nueva); un
    cuerpo válido no encuentra ningún hallazgo sintético (``ResourceNotFound``)."""

    async def reopen(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        reason_text: object,
        action_type: object,
        action_description: object,
        expected_outcome: object = None,
        assignee_user_id: object,
        deadline_days: object = None,
    ) -> Any:
        reopening_module._reason(reason_text)
        actions_module.parse_action_type(action_type)
        actions_module.action_text(
            action_description, actions_module.DESCRIPTION_FIELD, required=True
        )
        raise ResourceNotFound()


class RootCauseDouble:
    """Doble de ``RootCauseService`` para el arnés, sin base (TASK-315).

    Hace las comprobaciones del servicio que no miran la base, con el mismo código: el enunciado,
    las referencias y la categoría de un paso; las categorías, los textos finales, lo que falta
    (sobre un análisis sintético con un porqué) y la licencia de una firma. Un cuerpo válido no
    encuentra ningún análisis ni hallazgo sintético (``ResourceNotFound``)."""

    def __init__(self) -> None:
        self._free_text = FreeTextPolicyRegistry()
        self._free_text.seal()

    async def start(self, context: ScopeContext, finding_state_id: uuid.UUID) -> Any:
        raise ResourceNotFound()

    async def put_step(
        self,
        context: ScopeContext,
        analysis_id: uuid.UUID,
        step_index: int,
        *,
        statement: object,
        evidence_refs: object,
        category: object,
    ) -> Any:
        module = root_cause_module
        module.checked_evidence_refs(evidence_refs)
        module.checked_text(self._free_text, statement, module.STATEMENT_FIELD, None)
        if category is not None:
            module.checked_category(category)
        raise ResourceNotFound()

    async def sign(
        self,
        context: ScopeContext,
        analysis_id: uuid.UUID,
        *,
        professional_license: object,
        root_cause_category: object,
        root_cause_statement: object,
        conclusion: object,
        cause_effect_branches: Any,
    ) -> Any:
        module = root_cause_module
        category = (
            None if root_cause_category is None else module.checked_category(root_cause_category)
        )
        branches = module.checked_branches(self._free_text, cause_effect_branches, None)
        organization = context.organization_id
        statement = module._optional_text(
            self._free_text, root_cause_statement, module.ROOT_STATEMENT_FIELD, organization
        )
        final = module._optional_text(
            self._free_text, conclusion, module.CONCLUSION_FIELD, organization
        )
        try:
            result = root_cause_domain.sign(
                root_cause_domain.GuidedAnalysis(
                    steps=(root_cause_domain.WhyStep(1, "Porqué sintético del arnés"),)
                ),
                branches=branches,
                root_cause_category=category,
                root_cause_statement=statement,
                conclusion=final,
                signer_name=context.actor.display_name_snapshot,
                professional_license=module._license(professional_license),
            )
        except root_cause_domain.RootCauseRejected as rejected:
            raise module._rejected(rejected) from None
        if isinstance(result, root_cause_domain.Pending):
            raise module.AnalysisRejected(LoopDetailCode.ANALYSIS_INFORMATION_INSUFFICIENT)
        raise ResourceNotFound()


class ReviewQueueDouble:
    """Doble de ``ReviewQueueService`` para el arnés, sin base (TASK-309).

    Hace las comprobaciones del servicio que no miran la base, con el mismo código (salida y
    motivo de su lista); un cuerpo válido no encuentra ninguna detección sintética
    (``ResourceNotFound``), como los demás dobles de ``loop``."""

    async def resolve(
        self,
        context: ScopeContext,
        detection_id: uuid.UUID,
        *,
        outcome: object,
        reason_category: object,
        reason_text: object,
    ) -> Any:
        chosen = review_queue_module._outcome(outcome)
        review_queue_module._reason(chosen, reason_category, reason_text)
        raise ResourceNotFound()


def loop_state(clock: Any) -> dict[str, Any]:
    """Las entradas de ``app.state`` de ``loop`` en el arnés (adjuntos, configuración,
    clasificación, acciones y evidencia, causa raíz, cierre y reapertura, y cola de revisión)."""
    return {
        ATTACHMENTS_STATE_KEY: attachment_world(clock).service,
        LOOP_STATE_KEY: LoopHttp(
            configuration=cast(Any, LoopConfigurationDouble(clock)),
            classification=cast(Any, ClassificationDouble()),
            actions=cast(Any, ActionsDouble()),
            evidence=cast(Any, ActionsDouble()),
            root_cause=cast(Any, RootCauseDouble()),
            closure=cast(Any, ClosureDouble()),
            reopening=cast(Any, ReopeningDouble()),
            review_queue=cast(Any, ReviewQueueDouble()),
        ),
    }


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


@dataclass
class RecordingProviderQueries:
    """``ProviderQueryLedger`` que recuerda cada ``provider_query`` (y puede fallar)."""

    written: list[tuple[ScopeContext, dict[str, str], uuid.UUID | None]] = field(
        default_factory=list
    )
    fail: bool = False

    async def write_provider_query(
        self, context: ScopeContext, content: Mapping[str, str], plant_id: uuid.UUID | None
    ) -> None:
        if self.fail:
            raise ConnectionError("base caída")
        self.written.append((context, dict(content), plant_id))


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


NODE_READ_TEMPLATE = NodeRoute.ZONE_CATALOG.path
NODE_READ_PATH = "/api/nodes/zones/0192f0c4-0000-7000-8000-0000000000aa/catalog"
"""Una ruta de lectura de nodo (sin cuerpo) del arnés."""
NODE_HEARTBEAT_PATH = NodeRoute.HEARTBEAT.path


class HarnessNodeGate:
    """``NodeGate`` del arnés: sin identidad ni versión (las prueba ``node_api``); solo emite el
    ``payload_too_large`` que la cadena marcó (paso 3), como la verificación real."""

    async def admit(self, request: Request, route: NodeRoute) -> None:
        if request_state(request.scope).body_exceeded:
            raise NodeRejection(RejectionCode.PAYLOAD_TOO_LARGE)


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

    # Rutas del contrato (A-51): se declaran con node_route y su plantilla de NodeRoute; la
    # verificación previa la hace la NodeGate del arnés (HarnessNodeGate).
    @router.get(NODE_READ_TEMPLATE, dependencies=[node_route(NodeRoute.ZONE_CATALOG)])
    async def node_read(zone_id: str, request: Request) -> dict[str, str]:
        seen("node_read", request)
        return {"ok": "node"}

    @router.post(NodeRoute.HEARTBEAT.path, dependencies=[node_route(NodeRoute.HEARTBEAT)])
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
    provider_queries: RecordingProviderQueries = field(default_factory=RecordingProviderQueries)
    observed: Observed = field(default_factory=Observed)
    privacy_notice_version: str | None = NOTICE
    public_origin: str | None = ORIGIN
    extra_units: tuple[UnitRegistration, ...] = ()
    """Unidades de prueba propias de un archivo, además de la de este arnés."""

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
            ContextAuthorizer(
                audit=self.authz_audit,
                provider_organization_id=PROVIDER_ORG,
                provider_queries=self.provider_queries,
                clock=self.world.clock,
            )
        )
        values: dict[str, Any] = {
            "sessions": self.contexts,
            "csrf_audit": self.csrf_audit,
            "origin_secret": b"k" * 32,
            "privacy_notice_version": self.privacy_notice_version,
            "authorizer": authorizer,
            "state": {NODE_GATE_STATE_KEY: HarnessNodeGate(), **loop_state(self.world.clock)},
        }
        values.update(runtime)
        return self.world.app(
            units=(*chain_units(), _unit(self.observed), *self.extra_units),
            permissions=PERMISSIONS,
            runtime=values,
            public_origin=self.public_origin,
            csp_store_origins=(STORE_ORIGIN,),
        )
