"""PR-NUC-01: dos organizaciones sobre el catálogo completo de rutas (TASK-139, bloqueante).

TASK-139 (BR-NUC-09, 37, 38; NFR-NUC-22 y 47; PAT-NUC-SEG-01). La aplicación real (``create_app``
con la cadena fija de middleware y **todas** las unidades de ``platform_units()``: ``identity``,
``ledger`` y ``platform``) contra PostgreSQL 16 real como ``vigia_app``, con los servicios reales
de cada ruta y ``ContextAuthorizer`` con su ``provider_query``.

**Catálogo de casos** (``CASES``): cada método y plantilla registrados en la aplicación tiene su
caso, con el tipo de aislamiento que le corresponde:

- ``RESOURCE``: la ruta nombra un recurso (en la ruta, la consulta o el cuerpo). Con el contexto
  de A y el identificador **conocido** de un recurso de B responde exactamente lo mismo que con un
  identificador inexistente (estado y cuerpo, salvo ``correlation_id``): ``not_found``, nunca
  ``forbidden`` (BR-NUC-09);
- ``OWN``: la ruta no nombra recurso; actúa sobre la organización del contexto. Con el contexto de
  A no muestra ningún identificador de B;
- ``SESSION``: solo la persona de la sesión (``authenticated``);
- ``PROVIDER_ONLY``: solo la proveedora (``platform.*``, ``/provider/concessions``): desde A,
  igual que inexistente;
- ``PUBLIC``: sin sesión no hay organización de contexto; el caso dice por qué;
- ``NODE``: ruta del contrato (``node_route``, A-51; TASK-206): con el certificado de un nodo de A
  y un recurso de B responde ``node_zone_mismatch`` o ``not_found``. ``uncovered`` también exige
  su caso.

En todos los casos con sesión, ninguna petición de A cambia una sola fila de B (huella de cada
tabla con ``organization_id`` calculada como superusuario) ni devuelve un identificador de B.

**Cobertura del catálogo** (criterio 2): ``uncovered`` compara las rutas declaradas con
``CASES`` y falla **nombrando** cada ruta sin caso; ``test_a_route_without_case_is_named`` lo
comprueba con una ruta sonda. Corre sin base (``build_openapi_app``), así que la canalización la
ejecuta también en el trabajo sin integración.

**Bajo concesión** (PR-NUC-01, segunda mitad; BR-NUC-37, 38): un instalador del proveedor con una
concesión vigente sobre B hace la misma petición con los identificadores de B: responde el
recurso si y solo si la clave de la ruta está en la columna ``provider_installer``; si no, igual
que inexistente. Cada petición que la ruta autoriza deja **exactamente un** ``provider_query``
(visible en ``GET /concessions/{id}/queries`` del cliente) y, en las rutas que leen o escriben
datos del cliente, su entrada de auditoría normal (BR-NUC-38). Con una concesión de **una
planta**, los recursos de la otra planta responden igual que inexistentes.

``/provider/concessions`` (``concessions.grant``, que sí está en la columna) responde
``not_found`` bajo concesión por diseño: una concesión no se concede ni se lista desde el
contexto de otra (A-46). Es la única excepción y está en ``PROVIDER_SIDE_ONLY``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute

from tests.api_support import World
from tests.authz_support import Site
from tests.examples.test_admin_routes import UnusedPasswords
from tests.examples.test_ledger_routes import CLIP, StubEvidenceStorage
from tests.factories import uuid7
from tests.hierarchy_support import (
    LINK_BASE,
    FakeActivationPasswords,
    FakeActivationSecondFactor,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_coverage_port import COVERAGE_TYPES
from tests.ledger_database import evidence_values, insert_evidence, set_organization
from tests.live_view_support import LIVE_VIEW_URL, LiveViewEnvironment, live_view_environment
from tests.second_factor_support import FakeKms
from tests.session_support import ORIGIN_KEY
from tests.signing_support import ENVIRONMENT
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.admission_repository import (
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.adapters.s3.documents import DocumentObjectStore
from vigia_platform.catalog.application.admission import ADMISSION_RECORD_TYPE, AdmissionService
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.concessions import ConcessionService
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationService,
)
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.organization import OrganizationSettingsService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import RoleService
from vigia_platform.identity.application.users import SecondFactorResetService, UserService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.passwords import VerifyResult
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie, SessionService
from vigia_platform.identity.authz.matrix import permissions_of
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.http import LedgerHttp
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityResults
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.integrity_requests import IntegrityRequests
from vigia_platform.ledger.application.labels import LabelService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.node_api.router import node_router
from vigia_platform.shared.adapters.http import PlatformHttp
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import (
    NodeRoute,
    check_routes,
    iter_declared_routes,
    requires,
)
from vigia_platform.shared.api.errors import DetailCodeRegistry
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import ObjectHead, PresignedRequest

STATIC: Final = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
REASON: Final = "Mantenimiento sintético del nodo de la línea 2"
T0: Final = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
HOUR: Final = timedelta(hours=1)
NBSP: Final = chr(0x00A0)
ZERO_WIDTH_SPACE: Final = chr(0x200B)
INSTALLER_KEYS: Final = frozenset(key.value for key in permissions_of(Role.PROVIDER_INSTALLER))
"""La columna ``provider_installer`` de la matriz (BR-NUC-37)."""
ADMISSION_TYPES: Final = tuple(
    d for d in CATALOG_RECORD_TYPES if d.record_type == ADMISSION_RECORD_TYPE
)
"""El tipo que escriben las rutas de admisión (los que registra la unidad ``catalog``)."""
DOCUMENT: Final = b"%PDF-1.7 acta de alcance sintetica"


class StubDocumentStorage:
    """``vigia-evidence`` para ``POST /documents``: la clave nueva no tiene objeto y la URL es
    sintética (el almacén real, en ``test_catalog_documents_localstack``)."""

    async def head_object(self, key: str) -> ObjectHead | None:
        return None

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        return PresignedRequest(
            "PUT",
            f"https://almacen.vigia.test/{key}?X-Amz-Expires=900",
            {"content-type": content_type, "x-amz-checksum-sha256": checksum_sha256},
            T0 + ttl,
        )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


# --- Catálogo de casos ------------------------------------------------------------------------


class Kind(enum.Enum):
    RESOURCE = "resource"
    OWN = "own"
    SESSION = "session"
    PROVIDER_ONLY = "provider_only"
    PUBLIC = "public"
    NODE = "node"
    """Ruta del contrato (``node_route``, A-51): con el certificado de un nodo de A y un recurso
    de B responde ``node_zone_mismatch`` o ``not_found`` (TASK-206; los casos de cada ruta
    publicada los añade su tarea y TASK-228)."""


@dataclass(frozen=True)
class Ids:
    """Identificadores de una organización: los de B (conocidos) o inexistentes."""

    organization: uuid.UUID
    plant: uuid.UUID
    zone: uuid.UUID
    user: uuid.UUID
    assignment: uuid.UUID
    concession: uuid.UUID
    record: uuid.UUID
    evidence: uuid.UUID
    event: uuid.UUID
    label: uuid.UUID

    @classmethod
    def missing(cls) -> Ids:
        return cls(*(uuid7() for _ in range(10)))

    def values(self) -> tuple[uuid.UUID, ...]:
        return tuple(getattr(self, name) for name in self.__dataclass_fields__)


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    params: dict[str, str] | None = None
    json: Any = None


Builder = Callable[[Ids], Call]


@dataclass(frozen=True)
class Case:
    kind: Kind
    call: Builder | None = None
    why: str = ""
    own_trail: bool = False
    """La respuesta lista la auditoría de la organización del contexto: incluye las entradas de
    las peticiones de la propia persona que nombró ese identificador (su rastro, no datos de B),
    que no cuentan al comparar."""


def _assignment(ids: Ids) -> list[dict[str, str]]:
    return [{"role": "coordinator_sst", "scope_level": "plant", "scope_id": str(ids.plant)}]


def _coverage_period() -> dict[str, str]:
    return {"from": _stamp(T0), "to": _stamp(T0 + HOUR)}


_PUBLIC_WHY = "sin sesión no hay organización de contexto"

CASES: Final[dict[tuple[str, str], Case]] = {
    # --- Públicas (``unauthenticated``) ---
    ("GET", "/health/live"): Case(Kind.PUBLIC, why=_PUBLIC_WHY),
    ("GET", "/health/ready"): Case(Kind.PUBLIC, why=_PUBLIC_WHY),
    ("GET", "/.well-known/vigia-checkpoint-keys"): Case(Kind.PUBLIC, why="claves públicas"),
    ("GET", "/.well-known/vigia-verifier"): Case(Kind.PUBLIC, why="hash del verificador"),
    ("POST", "/auth/login"): Case(Kind.PUBLIC, why="credenciales, no identificadores"),
    ("POST", "/auth/second-factor"): Case(Kind.PUBLIC, why="sesión pendiente propia"),
    ("POST", "/auth/second-factor/enroll"): Case(Kind.PUBLIC, why="sesión pendiente propia"),
    ("POST", "/auth/logout"): Case(Kind.PUBLIC, why="cierra la sesión de la cookie"),
    ("POST", "/invitations/{token}/accept"): Case(
        Kind.PUBLIC, why="el token es un secreto de un solo uso, no un identificador"
    ),
    ("GET", "/assets/{asset_path:path}"): Case(Kind.PUBLIC, why="estáticos"),
    ("HEAD", "/assets/{asset_path:path}"): Case(Kind.PUBLIC, why="estáticos"),
    ("GET", "/version.json"): Case(Kind.PUBLIC, why="estáticos"),
    ("HEAD", "/version.json"): Case(Kind.PUBLIC, why="estáticos"),
    ("GET", "/robots.txt"): Case(Kind.PUBLIC, why="estáticos"),
    ("HEAD", "/robots.txt"): Case(Kind.PUBLIC, why="estáticos"),
    ("GET", "/{screen_path:path}"): Case(Kind.PUBLIC, why="pantallas de la aplicación"),
    ("HEAD", "/{screen_path:path}"): Case(Kind.PUBLIC, why="pantallas de la aplicación"),
    # --- Sesión (``authenticated``): solo la persona de la sesión ---
    ("GET", "/me"): Case(Kind.SESSION, lambda i: Call("GET", "/me")),
    ("GET", "/auth/sessions"): Case(Kind.SESSION, lambda i: Call("GET", "/auth/sessions")),
    ("POST", "/auth/sessions/close-others"): Case(
        Kind.SESSION, lambda i: Call("POST", "/auth/sessions/close-others")
    ),
    ("POST", "/auth/password"): Case(
        Kind.SESSION,
        lambda i: Call(
            "POST",
            "/auth/password",
            json={"current_password": "actual-sintetica", "new_password": "nueva-sintetica-1"},
        ),
    ),
    ("POST", "/privacy-notice/accept"): Case(
        Kind.SESSION,
        lambda i: Call("POST", "/privacy-notice/accept", json={"notice_version": "2026-09"}),
    ),
    # --- identity ---
    ("GET", "/users"): Case(Kind.OWN, lambda i: Call("GET", "/users")),
    ("POST", "/users"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "POST",
            "/users",
            json={
                "email": new_email("aislamiento"),
                "display_name": "Persona sintética",
                "assignments": _assignment(i),
            },
        ),
    ),
    ("POST", "/users/{user_id}/deactivate"): Case(
        Kind.RESOURCE, lambda i: Call("POST", f"/users/{i.user}/deactivate")
    ),
    ("POST", "/users/{user_id}/reactivate"): Case(
        Kind.RESOURCE,
        lambda i: Call("POST", f"/users/{i.user}/reactivate", json={"assignments": _assignment(i)}),
    ),
    ("PATCH", "/users/{user_id}"): Case(
        Kind.RESOURCE,
        lambda i: Call("PATCH", f"/users/{i.user}", json={"display_name": "Nombre sintético"}),
    ),
    ("POST", "/users/{user_id}/second-factor/reset"): Case(
        Kind.RESOURCE, lambda i: Call("POST", f"/users/{i.user}/second-factor/reset")
    ),
    ("POST", "/users/{user_id}/roles"): Case(
        Kind.RESOURCE,
        lambda i: Call("POST", f"/users/{i.user}/roles", json=_assignment(i)[0]),
    ),
    ("DELETE", "/users/{user_id}/roles/{assignment_id}"): Case(
        Kind.RESOURCE, lambda i: Call("DELETE", f"/users/{i.user}/roles/{i.assignment}")
    ),
    ("GET", "/hierarchy"): Case(Kind.OWN, lambda i: Call("GET", "/hierarchy")),
    ("POST", "/plants"): Case(
        Kind.OWN,
        lambda i: Call(
            "POST",
            "/plants",
            json={
                "code": new_code("PL"),
                "name": "Planta sintética",
                "country": "CO",
                "data_region": "us-east-1",
                "timezone": "America/Bogota",
            },
        ),
    ),
    ("POST", "/plants/{plant_id}/zones"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "POST",
            f"/plants/{i.plant}/zones",
            json={"code": new_code("ZN"), "name": "Zona sintética"},
        ),
    ),
    ("GET", "/organization/settings"): Case(
        Kind.OWN, lambda i: Call("GET", "/organization/settings")
    ),
    ("PATCH", "/organization/settings"): Case(
        Kind.OWN,
        lambda i: Call("PATCH", "/organization/settings", json={"concession_default_days": 7}),
    ),
    ("GET", "/concessions"): Case(
        Kind.RESOURCE, lambda i: Call("GET", "/concessions", params={"plant_id": str(i.plant)})
    ),
    ("GET", "/concessions/{concession_id}/queries"): Case(
        Kind.RESOURCE, lambda i: Call("GET", f"/concessions/{i.concession}/queries")
    ),
    ("POST", "/concessions/{concession_id}/revoke"): Case(
        Kind.RESOURCE, lambda i: Call("POST", f"/concessions/{i.concession}/revoke")
    ),
    ("POST", "/provider/concessions"): Case(
        Kind.PROVIDER_ONLY,
        lambda i: Call(
            "POST",
            "/provider/concessions",
            json={
                "client_organization_id": str(i.organization),
                "scope_level": "plant",
                "scope_id": str(i.plant),
                "reason": REASON,
            },
        ),
    ),
    ("GET", "/provider/concessions"): Case(
        Kind.PROVIDER_ONLY, lambda i: Call("GET", "/provider/concessions")
    ),
    # --- ledger ---
    ("GET", "/ledger/records"): Case(
        Kind.RESOURCE, lambda i: Call("GET", "/ledger/records", params={"plant_id": str(i.plant)})
    ),
    ("GET", "/ledger/records/{record_id}"): Case(
        Kind.RESOURCE, lambda i: Call("GET", f"/ledger/records/{i.record}")
    ),
    ("GET", "/audit/entries"): Case(
        Kind.RESOURCE,
        lambda i: Call("GET", "/audit/entries", params={"plant_id": str(i.plant)}),
        own_trail=True,
    ),
    ("POST", "/evidence/{evidence_id}/read-url"): Case(
        Kind.RESOURCE, lambda i: Call("POST", f"/evidence/{i.evidence}/read-url")
    ),
    ("GET", "/labels"): Case(
        Kind.RESOURCE,
        lambda i: Call("GET", "/labels", params={**_coverage_period(), "zone_id": str(i.zone)}),
    ),
    ("GET", "/zones/{zone_id}/coverage"): Case(
        Kind.RESOURCE,
        lambda i: Call("GET", f"/zones/{i.zone}/coverage", params=_coverage_period()),
    ),
    ("GET", "/zones/{zone_id}/coverage/at"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "GET", f"/zones/{i.zone}/coverage/at", params={"instant": _stamp(T0 + HOUR / 2)}
        ),
    ),
    ("POST", "/integrity/verify"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "POST", "/integrity/verify", json={"kind": "ledger", "plant_id": str(i.plant)}
        ),
    ),
    ("GET", "/integrity/results"): Case(Kind.OWN, lambda i: Call("GET", "/integrity/results")),
    ("GET", "/integrity/checkpoints"): Case(
        Kind.OWN, lambda i: Call("GET", "/integrity/checkpoints")
    ),
    ("POST", "/zones/{zone_id}/live-view-token"): Case(
        Kind.RESOURCE, lambda i: Call("POST", f"/zones/{i.zone}/live-view-token")
    ),
    # --- catalog (U-03; el aislamiento exhaustivo de U-03 es de VIG-165) ---
    ("POST", "/plants/{plant_id}/admissions"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "POST",
            f"/plants/{i.plant}/admissions",
            json={
                "family": "coexistence",
                "answers": {"standard": True, "remedy": True, "subject": True},
            },
        ),
    ),
    ("GET", "/plants/{plant_id}/admissions"): Case(
        Kind.RESOURCE, lambda i: Call("GET", f"/plants/{i.plant}/admissions")
    ),
    ("POST", "/documents"): Case(
        Kind.RESOURCE,
        lambda i: Call(
            "POST",
            "/documents",
            json={
                "plant_id": str(i.plant),
                "kind": "scope_record",
                "content_type": "application/pdf",
                "size_bytes": len(DOCUMENT),
                "sha256": hashlib.sha256(DOCUMENT).hexdigest(),
            },
        ),
    ),
    # --- platform ---
    ("POST", "/platform/dead-letter/{event_id}/{consumer}/replay"): Case(
        Kind.PROVIDER_ONLY,
        lambda i: Call("POST", f"/platform/dead-letter/{i.event}/notifications/replay"),
    ),
    ("POST", "/platform/keys/{purpose}/rotate"): Case(
        Kind.PROVIDER_ONLY, lambda i: Call("POST", "/platform/keys/checkpoint/rotate")
    ),
}
"""Un caso por método y plantilla registrados (``uncovered`` lo exige)."""

PROVIDER_SIDE_ONLY: Final = frozenset(
    {("POST", "/provider/concessions"), ("GET", "/provider/concessions")}
)
"""Clave de la columna, pero solo desde la proveedora sin concesión (A-46): bajo concesión,
``not_found`` tras su ``provider_query``."""


def declared(routes: Sequence[BaseRoute]) -> dict[tuple[str, str], Any]:
    """Cada método y plantilla de ``routes`` con su declaración.

    Solo rutas de FastAPI: la documentación que añade ``build_openapi_app`` (``/docs``,
    ``/openapi.json``) no es una ruta de la aplicación, y ``check_routes`` no deja arrancar una
    aplicación con una ruta de otra clase.
    """
    found: dict[tuple[str, str], Any] = {}
    for route in iter_declared_routes(routes):
        if not route.is_api_route and not route.navigation_only:
            continue
        for method in route.methods:
            found[(method, route.path)] = route.declarations[0] if route.declarations else None
    return found


def uncovered(routes: Sequence[BaseRoute]) -> list[str]:
    """``MÉTODO /plantilla`` de cada ruta registrada sin caso de aislamiento en ``CASES``."""
    return sorted(f"{method} {path}" for method, path in set(declared(routes)) - set(CASES))


def _permission(app_routes: Sequence[BaseRoute], key: tuple[str, str]) -> str | None:
    declaration = declared(app_routes)[key]
    permission: str | None = declaration.permission
    return permission


# --- Cobertura del catálogo (sin base) -----------------------------------------------------------


def test_every_registered_route_has_its_isolation_case() -> None:
    missing = uncovered(build_openapi_app().routes)
    assert not missing, f"rutas sin caso de aislamiento en tests/isolation: {missing}"


def test_a_route_without_case_is_named() -> None:
    # Una ruta nueva (p. ej. de U-03) sin su caso: la verificación la nombra.
    async def probe(probe_id: uuid.UUID) -> dict[str, str]:  # pragma: no cover - no se llama
        return {}

    sonda = APIRoute(
        "/sonda/{probe_id}",
        probe,
        methods=["GET"],
        dependencies=[requires("hierarchy.read")],
    )
    routes = [*build_openapi_app().routes, sonda]
    assert uncovered(routes) == ["GET /sonda/{probe_id}"]
    with pytest.raises(AssertionError, match=r"GET /sonda/\{probe_id\}"):
        missing = uncovered(routes)
        assert not missing, f"rutas sin caso de aislamiento en tests/isolation: {missing}"


def test_contract_routes_are_in_the_catalog_and_need_their_case() -> None:
    # TASK-206: iter_declared_routes incluye las rutas declaradas con NodeRoute, así que
    # ``uncovered`` exige un caso por ruta del contrato (otra organización → node_zone_mismatch
    # o not_found) igual que por ruta de personas.
    contract = node_router((NodeRoute.FINDING, NodeRoute.ZONE_CATALOG)).routes
    routes = [*build_openapi_app().routes, *contract]
    assert uncovered(routes) == [
        "GET /api/nodes/zones/{zone_id}/catalog",
        "POST /api/nodes/findings",
    ]
    found = declared(contract)
    assert all(declaration.node is not None for declaration in found.values())


def test_a_route_under_api_nodes_without_declaration_is_named_and_does_not_start() -> None:
    async def probe() -> dict[str, str]:  # pragma: no cover - no se llama
        return {}

    sonda = APIRoute("/api/nodes/sonda", probe, methods=["GET"])
    routes = [*build_openapi_app().routes, sonda]
    assert uncovered(routes) == ["GET /api/nodes/sonda"]
    registry = DetailCodeRegistry()
    registry.seal()
    problems = check_routes(routes, frozenset(), registry, docs_enabled=True)
    assert any("no declara node_route" in problem for problem in problems), problems


def test_cases_name_only_registered_routes_and_match_their_declaration() -> None:
    # Un caso de una ruta que ya no existe tampoco pasa: el catálogo no se queda viejo.
    routes = build_openapi_app().routes
    registered = declared(routes)
    stale = sorted(
        f"{method} {path}"
        for method, path in set(CASES) - set(registered)
        if path != "/{screen_path:path}"  # solo con un directorio de estáticos (fixture)
    )
    assert not stale, stale
    for key, case in CASES.items():
        if key not in registered:
            continue
        declaration = registered[key]
        if case.kind is Kind.PUBLIC:
            assert declaration.unauthenticated is not None, key
        elif case.kind is Kind.SESSION:
            assert declaration.session is not None, key
        elif case.kind is Kind.NODE:
            assert declaration.node is not None and case.call is not None, key
        else:
            assert declaration.permission is not None, key
            assert case.call is not None, key
        assert case.call is not None or case.why, key


# --- Entorno ------------------------------------------------------------------------------------


class RejectingPasswords(UnusedPasswords):
    """La contraseña actual nunca coincide; cuenta las verificaciones."""

    def __init__(self) -> None:
        self.verified = 0

    async def verify(self, password: str, encoded: str) -> Any:
        self.verified += 1
        return VerifyResult(ok=False, needs_rehash=False)


@dataclass
class Organization:
    site: Site
    ids: Ids
    other_plant: Ids
    """Los mismos tipos de recurso en la segunda planta (para la concesión de una planta)."""


@dataclass
class Isolation:
    env: LiveViewEnvironment
    client: httpx.AsyncClient
    app: Any
    writer: EscritorExpediente
    passwords: RejectingPasswords
    a: Organization = field(init=False)
    b: Organization = field(init=False)

    @property
    def authz(self) -> Any:
        return self.env.authz

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.env.fetch(sql, *args)

    def send(
        self, call: Call, cookie: SessionCookie, concession: uuid.UUID | None = None
    ) -> httpx.Response:
        headers = dict(SAME_ORIGIN)
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(
                call.method, call.path, params=call.params, json=call.json, headers=headers
            )
        )
        return response

    # --- Altas ------------------------------------------------------------------------------

    def gate(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> uuid.UUID:
        context = unit_context(site.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
        receipt = self.run(
            self.writer.write(
                context,
                "gate_state_changed",
                {
                    "zone_id": str(zone_id),
                    "plant_id": str(plant_id),
                    "gate": "use",
                    "status": "approved",
                    "resulting_mode": "productive",
                },
                occurred_at=T0,
            )
        )
        assert isinstance(receipt, Receipt), receipt
        return receipt.record_id

    def evidence(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> uuid.UUID:
        record_id = self.gate(site, plant_id, zone_id)
        values = evidence_values(site.organization_id, plant_id, record_id, T0)
        values.update(
            zone_id=zone_id,
            storage_key=f"org/{site.organization_id}/plant/{plant_id}/zone/{zone_id}/clip.mp4",
            sha256=hashlib.sha256(CLIP).hexdigest(),
            size_bytes=len(CLIP),
        )
        admin = self.authz.sessions.admin

        async def insert() -> None:
            async with admin.transaction():
                await set_organization(admin, site.organization_id)
                await insert_evidence(admin, values)

        self.run(insert())
        evidence_id: uuid.UUID = values["evidence_id"]
        return evidence_id

    def label(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> uuid.UUID:
        label_id = uuid7()
        self.env.execute(
            "INSERT INTO ledger.label (label_id, organization_id, plant_id, zone_id,"
            " source_record_id, subject_record_id, family, outcome, reason_category, labeled_at,"
            " labeled_by) VALUES ($1, $2, $3, $4, $5, $6, 'ppe_helmet', 'confirmed',"
            " 'observed', $7, $8)",
            label_id,
            site.organization_id,
            plant_id,
            zone_id,
            uuid7(),
            uuid7(),
            T0 + HOUR / 4,
            json.dumps({"kind": "user", "role": "coordinator_sst"}),
        )
        return label_id

    def dead_letter(self, organization_id: uuid.UUID, plant_id: uuid.UUID) -> uuid.UUID:
        event_id = uuid7()
        self.env.execute(
            "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
            " payload, correlation_id, created_at) VALUES ($1, $2, $3, 'security_alert', $4, $5,"
            " $6)",
            event_id,
            organization_id,
            plant_id,
            json.dumps({"alert_kind": "context_absent_attempt"}),
            uuid7(),
            self.authz.now(),
        )
        return event_id

    def resources(self, site: Site, plant_index: int) -> Ids:
        """Un recurso de cada tipo en la planta ``plant_index`` de ``site`` (filas reales)."""
        authz = self.authz
        organization_id = site.organization_id
        plant_id = list(site.plants)[plant_index]
        zone_id = site.plants[plant_id][0]
        node_id = self.env.add_node(organization_id, plant_id, url=LIVE_VIEW_URL)
        self.env.assign_node(organization_id, plant_id, zone_id, node_id)
        user_id = authz.add_user(organization_id)
        assignment_id = authz.assign(
            organization_id, user_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_id
        )
        concession_id = authz.add_concession(
            organization_id,
            authz.add_provider_user(),
            level=ScopeLevel.PLANT,
            scope_id=plant_id,
            granted_at=authz.now() - HOUR,
        )
        evidence_id = self.evidence(site, plant_id, zone_id)
        (record,) = self.fetch(
            "SELECT record_id FROM ledger.evidence WHERE evidence_id = $1", evidence_id
        )
        label_id = self.label(site, plant_id, zone_id)
        return Ids(
            organization=organization_id,
            plant=plant_id,
            zone=zone_id,
            user=user_id,
            assignment=assignment_id,
            concession=concession_id,
            record=record["record_id"],
            evidence=evidence_id,
            event=self.dead_letter(organization_id, plant_id),
            label=label_id,
        )

    def organization(self) -> Organization:
        site = self.authz.add_site(plants=2, zones_per_plant=1)
        return Organization(site, self.resources(site, 0), self.resources(site, 1))

    # --- Personas ---------------------------------------------------------------------------

    def person(self, organization_id: uuid.UUID, *roles: Role) -> tuple[uuid.UUID, SessionCookie]:
        """Una persona con ``roles`` de nivel organización y su sesión."""
        user_id = self.authz.add_user(organization_id)
        for role in roles:
            self.authz.assign(organization_id, user_id, role)
        cookie: SessionCookie = self.authz.open_session(organization_id, user_id)
        return user_id, cookie

    def member(self, organization_id: uuid.UUID, *roles: Role) -> SessionCookie:
        return self.person(organization_id, *roles)[1]

    def installer(self) -> tuple[uuid.UUID, SessionCookie]:
        user_id = self.authz.add_provider_user()
        cookie = self.authz.open_session(self.authz.provider_organization_id, user_id)
        return user_id, cookie

    # --- Huellas ------------------------------------------------------------------------------

    def fingerprint(self, organization_id: uuid.UUID) -> dict[str, str]:
        """Huella de cada tabla con ``organization_id`` (filas de la organización, superusuario)."""
        tables = self.fetch(
            "SELECT DISTINCT n.nspname || '.' || c.relname AS name"
            " FROM pg_catalog.pg_attribute AS a"
            " JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid"
            " JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace"
            " WHERE n.nspname IN ('identity', 'ledger', 'shared', 'catalog')"
            " AND a.attname = 'organization_id' AND NOT a.attisdropped"
            " AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        )
        prints: dict[str, str] = {}
        for row in tables:
            name = row["name"]
            (digest,) = self.fetch(
                "SELECT count(*)::text || ':' || coalesce(md5(string_agg(to_jsonb(t)::text,"  # noqa: S608
                f" '|' ORDER BY to_jsonb(t)::text)), '') AS d FROM {name} AS t"
                " WHERE organization_id = $1",
                organization_id,
            )
            prints[name] = digest["d"]
        return prints

    def provider_queries(self, organization_id: uuid.UUID, concession_id: uuid.UUID) -> list[Any]:
        return [
            json.loads(row["content_json"])
            for row in self.fetch(
                "SELECT content_json FROM ledger.ledger_record WHERE organization_id = $1"
                " AND record_type = 'provider_query' ORDER BY chain_sequence",
                organization_id,
            )
            if json.loads(row["content_json"])["concession_id"] == str(concession_id)
        ]

    def concession_audit(self, organization_id: uuid.UUID, concession_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT operation, outcome FROM shared.audit_entry WHERE organization_id = $1"
            " AND actor_concession_id = $2 ORDER BY chain_sequence",
            organization_id,
            concession_id,
        )

    def audit_operations(self, organization_id: uuid.UUID, operation: str) -> int:
        (row,) = self.fetch(
            "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = $2",
            organization_id,
            operation,
        )
        return int(row["n"])


@pytest.fixture(scope="module")
def isolation(postgres_endpoint: PostgresEndpoint) -> Iterator[Isolation]:
    # La RLS de las concesiones compara la vigencia con la hora de la base (nuc_0009): el reloj
    # simulado arranca en ella antes del alta de las claves de firma, que así no caducan (VIG-135).
    with live_view_environment(postgres_endpoint, "route_isolation", at_database_time=True) as env:
        authz = env.authz
        sessions = authz.sessions
        registry = RecordTypeRegistry()
        for definition in (*U02_RECORD_TYPES, *COVERAGE_TYPES, *ADMISSION_TYPES):
            registry.register(definition)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
            registry.seal()

        env.run(synchronize())
        storage = StubEvidenceStorage()
        free_text = FreeTextPolicyRegistry()
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(storage, env.clock),
            outbox=sessions.outbox,
            clock=env.clock,
        )
        provider = authz.provider_organization_id
        deps = IdentityDependencies(
            database=sessions.database,
            writer=writer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=sessions.clock,
            provider_organization_id=provider,
        )
        pool = CpuPool(sessions.clock, max_workers=1)
        store = PostgresSessionStore(sessions.database, sessions.audit, sessions.outbox)
        second_factor = SecondFactorService(
            PostgresSecondFactorStore(sessions.database, sessions.audit),
            EnvelopeCipher(FakeKms(), "alias/vigia-secrets", sessions.clock),
            pool,
            sessions.clock,
        )
        passwords = RejectingPasswords()
        identity = IdentityHttp(
            login=LoginService(
                store=store,
                sessions=store,
                passwords=UnusedPasswords(),
                second_factor=second_factor,
                contexts=authz.contexts,
                clock=sessions.clock,
                provider_organization_id=provider,
                origin_key=ORIGIN_KEY,
            ),
            sessions=SessionService(store, authz.contexts, sessions.clock),
            invitations=InvitationService(
                deps,
                contexts=authz.contexts,
                passwords=FakeActivationPasswords(),
                second_factor=FakeActivationSecondFactor(),
            ),
            privacy_notice=PrivacyNoticeService(deps),
            passwords=PasswordChangeService(
                database=sessions.database,
                audit=sessions.audit,
                passwords=passwords,
                throttle=store,
                contexts=authz.contexts,
                clock=sessions.clock,
            ),
            me=MeService(
                sessions.database, contexts=authz.contexts, provider_organization_id=provider
            ),
            provider_organization_id=provider,
            users=UserService(deps, senders=EmailSenderRegistry(), link_base=LINK_BASE),
            roles=RoleService(deps),
            second_factor_reset=SecondFactorResetService(deps, second_factor),
            hierarchy=HierarchyService(deps),
            organization=OrganizationSettingsService(deps),
            concessions=ConcessionService(
                store=PostgresConcessionStore(database=sessions.database, audit=sessions.audit),
                writer=writer,
                authorizer=authz.authorizer,
                contexts=authz.contexts,
                clock=sessions.clock,
            ),
        )
        signing = SigningService(
            provider_organization_id=provider,
            store=env.store,
            secrets=env.secrets,
            events=env.events,
            clock=env.clock,
            environment=ENVIRONMENT,
        )
        env.run(signing.start())
        checkpoints = CheckpointService(
            store=SqlCheckpointStore(
                database=sessions.database,
                writer=writer,
                audit=sessions.audit,
                outbox=sessions.outbox,
            ),
            signer=signing,
            clock=env.clock,
        )
        ledger = LedgerHttp(
            reader=LectorExpediente(database=sessions.database, audit=sessions.audit),
            evidence=EvidenceService(
                database=sessions.database, audit=sessions.audit, storage=storage
            ),
            labels=LabelService(database=sessions.database, audit=sessions.audit),
            coverage=CoverageService(database=sessions.database, audit=sessions.audit),
            integrity_results=SqlIntegrityResults(database=sessions.database),
            integrity_requests=IntegrityRequests(
                database=sessions.database, outbox=sessions.outbox, clock=env.clock
            ),
            checkpoints=checkpoints,
            live_view=env.service(),
            authorizer=authz.authorizer,
            provider_organization_id=provider,
        )
        platform = PlatformHttp(
            signing=signing,
            dead_letter=DeadLetterReplay(
                database=sessions.database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=env.clock,
            ),
            operators=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            provider_organization_id=provider,
        )
        app = World(clock=sessions.clock).app(
            units=None,
            permissions=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=provider,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=sessions.clock,
                ),
                "identity": identity,
                "ledger": ledger,
                "platform": platform,
                "state": {
                    CATALOG_STATE_KEY: CatalogHttp(
                        admissions=AdmissionService(
                            repository=PostgresAdmissionRepository(sessions.database),
                            database=sessions.database,
                            writer=writer,
                            authorizer=authz.authorizer,
                            audit=sessions.audit,
                            free_text=free_text,
                            clock=sessions.clock,
                        ),
                        documents=DocumentService(
                            database=sessions.database,
                            audit=sessions.audit,
                            authorizer=authz.authorizer,
                            store=DocumentObjectStore(StubDocumentStorage()),
                            clock=env.clock,
                        ),
                    )
                },
            },
            static_dir=STATIC,
            public_origin=ORIGIN,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=30.0
        )
        world = Isolation(env, client, app, writer, passwords)
        world.a = world.organization()
        world.b = world.organization()
        try:
            yield world
        finally:
            env.run(client.aclose())
            pool.shutdown()


def _comparable(response: httpx.Response, own_trail_of: uuid.UUID | None = None) -> tuple[int, Any]:
    """Estado y cuerpo, sin ``correlation_id`` (lo único que cambia entre dos peticiones).

    Con ``own_trail_of``, sin las entradas de auditoría de esa persona (``Case.own_trail``).
    """
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "correlation_id"}
        if own_trail_of is not None and isinstance(body.get("items"), list):
            body["items"] = [
                item
                for item in body["items"]
                if not (isinstance(item, dict) and item["actor"]["id"] == str(own_trail_of))
            ]
    return response.status_code, body


def _mentions(
    response: httpx.Response, ids: Ids, own_trail_of: uuid.UUID | None = None
) -> list[str]:
    text = json.dumps(_comparable(response, own_trail_of)[1])
    return [str(value) for value in ids.values() if str(value) in text]


def _with_key(isolation: Isolation, kinds: set[Kind]) -> list[tuple[tuple[str, str], Case]]:
    return [
        (key, case)
        for key, case in CASES.items()
        if case.kind in kinds and key in declared(isolation.app.routes)
    ]


def _call(case: Case, ids: Ids) -> Call:
    assert case.call is not None
    return case.call(ids)


def _code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


# --- PR-NUC-01: el contexto de A contra los recursos de B ---------------------------------------


@pytest.mark.integration
def test_the_fixture_app_registers_exactly_the_cataloged_routes(isolation: Isolation) -> None:
    # La aplicación de la prueba es la de producción: mismas rutas que el catálogo, ninguna menos.
    assert uncovered(isolation.app.routes) == []
    assert set(declared(isolation.app.routes)) == set(CASES)


@pytest.mark.integration
def test_pr_nuc_01_a_known_resource_of_b_answers_exactly_like_a_missing_one(
    isolation: Isolation,
) -> None:
    a, b = isolation.a, isolation.b
    actor_id, actor = isolation.person(
        a.site.organization_id, Role.ADMINISTRATOR, Role.COORDINATOR_SST
    )
    failures: list[str] = []
    for (method, path), case in _with_key(isolation, {Kind.RESOURCE, Kind.PROVIDER_ONLY}):
        trail = actor_id if case.own_trail else None
        before = isolation.fingerprint(b.ids.organization)
        foreign = isolation.send(_call(case, b.ids), actor)
        missing = isolation.send(_call(case, Ids.missing()), actor)
        changed = sorted(
            name
            for name, digest in isolation.fingerprint(b.ids.organization).items()
            if before[name] != digest
        )
        route = f"{method} {path}"
        if _comparable(foreign, trail) != _comparable(missing, trail):
            failures.append(f"{route}: {_comparable(foreign)} != {_comparable(missing)}")
        if foreign.status_code == 403 or _code(foreign) == "forbidden":
            failures.append(f"{route}: forbidden revela que el recurso existe")
        if _mentions(foreign, b.ids, trail):
            failures.append(f"{route}: devuelve identificadores de B {_mentions(foreign, b.ids)}")
        if changed:
            failures.append(f"{route}: cambió filas de B en {changed}")
    assert not failures, "\n".join(failures)


@pytest.mark.integration
def test_pr_nuc_01_routes_without_identifier_never_show_or_touch_b(isolation: Isolation) -> None:
    a, b = isolation.a, isolation.b
    actor = isolation.member(a.site.organization_id, Role.ADMINISTRATOR, Role.COORDINATOR_SST)
    failures: list[str] = []
    for (method, path), case in _with_key(isolation, {Kind.OWN, Kind.SESSION}):
        before = isolation.fingerprint(b.ids.organization)
        response = isolation.send(_call(case, a.ids), actor)
        changed = sorted(
            name
            for name, digest in isolation.fingerprint(b.ids.organization).items()
            if before[name] != digest
        )
        route = f"{method} {path}"
        if response.status_code >= 500:
            failures.append(f"{route}: {response.status_code} {response.text}")
        for ids in (b.ids, b.other_plant):
            if _mentions(response, ids):
                failures.append(
                    f"{route}: devuelve identificadores de B {_mentions(response, ids)}"
                )
        if changed:
            failures.append(f"{route}: cambió filas de B en {changed}")
    assert not failures, "\n".join(failures)


@pytest.mark.integration
def test_the_own_routes_do_show_the_organizations_own_resources(isolation: Isolation) -> None:
    # Sin esto, «no muestra nada de B» lo cumpliría también una ruta que no muestra nada.
    a = isolation.a
    actor = isolation.member(a.site.organization_id, Role.ADMINISTRATOR, Role.COORDINATOR_SST)
    users = isolation.send(Call("GET", "/users", params={"limit": "200"}), actor)
    assert users.status_code == 200, users.text
    assert str(a.ids.user) in users.text and str(a.other_plant.user) in users.text
    hierarchy = isolation.send(Call("GET", "/hierarchy"), actor)
    assert str(a.ids.plant) in hierarchy.text and str(a.other_plant.zone) in hierarchy.text
    record = isolation.send(Call("GET", f"/ledger/records/{a.ids.record}"), actor)
    assert record.status_code == 200, record.text
    concessions = isolation.send(Call("GET", "/concessions"), actor)
    assert str(a.ids.concession) in concessions.text


# --- PR-NUC-01 bajo concesión (BR-NUC-37, 38) ---------------------------------------------------


@dataclass
class Observed:
    response: httpx.Response
    queries: list[Any]
    audit: list[Any]


def _observe(
    isolation: Isolation, call: Call, cookie: SessionCookie, concession: uuid.UUID
) -> Observed:
    client = isolation.b.ids.organization
    queries = len(isolation.provider_queries(client, concession))
    audit = len(isolation.concession_audit(client, concession))
    response = isolation.send(call, cookie, concession)
    return Observed(
        response,
        isolation.provider_queries(client, concession)[queries:],
        isolation.concession_audit(client, concession)[audit:],
    )


@pytest.mark.integration
@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT])
def test_pr_nuc_01_under_concession_the_provider_installer_column_decides(
    isolation: Isolation, level: ScopeLevel
) -> None:
    authz, b = isolation.authz, isolation.b
    installer, cookie = isolation.installer()
    concession = authz.add_concession(
        b.ids.organization,
        installer,
        level=level,
        scope_id=b.ids.plant if level is ScopeLevel.PLANT else None,
        granted_at=authz.now() - HOUR,
    )
    # Lo propio de la otra planta (la organización es la misma y sí puede aparecer).
    other_plant = replace(b.other_plant, organization=uuid7())
    failures: list[str] = []
    allowed_routes: list[str] = []
    for (method, path), case in _with_key(isolation, {Kind.RESOURCE, Kind.OWN, Kind.PROVIDER_ONLY}):
        route = f"{method} {path}"
        permission = _permission(isolation.app.routes, (method, path))
        in_column = permission in INSTALLER_KEYS
        expected_queries = 1 if in_column else 0
        own = _observe(isolation, _call(case, b.ids), cookie, concession)
        missing = _observe(isolation, _call(case, Ids.missing()), cookie, concession)
        for name, seen in (("propio", own), ("inexistente", missing)):
            documents = [(q["method"], q["resource"]) for q in seen.queries]
            if documents != [(method, path)] * expected_queries:
                failures.append(f"{route} ({name}): provider_query {documents}")
        if in_column and (method, path) not in PROVIDER_SIDE_ONLY:
            allowed_routes.append(route)
            if not 200 <= own.response.status_code < 300:
                failures.append(f"{route}: en la columna y responde {own.response.text}")
            if not own.audit:
                failures.append(f"{route}: sin auditoría normal de la petición del proveedor")
            if case.kind is Kind.RESOURCE and _code(missing.response) != "not_found":
                failures.append(f"{route}: inexistente responde {missing.response.text}")
            if level is ScopeLevel.PLANT:
                # La otra planta del cliente: igual que inexistente (o ausente del listado).
                other = _observe(isolation, _call(case, b.other_plant), cookie, concession)
                if case.kind is Kind.RESOURCE and _comparable(other.response) != _comparable(
                    missing.response
                ):
                    failures.append(f"{route}: otra planta {_comparable(other.response)}")
                if _mentions(other.response, other_plant) or _mentions(own.response, other_plant):
                    failures.append(f"{route}: muestra la otra planta")
        else:
            if _comparable(own.response) != _comparable(missing.response):
                failures.append(
                    f"{route}: {_comparable(own.response)} != {_comparable(missing.response)}"
                )
            if _code(own.response) != "not_found":
                failures.append(f"{route}: fuera de la columna responde {own.response.text}")
            if not in_column and "authorization_denied" not in {e["operation"] for e in own.audit}:
                # La denegación por ruta del proveedor queda en la auditoría del cliente.
                failures.append(f"{route}: denegación sin auditar {own.audit}")
    assert not failures, "\n".join(failures)
    # Las rutas de la columna que existen hoy: si una desaparece, la prueba ya no las prueba.
    assert set(allowed_routes) == {
        "POST /documents",
        "GET /hierarchy",
        "GET /zones/{zone_id}/coverage",
        "GET /zones/{zone_id}/coverage/at",
        "POST /zones/{zone_id}/live-view-token",
        "GET /plants/{plant_id}/admissions",
    }
    # Ningún acceso del proveedor es invisible para el cliente (BR-NUC-41): cada provider_query
    # está en GET /concessions/{id}/queries.
    client_admin = isolation.member(b.ids.organization, Role.ADMINISTRATOR)
    listed: list[Any] = []
    params: dict[str, str] = {}
    while True:
        page = isolation.send(
            Call("GET", f"/concessions/{concession}/queries", params), client_admin
        )
        assert page.status_code == 200, page.text
        body = page.json()
        listed += body["queries"]
        if body["next_after"] is None:
            break
        params = {"after": body["next_after"]}
    recorded = isolation.provider_queries(b.ids.organization, concession)
    assert [(q["method"], q["resource"]) for q in listed] == [
        (q["method"], q["resource"]) for q in recorded
    ]
    assert recorded


@pytest.mark.integration
def test_br_nuc_38_a_hierarchy_read_under_concession_is_audited(isolation: Isolation) -> None:
    authz, b = isolation.authz, isolation.b
    installer, cookie = isolation.installer()
    concession = authz.add_concession(b.ids.organization, installer, granted_at=authz.now() - HOUR)
    seen = _observe(isolation, Call("GET", "/hierarchy"), cookie, concession)
    assert seen.response.status_code == 200, seen.response.text
    assert len(seen.queries) == 1
    # Su entrada de auditoría normal (VIG-135), en la cadena del cliente y con su concesión.
    assert [(e["operation"], e["outcome"]) for e in seen.audit] == [("hierarchy_read", "success")]
    # La lectura de un miembro del cliente, sin concesión, no se audita (no es de BR-NUC-38).
    member = isolation.member(b.ids.organization, Role.ADMINISTRATOR)
    before = isolation.audit_operations(b.ids.organization, "hierarchy_read")
    assert isolation.send(Call("GET", "/hierarchy"), member).status_code == 200
    assert isolation.audit_operations(b.ids.organization, "hierarchy_read") == before


@pytest.mark.integration
def test_after_revocation_no_route_answers_the_provider(isolation: Isolation) -> None:
    authz, b = isolation.authz, isolation.b
    installer, cookie = isolation.installer()
    concession = authz.add_concession(b.ids.organization, installer, granted_at=authz.now() - HOUR)
    coverage = CASES[("GET", "/zones/{zone_id}/coverage")]
    assert isolation.send(_call(coverage, b.ids), cookie, concession).status_code == 200
    authz.revoke_concession(concession, authz.now())
    for (method, path), case in _with_key(isolation, {Kind.RESOURCE, Kind.OWN}):
        response = isolation.send(_call(case, b.ids), cookie, concession)
        assert _code(response) in ("not_found", "unauthenticated"), (method, path, response.text)
        assert not _mentions(response, b.ids), (method, path)


# --- Seguimientos de VIG-86: etiquetas de dos organizaciones y dos plantas -----------------------


@pytest.mark.integration
def test_labels_of_two_organizations_and_two_plants(isolation: Isolation) -> None:
    a, b, authz = isolation.a, isolation.b, isolation.authz
    period = Call("GET", "/labels", _coverage_period())
    organization_wide = isolation.member(a.site.organization_id, Role.COORDINATOR_SST)
    response = isolation.send(period, organization_wide)
    assert response.status_code == 200, response.text
    assert str(a.ids.label) in response.text and str(a.other_plant.label) in response.text
    assert not _mentions(response, b.ids) and not _mentions(response, b.other_plant)
    # Un coordinador de la planta 0: solo la etiqueta de su planta.
    user = authz.add_user(a.site.organization_id)
    authz.assign(a.site.organization_id, user, Role.COORDINATOR_SST, ScopeLevel.PLANT, a.ids.plant)
    plant_only = authz.open_session(a.site.organization_id, user)
    response = isolation.send(period, plant_only)
    assert response.status_code == 200, response.text
    assert str(a.ids.label) in response.text
    assert str(a.other_plant.label) not in response.text
    # La zona de la otra planta, igual que una inexistente.
    other_zone = isolation.send(
        Call("GET", "/labels", {**_coverage_period(), "zone_id": str(a.other_plant.zone)}),
        plant_only,
    )
    missing_zone = isolation.send(
        Call("GET", "/labels", {**_coverage_period(), "zone_id": str(uuid7())}), plant_only
    )
    assert _comparable(other_zone) == _comparable(missing_zone)
    # El proveedor con concesión de la planta 0 de B: labels.read no está en su columna.
    installer, cookie = isolation.installer()
    concession = authz.add_concession(
        b.ids.organization,
        installer,
        level=ScopeLevel.PLANT,
        scope_id=b.ids.plant,
        granted_at=authz.now() - HOUR,
    )
    seen = _observe(isolation, period, cookie, concession)
    assert _code(seen.response) == "not_found" and seen.queries == []


# --- Seguimientos de VIG-83: roles fuera del alcance por HTTP -----------------------------------


@pytest.mark.integration
def test_a_plant_administrator_neither_assigns_nor_removes_in_another_plant(
    isolation: Isolation,
) -> None:
    a, authz = isolation.a, isolation.authz
    organization_id = a.site.organization_id
    user = authz.add_user(organization_id)
    authz.assign(organization_id, user, Role.ADMINISTRATOR, ScopeLevel.PLANT, a.ids.plant)
    plant_admin = authz.open_session(organization_id, user)
    target = authz.add_user(organization_id)
    other_plant = Call(
        "POST",
        f"/users/{target}/roles",
        json={"role": "copasst", "scope_level": "plant", "scope_id": str(a.other_plant.plant)},
    )
    missing_plant = replace(other_plant, json={**other_plant.json, "scope_id": str(uuid7())})
    assigned = isolation.send(other_plant, plant_admin)
    assert _comparable(assigned) == _comparable(isolation.send(missing_plant, plant_admin))
    assert _code(assigned) == "not_found"
    assert not isolation.fetch("SELECT 1 FROM identity.role_assignment WHERE user_id = $1", target)
    # Retirar la asignación de la otra planta: igual que una inexistente, y sigue vigente.
    remove = Call("DELETE", f"/users/{a.other_plant.user}/roles/{a.other_plant.assignment}")
    removed = isolation.send(remove, plant_admin)
    missing = isolation.send(
        Call("DELETE", f"/users/{a.other_plant.user}/roles/{uuid7()}"), plant_admin
    )
    assert _comparable(removed) == _comparable(missing) and _code(removed) == "not_found"
    (row,) = isolation.fetch(
        "SELECT removed_at FROM identity.role_assignment WHERE assignment_id = $1",
        a.other_plant.assignment,
    )
    assert row["removed_at"] is None
    # La asignación de otra persona por la ruta de una tercera: tampoco existe.
    organization_admin = isolation.member(organization_id, Role.ADMINISTRATOR)
    crossed = isolation.send(
        Call("DELETE", f"/users/{a.ids.user}/roles/{a.other_plant.assignment}"),
        organization_admin,
    )
    assert _code(crossed) == "not_found"
    (row,) = isolation.fetch(
        "SELECT removed_at FROM identity.role_assignment WHERE assignment_id = $1",
        a.other_plant.assignment,
    )
    assert row["removed_at"] is None


# --- Seguimientos de VIG-82: rutas de sesión bajo concesión -------------------------------------


@pytest.mark.integration
def test_me_under_concession_shows_the_concession_and_only_the_installer_column(
    isolation: Isolation,
) -> None:
    authz, b = isolation.authz, isolation.b
    installer, cookie = isolation.installer()
    concession = authz.add_concession(
        b.ids.organization,
        installer,
        level=ScopeLevel.PLANT,
        scope_id=b.ids.plant,
        granted_at=authz.now() - HOUR,
    )
    response = isolation.send(Call("GET", "/me"), cookie, concession)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["concession_id"] == str(concession)
    assert set(body["effective_permissions"]) <= INSTALLER_KEYS
    assert not _mentions(response, b.other_plant)
    assert str(b.ids.user) not in response.text


@pytest.mark.integration
def test_password_change_under_concession_is_rejected_before_touching_the_client(
    isolation: Isolation,
) -> None:
    authz, b = isolation.authz, isolation.b
    installer, cookie = isolation.installer()
    concession = authz.add_concession(b.ids.organization, installer, granted_at=authz.now() - HOUR)
    verified = isolation.passwords.verified
    throttle = isolation.fetch(
        "SELECT count(*) AS n FROM identity.auth_throttle WHERE organization_id = $1",
        b.ids.organization,
    )
    response = isolation.send(_call(CASES[("POST", "/auth/password")], b.ids), cookie, concession)
    assert 400 <= response.status_code < 500, response.text
    # La guarda de concesión corta antes de reservar el retardo en la organización del cliente y
    # de verificar la contraseña: sin ella, la reserva deja su fila en el cliente.
    assert isolation.passwords.verified == verified
    assert (
        isolation.fetch(
            "SELECT count(*) AS n FROM identity.auth_throttle WHERE organization_id = $1",
            b.ids.organization,
        )
        == throttle
    )


# --- Seguimiento de VIG-76: motivo vacío de hecho -----------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "reason",
    [
        " " * 12,
        NBSP * 12,
        "." * 12,
        ZERO_WIDTH_SPACE * 12,
        f" . - _ , ; : {NBSP}{ZERO_WIDTH_SPACE} ",
    ],
    ids=["espacios", "nbsp", "puntos", "invisibles", "mezcla"],
)
def test_a_reason_without_content_is_rejected(isolation: Isolation, reason: str) -> None:
    b = isolation.b
    _, cookie = isolation.installer()
    before = isolation.fetch(
        "SELECT count(*) AS n FROM identity.provider_concession WHERE organization_id = $1",
        b.ids.organization,
    )
    response = isolation.send(
        Call(
            "POST",
            "/provider/concessions",
            json={
                "client_organization_id": str(b.ids.organization),
                "scope_level": "plant",
                "scope_id": str(b.ids.plant),
                "reason": reason,
            },
        ),
        cookie,
    )
    # El mismo rechazo que un motivo corto (``reason_invalid`` se traduce a invalid_request).
    assert response.status_code == 400, response.text
    assert _code(response) == "invalid_request", response.text
    assert (
        isolation.fetch(
            "SELECT count(*) AS n FROM identity.provider_concession WHERE organization_id = $1",
            b.ids.organization,
        )
        == before
    )
