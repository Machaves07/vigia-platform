"""Rutas de administración de extremo a extremo (TASK-136; ``business-logic-model.md`` §10.2).

La aplicación real (``create_app`` con la cadena fija de middleware y las rutas de
``platform_units()``) contra PostgreSQL 16 real como ``vigia_app``, con los servicios reales de
``identity`` (jerarquía, usuarios, roles, concesiones, segundo factor), el ``EscritorExpediente``
real y ``ContextAuthorizer``. Las peticiones van por ``httpx.ASGITransport`` en el mismo bucle de
la base. Las sesiones se abren directamente en la base (``open_session``): el inicio de sesión lo
prueba ``test_auth_routes``.

- **H-54 / BR-NUC-14**: asignar ``line_manager`` sobre una zona a quien es ``coordinator_sst`` de
  su planta responde ``conflict`` y queda auditado como ``role_assignment_rejected`` con la
  asignación en conflicto; en la dirección contraria, también; en otra planta, se acepta.
- **H-56 / BR-NUC-39 y 41**: el proveedor se concede acceso, lo usa con ``X-Vigia-Concession``, el
  cliente lo ve en su panel con el motivo, lo revoca y la petición siguiente del proveedor
  responde ``not_found``; el panel del cliente y la lista del proveedor muestran ``revoked``.
- **VIG-132 / BR-NUC-38 y 41**: ``GET /hierarchy`` bajo concesión aparece una sola vez en
  ``GET /concessions/{id}/queries`` del cliente; otra organización y el proveedor reciben
  ``not_found`` en esa ruta.
- Bordes de cada grupo: páginas de ``GET /users`` (``limit`` 0, 1, 200 y 201), correo repetido,
  sin asignaciones, último administrador, segundo factor, retiro repetido, código repetido,
  planta de otra organización, alcance de ``GET /hierarchy``, topes de concesión (``default >
  max``, 0 y 91), duración fuera del tope del cliente, motivo corto, cursor ilegible y rutas sin
  la clave (``not_found``, nunca ``forbidden``).

La prueba de aislamiento por ruta con dos organizaciones es de TASK-139.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import text

from tests.api_support import World
from tests.authz_support import Site
from tests.hierarchy_support import (
    HierarchyEnvironment,
    hierarchy_environment,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.second_factor_support import FakeKms
from tests.session_support import ORIGIN_KEY
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.http.concessions import decode_cursor, encode_cursor
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.concessions import ConcessionService, ProviderQueryCursor
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.organization import OrganizationSettingsService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.users import SecondFactorResetService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie, SessionService
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import iter_declared_routes
from vigia_platform.shared.api.errors import ApiError
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

STATIC: Final = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
REASON: Final = "Mantenimiento sintético del nodo de la línea 2"
CONCESSION_HEADER: Final = "X-Vigia-Concession"


class UnusedPasswords:
    """Las rutas de administración no verifican contraseñas (``LoginService`` lo exige)."""

    async def verify(self, password: str, encoded: str) -> Any:
        raise AssertionError("no se usa")

    async def hash(self, password: str) -> Any:
        raise AssertionError("no se usa")

    async def check_policy(self, password: str, email: str) -> Any:
        raise AssertionError("no se usa")


@dataclass
class Api:
    env: HierarchyEnvironment
    client: httpx.AsyncClient
    app: Any

    def request(
        self,
        method: str,
        path: str,
        *,
        cookie: SessionCookie | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        concession: uuid.UUID | None = None,
    ) -> httpx.Response:
        headers = dict(SAME_ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.env.run(
            self.client.request(method, path, headers=headers, json=json, params=params)
        )
        return response

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.env.fetch(sql, *args)


@pytest.fixture(scope="module")
def api(postgres_endpoint: PostgresEndpoint) -> Iterator[Api]:
    with hierarchy_environment(postgres_endpoint, "admin_routes") as env:
        authz = env.authz
        sessions = authz.sessions
        # La RLS de las concesiones compara la vigencia con la hora de la base (nuc_0009).
        (row,) = env.fetch("SELECT now() AS now")
        sessions.clock.set(row["now"])
        pool = CpuPool(sessions.clock, max_workers=1)
        store = PostgresSessionStore(sessions.database, sessions.audit, sessions.outbox)
        second_factor = SecondFactorService(
            PostgresSecondFactorStore(sessions.database, sessions.audit),
            EnvelopeCipher(FakeKms(), "alias/vigia-secrets", sessions.clock),
            pool,
            sessions.clock,
        )
        provider = authz.provider_organization_id
        concessions = ConcessionService(
            store=PostgresConcessionStore(database=sessions.database, audit=sessions.audit),
            writer=env.writer,
            authorizer=authz.authorizer,
            contexts=authz.contexts,
            clock=sessions.clock,
        )
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
            invitations=env.invitations(),
            privacy_notice=PrivacyNoticeService(env.deps),
            passwords=PasswordChangeService(
                database=sessions.database,
                audit=sessions.audit,
                passwords=UnusedPasswords(),
                throttle=store,
                contexts=authz.contexts,
                clock=sessions.clock,
            ),
            me=MeService(
                sessions.database, contexts=authz.contexts, provider_organization_id=provider
            ),
            provider_organization_id=provider,
            users=env.users(),
            roles=env.roles(),
            second_factor_reset=SecondFactorResetService(env.deps, second_factor),
            hierarchy=env.hierarchy(),
            organization=OrganizationSettingsService(env.deps),
            concessions=concessions,
        )
        app = World(clock=sessions.clock).app(
            units=None,
            permissions=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=provider,
                    provider_queries=LedgerProviderQueryLedger(env.writer),
                    clock=sessions.clock,
                ),
                "identity": identity,
            },
            static_dir=STATIC,
            public_origin=ORIGIN,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=10.0
        )
        try:
            yield Api(env, client, app)
        finally:
            env.run(client.aclose())
            pool.shutdown()


# --- Datos --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Client:
    site: Site
    admin_id: uuid.UUID
    admin: SessionCookie

    @property
    def organization_id(self) -> uuid.UUID:
        return self.site.organization_id

    @property
    def plant_id(self) -> uuid.UUID:
        return next(iter(self.site.plants))

    def zone_id(self, plant: int = 0, zone: int = 0) -> uuid.UUID:
        return list(self.site.plants.values())[plant][zone]

    def plant(self, index: int) -> uuid.UUID:
        return list(self.site.plants)[index]


def _client(api: Api, plants: int = 2, zones_per_plant: int = 2) -> Client:
    """Organización cliente con un administrador de nivel organización y su sesión."""
    authz = api.env.authz
    site = authz.add_site(plants=plants, zones_per_plant=zones_per_plant)
    admin = authz.add_user(site.organization_id)
    authz.assign(site.organization_id, admin, Role.ADMINISTRATOR)
    return Client(site, admin, authz.open_session(site.organization_id, admin))


def _member(
    api: Api,
    client: Client,
    role: Role,
    level: ScopeLevel = ScopeLevel.ORGANIZATION,
    scope_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Una persona activa con ``role`` sobre el alcance; devuelve ``(user_id, assignment_id)``."""
    authz = api.env.authz
    user = authz.add_user(client.organization_id)
    assignment = authz.assign(client.organization_id, user, role, level, scope_id)
    return user, assignment


def _session(api: Api, client: Client, user_id: uuid.UUID) -> SessionCookie:
    return api.env.authz.open_session(client.organization_id, user_id)


def _assignment(role: Role, level: ScopeLevel, scope_id: uuid.UUID) -> dict[str, str]:
    return {"role": role.value, "scope_level": level.value, "scope_id": str(scope_id)}


def _code(response: httpx.Response) -> str:
    code: str = response.json()["code"]
    return code


def _audit(api: Api, organization_id: uuid.UUID, operation: str) -> list[Any]:
    return api.fetch(
        "SELECT outcome, resource_id, convert_from(filters, 'UTF8') AS filters,"
        " actor_role_in_use FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = $2 ORDER BY chain_sequence",
        organization_id,
        operation,
    )


# --- H-54: incompatibilidad de roles con el conflicto nombrado -----------------------------------


def test_h54_line_manager_over_a_zone_of_the_coordinators_plant_is_rejected_and_audited(
    api: Api,
) -> None:
    client = _client(api)
    zone = client.zone_id(0, 0)
    coordinator, plant_assignment = _member(
        api, client, Role.COORDINATOR_SST, ScopeLevel.PLANT, client.plant_id
    )
    response = api.request(
        "POST",
        f"/users/{coordinator}/roles",
        cookie=client.admin,
        json=_assignment(Role.LINE_MANAGER, ScopeLevel.ZONE, zone),
    )
    assert response.status_code == 409, response.text
    assert _code(response) == "conflict"
    # El cuerpo es el genérico de la plataforma: nunca repite lo recibido.
    assert set(response.json()) == {"code", "message_es", "correlation_id"}
    rows = [
        row
        for row in _audit(api, client.organization_id, "role_assignment_rejected")
        if row["resource_id"] == coordinator
    ]
    assert len(rows) == 1
    filters = json.loads(rows[0]["filters"])
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["actor_role_in_use"] == "administrator"
    assert filters["conflict_assignment_id"] == str(plant_assignment)
    assert filters["reason"] == "role_incompatible"
    assert filters["role"] == "line_manager" and filters["scope_level"] == "zone"
    # Nada se escribió: la persona sigue con su única asignación.
    listed = api.request("GET", "/users", cookie=client.admin).json()["users"]
    (person,) = [user for user in listed if user["user_id"] == str(coordinator)]
    assert [a["assignment_id"] for a in person["assignments"]] == [str(plant_assignment)]


def test_h54_the_rule_holds_in_the_other_direction(api: Api) -> None:
    client = _client(api)
    line_manager, zone_assignment = _member(
        api, client, Role.LINE_MANAGER, ScopeLevel.ZONE, client.zone_id(0, 1)
    )
    response = api.request(
        "POST",
        f"/users/{line_manager}/roles",
        cookie=client.admin,
        json=_assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, client.plant_id),
    )
    assert response.status_code == 409 and _code(response) == "conflict"
    (row,) = [
        row
        for row in _audit(api, client.organization_id, "role_assignment_rejected")
        if row["resource_id"] == line_manager
    ]
    assert json.loads(row["filters"])["conflict_assignment_id"] == str(zone_assignment)


def test_h54_a_zone_of_another_plant_does_not_conflict(api: Api) -> None:
    client = _client(api)
    coordinator, _ = _member(api, client, Role.COORDINATOR_SST, ScopeLevel.PLANT, client.plant(0))
    other_zone = client.zone_id(1, 0)
    response = api.request(
        "POST",
        f"/users/{coordinator}/roles",
        cookie=client.admin,
        json=_assignment(Role.LINE_MANAGER, ScopeLevel.ZONE, other_zone),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "line_manager" and body["scope_id"] == str(other_zone)
    assert body["user_id"] == str(coordinator)
    # La misma asignación otra vez: también es un conflicto (repetida).
    again = api.request(
        "POST",
        f"/users/{coordinator}/roles",
        cookie=client.admin,
        json=_assignment(Role.LINE_MANAGER, ScopeLevel.ZONE, other_zone),
    )
    assert again.status_code == 409


def test_nobody_assigns_themself_a_role_they_do_not_hold(api: Api) -> None:
    client = _client(api)
    response = api.request(
        "POST",
        f"/users/{client.admin_id}/roles",
        cookie=client.admin,
        json=_assignment(Role.COPASST, ScopeLevel.ZONE, client.zone_id()),
    )
    assert response.status_code == 409 and _code(response) == "conflict"
    rows = _audit(api, client.organization_id, "role_assignment_rejected")
    assert any("self_assignment" in row["filters"] for row in rows)


def test_a_provider_role_is_not_assignable_in_a_client(api: Api) -> None:
    client = _client(api)
    user, _ = _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    response = api.request(
        "POST",
        f"/users/{user}/roles",
        cookie=client.admin,
        json=_assignment(Role.PROVIDER_INSTALLER, ScopeLevel.ORGANIZATION, client.organization_id),
    )
    assert response.status_code == 400 and _code(response) == "invalid_request"


def test_remove_a_role_then_again_and_from_another_user(api: Api) -> None:
    client = _client(api)
    user, assignment = _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    other, _ = _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    # La asignación no es de ``other``: como inexistente.
    wrong_owner = api.request("DELETE", f"/users/{other}/roles/{assignment}", cookie=client.admin)
    assert wrong_owner.status_code == 404 and _code(wrong_owner) == "not_found"
    removed = api.request("DELETE", f"/users/{user}/roles/{assignment}", cookie=client.admin)
    assert removed.status_code == 204 and removed.content == b""
    again = api.request("DELETE", f"/users/{user}/roles/{assignment}", cookie=client.admin)
    assert again.status_code == 409 and _code(again) == "conflict"
    (row,) = api.fetch(
        "SELECT removed_at FROM identity.role_assignment WHERE assignment_id = $1", assignment
    )
    assert row["removed_at"] is not None  # nunca se borra


def test_the_last_administrators_assignment_is_not_removed(api: Api) -> None:
    client = _client(api)
    (row,) = api.fetch(
        "SELECT assignment_id FROM identity.role_assignment WHERE user_id = $1", client.admin_id
    )
    response = api.request(
        "DELETE", f"/users/{client.admin_id}/roles/{row['assignment_id']}", cookie=client.admin
    )
    assert response.status_code == 409 and _code(response) == "conflict"


# --- H-56: revocación inmediata visible al cliente -----------------------------------------------


def test_h56_after_revoke_the_next_provider_request_is_not_found(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    provider = authz.open_session(authz.provider_organization_id, installer)
    granted = api.request(
        "POST",
        "/provider/concessions",
        cookie=provider,
        json={
            "client_organization_id": str(client.organization_id),
            "scope_level": "organization",
            "scope_id": str(client.organization_id),
            "reason": REASON,
            "duration_hours": 4,
        },
    )
    assert granted.status_code == 201, granted.text
    concession = granted.json()
    concession_id = uuid.UUID(concession["concession_id"])
    assert concession["status"] == "active" and "reason" not in concession
    assert concession["client_organization_id"] == str(client.organization_id)
    # El proveedor la ve en su lista, sin el motivo.
    own = api.request("GET", "/provider/concessions", cookie=provider).json()["concessions"]
    assert [item["concession_id"] for item in own] == [str(concession_id)]
    # Y la usa: el contexto es el del cliente.
    used = api.request("GET", "/hierarchy", cookie=provider, concession=concession_id)
    assert used.status_code == 200, used.text
    assert used.json()["organization"]["organization_id"] == str(client.organization_id)
    # El cliente la ve en su panel con el motivo (BR-NUC-41).
    panel = api.request("GET", "/concessions", cookie=client.admin).json()["concessions"]
    (shown,) = panel
    assert shown["concession_id"] == str(concession_id)
    assert shown["status"] == "active" and shown["reason"] == REASON
    assert shown["provider_user_id"] == str(installer)
    # Revocación por el cliente.
    revoked = api.request("POST", f"/concessions/{concession_id}/revoke", cookie=client.admin)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_by_side"] == "client"
    # La petición siguiente del proveedor, con la misma sesión: not_found (BR-NUC-39).
    after = api.request("GET", "/hierarchy", cookie=provider, concession=concession_id)
    assert after.status_code == 404 and _code(after) == "not_found"
    # Su sesión sigue viva para su propia organización.
    assert api.request("GET", "/provider/concessions", cookie=provider).status_code == 200
    # El cliente y el proveedor ven el estado honesto.
    panel = api.request("GET", "/concessions", cookie=client.admin).json()["concessions"]
    assert panel[0]["status"] == "revoked" and panel[0]["revoked_at"] is not None
    own = api.request("GET", "/provider/concessions", cookie=provider).json()["concessions"]
    assert own[0]["status"] == "revoked" and own[0]["revoked_by_side"] == "client"
    # Revocar otra vez: conflicto.
    again = api.request("POST", f"/concessions/{concession_id}/revoke", cookie=client.admin)
    assert again.status_code == 409 and _code(again) == "conflict"
    # Constancia en el expediente del cliente.
    records = api.fetch(
        "SELECT record_type FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type LIKE 'provider_concession_%' ORDER BY received_at",
        client.organization_id,
    )
    assert [r["record_type"] for r in records] == [
        "provider_concession_granted",
        "provider_concession_revoked",
    ]


def test_provider_queries_page_and_cursor(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    concession_id = authz.add_concession(client.organization_id, installer)
    page = api.request("GET", f"/concessions/{concession_id}/queries", cookie=client.admin)
    assert page.status_code == 200, page.text
    assert page.json() == {"queries": [], "next_after": None}
    for bad in ("x" * 129, "no válido", "AAAA", _naive_cursor()):
        response = api.request(
            "GET",
            f"/concessions/{concession_id}/queries",
            cookie=client.admin,
            params={"after": bad},
        )
        assert response.status_code == 400, bad
    for limit in (0, 201):
        response = api.request(
            "GET",
            f"/concessions/{concession_id}/queries",
            cookie=client.admin,
            params={"limit": limit},
        )
        assert response.status_code == 400
    unknown = api.request("GET", f"/concessions/{uuid.uuid4()}/queries", cookie=client.admin)
    assert unknown.status_code == 404


def test_vig132_a_hierarchy_read_under_concession_shows_in_the_clients_queries(
    api: Api,
) -> None:
    """VIG-132 (BR-NUC-38 y 41): la lectura del proveedor aparece en el panel del cliente."""
    authz = api.env.authz
    client = _client(api)
    other = _client(api, plants=1, zones_per_plant=1)
    installer = authz.add_provider_user()
    provider = authz.open_session(authz.provider_organization_id, installer)
    granted = api.request(
        "POST",
        "/provider/concessions",
        cookie=provider,
        json={
            "client_organization_id": str(client.organization_id),
            "scope_level": "plant",
            "scope_id": str(client.plant_id),
            "reason": REASON,
        },
    )
    assert granted.status_code == 201, granted.text
    concession_id = uuid.UUID(granted.json()["concession_id"])
    queries = f"/concessions/{concession_id}/queries"
    assert api.request("GET", queries, cookie=client.admin).json()["queries"] == []
    moment = format_timestamp(authz.sessions.clock.now())

    used = api.request("GET", "/hierarchy", cookie=provider, concession=concession_id)

    assert used.status_code == 200, used.text
    page = api.request("GET", queries, cookie=client.admin)
    assert page.status_code == 200, page.text
    (item,) = page.json()["queries"]
    assert (item["operation"], item["method"], item["resource"]) == ("read", "GET", "/hierarchy")
    assert item["occurred_at"] == moment
    assert item["plant_id"] == str(client.plant_id)
    assert item["provider_user_id"] == str(installer) and item["reason"] == REASON
    # Nadie más ve el panel: otra organización, ni el proveedor con o sin la concesión.
    assert api.request("GET", queries, cookie=other.admin).status_code == 404
    for concession in (concession_id, None):
        refused = api.request("GET", queries, cookie=provider, concession=concession)
        assert refused.status_code == 404 and _code(refused) == "not_found"
    # Las peticiones rechazadas antes de la ruta no se cuentan como acceso.
    assert len(api.request("GET", queries, cookie=client.admin).json()["queries"]) == 1


def _naive_cursor() -> str:
    """Un cursor bien codificado pero con una marca sin zona horaria."""
    raw = f"2026-09-30T10:00:00|{uuid.uuid4()}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_cursor_round_trip_keeps_microseconds() -> None:
    cursor = ProviderQueryCursor(
        datetime.fromisoformat("2026-09-30T10:00:00.123456+00:00"), uuid.uuid4()
    )
    assert decode_cursor(encode_cursor(cursor)) == cursor
    for bad in (_naive_cursor(), "", "=" * 4, "a" * 129):
        with pytest.raises(ApiError):
            decode_cursor(bad)


def test_grant_limits_and_wrong_contexts(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    api.request(
        "PATCH",
        "/organization/settings",
        cookie=client.admin,
        json={"concession_default_days": 1, "concession_max_days": 2},
    )
    installer = authz.add_provider_user()
    provider = authz.open_session(authz.provider_organization_id, installer)

    def grant(**changes: Any) -> httpx.Response:
        body: dict[str, Any] = {
            "client_organization_id": str(client.organization_id),
            "scope_level": "plant",
            "scope_id": str(client.plant_id),
            "reason": REASON,
        }
        body.update(changes)
        return api.request("POST", "/provider/concessions", cookie=provider, json=body)

    # Duración: el tope del cliente (2 días = 48 h) y justo fuera.
    assert grant(duration_hours=49).status_code == 400
    assert grant(duration_hours=0).status_code == 400
    assert grant(reason="x" * 9).status_code == 400
    assert grant(scope_level="zone", scope_id=str(client.zone_id())).status_code == 400
    assert grant(client_organization_id=str(uuid.uuid4())).status_code == 404
    assert grant(client_organization_id=str(authz.provider_organization_id)).status_code == 404
    ok = grant(duration_hours=48)
    assert ok.status_code == 201, ok.text
    assert ok.json()["scope_level"] == "plant"
    # Sin duración: la de omisión del cliente (1 día).
    default = grant()
    assert default.status_code == 201
    granted_at = datetime.fromisoformat(default.json()["granted_at"])
    expires_at = datetime.fromisoformat(default.json()["expires_at"])
    assert (expires_at - granted_at).total_seconds() == 24 * 3600
    # Bajo concesión el proveedor no concede ni lista concesiones.
    concession_id = uuid.UUID(ok.json()["concession_id"])
    for method, path in (("GET", "/provider/concessions"), ("GET", "/concessions")):
        under = api.request(method, path, cookie=provider, concession=concession_id)
        assert under.status_code == 404, (path, under.text)
    nested = api.request(
        "POST",
        "/provider/concessions",
        cookie=provider,
        concession=concession_id,
        json={
            "client_organization_id": str(client.organization_id),
            "scope_level": "organization",
            "scope_id": str(client.organization_id),
            "reason": REASON,
        },
    )
    assert nested.status_code == 404
    # Un cliente no se concede concesiones.
    from_client = api.request(
        "POST",
        "/provider/concessions",
        cookie=client.admin,
        json={
            "client_organization_id": str(client.organization_id),
            "scope_level": "organization",
            "scope_id": str(client.organization_id),
            "reason": REASON,
        },
    )
    assert from_client.status_code == 404


def test_concession_panel_by_plant_and_permission(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    whole = authz.add_concession(client.organization_id, installer)
    first = authz.add_concession(
        client.organization_id, installer, level=ScopeLevel.PLANT, scope_id=client.plant(0)
    )
    second = authz.add_concession(
        client.organization_id, installer, level=ScopeLevel.PLANT, scope_id=client.plant(1)
    )
    listed = api.request(
        "GET", "/concessions", cookie=client.admin, params={"plant_id": str(client.plant(0))}
    ).json()["concessions"]
    assert {item["concession_id"] for item in listed} == {str(whole), str(first)}
    everything = api.request("GET", "/concessions", cookie=client.admin).json()["concessions"]
    assert {item["concession_id"] for item in everything} == {str(whole), str(first), str(second)}
    # Un gerente de una planta revoca solo lo que alcanza su planta.
    manager, _ = _member(api, client, Role.PLANT_MANAGER, ScopeLevel.PLANT, client.plant(0))
    manager_session = _session(api, client, manager)
    denied = api.request("POST", f"/concessions/{second}/revoke", cookie=manager_session)
    assert denied.status_code == 404
    allowed = api.request("POST", f"/concessions/{first}/revoke", cookie=manager_session)
    assert allowed.status_code == 200, allowed.text
    # Un mando de línea no tiene ``concessions.read``.
    line_manager, _ = _member(api, client, Role.LINE_MANAGER, ScopeLevel.ZONE, client.zone_id())
    response = api.request("GET", "/concessions", cookie=_session(api, client, line_manager))
    assert response.status_code == 404 and _code(response) == "not_found"


# --- Usuarios ------------------------------------------------------------------------------------


def test_list_users_pages_and_limits(api: Api) -> None:
    client = _client(api)
    for _ in range(2):
        _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    other = _client(api)
    everyone = api.request("GET", "/users", cookie=client.admin)
    assert everyone.status_code == 200
    assert everyone.headers["cache-control"] == "no-store"
    users = everyone.json()["users"]
    assert len(users) == 3 and everyone.json()["next_after"] is None
    assert str(other.admin_id) not in {user["user_id"] for user in users}
    assert all("password" not in json.dumps(user) for user in users)
    seen: list[str] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 1}
        if after is not None:
            params["after"] = after
        page = api.request("GET", "/users", cookie=client.admin, params=params).json()
        seen += [user["user_id"] for user in page["users"]]
        after = page["next_after"]
        if after is None:
            break
    assert seen == sorted(user["user_id"] for user in users)
    for limit in (0, 201, "x"):
        response = api.request("GET", "/users", cookie=client.admin, params={"limit": limit})
        assert response.status_code == 400
    assert api.request("GET", "/users", cookie=client.admin, params={"limit": 200}).status_code == (
        200
    )


def test_invite_deactivate_reactivate_and_profile(api: Api) -> None:
    client = _client(api)
    email = new_email()
    invite = {
        "email": email,
        "display_name": "Persona sintética",
        "assignments": [_assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, client.plant_id)],
        "disclose_link": True,
    }
    created = api.request("POST", "/users", cookie=client.admin, json=invite)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["delivery"] == "link_disclosed" and body["link"].startswith("https://")
    user_id = body["user_id"]
    # El mismo correo, ahora o en otra organización: conflicto sin decir dónde.
    other = _client(api)
    for session in (client.admin, other.admin):
        repeated = api.request(
            "POST",
            "/users",
            cookie=session,
            json={
                **invite,
                "email": email.upper(),
                "assignments": [
                    _assignment(
                        Role.COPASST,
                        ScopeLevel.ORGANIZATION,
                        client.organization_id
                        if session is client.admin
                        else other.organization_id,
                    )
                ],
            },
        )
        assert repeated.status_code == 409 and _code(repeated) == "conflict"
    # Sin asignaciones, o con incompatibles entre sí: rechazo de forma.
    assert (
        api.request("POST", "/users", cookie=client.admin, json={**invite, "assignments": []})
    ).status_code == 400
    incompatible = api.request(
        "POST",
        "/users",
        cookie=client.admin,
        json={
            **invite,
            "email": new_email(),
            "assignments": [
                _assignment(Role.COORDINATOR_SST, ScopeLevel.PLANT, client.plant_id),
                _assignment(Role.LINE_MANAGER, ScopeLevel.ZONE, client.zone_id()),
            ],
        },
    )
    assert incompatible.status_code == 409
    # Perfil: el nombre cambia y la auditoría guarda solo el nombre del campo.
    patched = api.request(
        "PATCH", f"/users/{user_id}", cookie=client.admin, json={"display_name": "Otro nombre"}
    )
    assert patched.status_code == 204
    rows = _audit(api, client.organization_id, "user_profile_changed")
    assert rows and "Otro nombre" not in rows[-1]["filters"]
    assert json.loads(rows[-1]["filters"]) == {"fields": ["display_name"]}
    assert (
        api.request(
            "PATCH", f"/users/{user_id}", cookie=client.admin, json={"unknown": True}
        ).status_code
        == 400
    )
    # Desactivar, otra vez y reactivar.
    deactivated = api.request("POST", f"/users/{user_id}/deactivate", cookie=client.admin)
    assert deactivated.status_code == 200 and deactivated.json()["status"] == "deactivated"
    again = api.request("POST", f"/users/{user_id}/deactivate", cookie=client.admin)
    assert again.status_code == 409
    reactivated = api.request(
        "POST",
        f"/users/{user_id}/reactivate",
        cookie=client.admin,
        json={
            "assignments": [_assignment(Role.COPASST, ScopeLevel.ZONE, client.zone_id())],
            "disclose_link": True,
        },
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["user_id"] == user_id
    # El último administrador no se desactiva.
    last = api.request("POST", f"/users/{client.admin_id}/deactivate", cookie=client.admin)
    assert last.status_code == 409 and _code(last) == "conflict"
    # Un usuario de otra organización: como inexistente.
    foreign = api.request("POST", f"/users/{other.admin_id}/deactivate", cookie=client.admin)
    assert foreign.status_code == 404


def test_second_factor_reset_closes_sessions(api: Api) -> None:
    client = _client(api)
    user, _ = _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    target = _session(api, client, user)
    assert api.request("GET", "/me", cookie=target).status_code == 200
    reset = api.request("POST", f"/users/{user}/second-factor/reset", cookie=client.admin)
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"user_id": str(user), "sessions_closed": 1}
    assert api.request("GET", "/me", cookie=target).status_code == 401
    (row,) = _audit(api, client.organization_id, "second_factor_reset")[-1:]
    assert row["resource_id"] == user
    other = _client(api)
    foreign = api.request(
        "POST", f"/users/{other.admin_id}/second-factor/reset", cookie=client.admin
    )
    assert foreign.status_code == 404 and _code(foreign) == "not_found"
    # Un coordinador no tiene ``users.manage``.
    coordinator, _ = _member(api, client, Role.COORDINATOR_SST)
    denied = api.request(
        "POST", f"/users/{user}/second-factor/reset", cookie=_session(api, client, coordinator)
    )
    assert denied.status_code == 404


# --- Jerarquía -----------------------------------------------------------------------------------


def test_create_plant_and_zone(api: Api) -> None:
    client = _client(api)
    plant = {
        "code": new_code("PL"),
        "name": "Planta nueva sintética",
        "country": "CO",
        "data_region": "us-east-1",
        "timezone": "America/Bogota",
    }
    created = api.request("POST", "/plants", cookie=client.admin, json=plant)
    assert created.status_code == 201, created.text
    plant_id = created.json()["plant_id"]
    assert created.json()["zones"] == []
    assert api.request("POST", "/plants", cookie=client.admin, json=plant).status_code == 409
    for change in ({"country": "co"}, {"timezone": "america bogota"}, {"data_region": "nowhere-1"}):
        bad = api.request(
            "POST", "/plants", cookie=client.admin, json={**plant, "code": new_code("PL"), **change}
        )
        assert bad.status_code == 400, change
    zone = {"code": new_code("ZN"), "name": "Zona nueva sintética"}
    made = api.request("POST", f"/plants/{plant_id}/zones", cookie=client.admin, json=zone)
    assert made.status_code == 201, made.text
    assert made.json()["plant_id"] == plant_id and made.json()["node_id"] is None
    repeated = api.request("POST", f"/plants/{plant_id}/zones", cookie=client.admin, json=zone)
    assert repeated.status_code == 409
    other = _client(api)
    foreign = api.request("POST", f"/plants/{other.plant_id}/zones", cookie=client.admin, json=zone)
    assert foreign.status_code == 404 and _code(foreign) == "not_found"
    # La génesis de la cadena de la planta y la zona en el expediente.
    records = api.fetch(
        "SELECT record_type FROM ledger.ledger_record WHERE organization_id = $1"
        " AND plant_id = $2 ORDER BY chain_sequence",
        client.organization_id,
        uuid.UUID(plant_id),
    )
    assert [r["record_type"] for r in records] == ["plant_created", "zone_created"]
    # Un coordinador no tiene ``hierarchy.manage``.
    coordinator, _ = _member(api, client, Role.COORDINATOR_SST)
    denied = api.request(
        "POST",
        "/plants",
        cookie=_session(api, client, coordinator),
        json={**plant, "code": new_code("PL")},
    )
    assert denied.status_code == 404 and _code(denied) == "not_found"


def test_hierarchy_shows_only_the_session_scope(api: Api) -> None:
    client = _client(api)
    zone = client.zone_id(0, 1)
    line_manager, _ = _member(api, client, Role.LINE_MANAGER, ScopeLevel.ZONE, zone)
    view = api.request("GET", "/hierarchy", cookie=_session(api, client, line_manager))
    assert view.status_code == 200, view.text
    (plant,) = view.json()["plants"]
    assert plant["plant_id"] == str(client.plant(0))
    assert [z["zone_id"] for z in plant["zones"]] == [str(zone)]
    whole = api.request("GET", "/hierarchy", cookie=client.admin).json()
    assert {p["plant_id"] for p in whole["plants"]} == {str(p) for p in client.site.plants}
    assert sum(len(p["zones"]) for p in whole["plants"]) == 4
    assert api.request("GET", "/hierarchy").status_code == 401


# --- Organización --------------------------------------------------------------------------------


def test_organization_settings(api: Api) -> None:
    client = _client(api)
    current = api.request("GET", "/organization/settings", cookie=client.admin)
    assert current.status_code == 200, current.text
    assert current.json()["concession_max_days"] == 30
    assert current.json()["concession_default_days"] == 7
    assert current.json()["organization_id"] == str(client.organization_id)
    for body in (
        {"concession_max_days": 0},
        {"concession_max_days": 91},
        {"concession_default_days": 31},  # mayor que el tope vigente (30)
        {"concession_max_days": 5},  # menor que la omisión vigente (7)
        {"concession_max_days": "30"},
        {"code": "OTRO"},
    ):
        response = api.request("PATCH", "/organization/settings", cookie=client.admin, json=body)
        assert response.status_code == 400, body
    unchanged = api.request("GET", "/organization/settings", cookie=client.admin).json()
    assert unchanged == current.json()
    changed = api.request(
        "PATCH",
        "/organization/settings",
        cookie=client.admin,
        json={"concession_max_days": 90, "concession_default_days": 90, "name": "Nombre nuevo"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["concession_max_days"] == 90
    assert changed.json()["name"] == "Nombre nuevo"
    rows = _audit(api, client.organization_id, "organization_settings_changed")
    assert len(rows) == 1 and "Nombre nuevo" not in rows[0]["filters"]
    assert json.loads(rows[0]["filters"])["fields"] == [
        "concession_default_days",
        "concession_max_days",
        "name",
    ]
    # Un gerente de planta (nivel planta) no alcanza la organización; uno de organización, sí.
    plant_manager, _ = _member(api, client, Role.PLANT_MANAGER, ScopeLevel.PLANT, client.plant_id)
    denied = api.request(
        "GET", "/organization/settings", cookie=_session(api, client, plant_manager)
    )
    assert denied.status_code == 404
    org_manager, _ = _member(api, client, Role.PLANT_MANAGER)
    allowed = api.request(
        "GET", "/organization/settings", cookie=_session(api, client, org_manager)
    )
    assert allowed.status_code == 200


def test_every_new_route_requires_its_permission_key(api: Api) -> None:
    wanted = {
        ("GET", "/users"): PermissionKey.USERS_MANAGE,
        ("POST", "/users"): PermissionKey.USERS_MANAGE,
        ("POST", "/users/{user_id}/deactivate"): PermissionKey.USERS_MANAGE,
        ("POST", "/users/{user_id}/reactivate"): PermissionKey.USERS_MANAGE,
        ("PATCH", "/users/{user_id}"): PermissionKey.USERS_MANAGE,
        ("POST", "/users/{user_id}/second-factor/reset"): PermissionKey.USERS_MANAGE,
        ("POST", "/users/{user_id}/roles"): PermissionKey.ROLES_MANAGE,
        ("DELETE", "/users/{user_id}/roles/{assignment_id}"): PermissionKey.ROLES_MANAGE,
        ("GET", "/hierarchy"): PermissionKey.HIERARCHY_READ,
        ("POST", "/plants"): PermissionKey.HIERARCHY_MANAGE,
        ("POST", "/plants/{plant_id}/zones"): PermissionKey.HIERARCHY_MANAGE,
        ("GET", "/organization/settings"): PermissionKey.ORGANIZATION_SETTINGS,
        ("PATCH", "/organization/settings"): PermissionKey.ORGANIZATION_SETTINGS,
        ("GET", "/concessions"): PermissionKey.CONCESSIONS_READ,
        ("GET", "/concessions/{concession_id}/queries"): PermissionKey.CONCESSIONS_READ,
        ("POST", "/concessions/{concession_id}/revoke"): PermissionKey.CONCESSIONS_REVOKE,
        ("POST", "/provider/concessions"): PermissionKey.CONCESSIONS_GRANT,
        ("GET", "/provider/concessions"): PermissionKey.CONCESSIONS_GRANT,
    }
    found = {
        (method, route.path): route.declarations[0].permission
        for route in iter_declared_routes(api.app.routes)
        for method in route.methods
        if (method, route.path) in wanted
    }
    assert found == {key: value.value for key, value in wanted.items()}
    # Sin sesión, toda ruta nueva responde ``unauthenticated``.
    for method, path in wanted:
        concrete = path.format(
            user_id=uuid.uuid4(),
            assignment_id=uuid.uuid4(),
            plant_id=uuid.uuid4(),
            concession_id=uuid.uuid4(),
        )
        response = api.request(method, concrete, json={})
        assert response.status_code == 401, (method, path, response.text)


# --- Alcance de las guardas propias --------------------------------------------------------------


def test_a_plant_level_administrator_does_not_reach_organization_operations(api: Api) -> None:
    client = _client(api)
    plant_admin, _ = _member(api, client, Role.ADMINISTRATOR, ScopeLevel.PLANT, client.plant_id)
    target, _ = _member(api, client, Role.COPASST, ScopeLevel.ZONE, client.zone_id())
    session = _session(api, client, plant_admin)
    # La ruta deja pasar (tiene la clave en algún alcance); el servicio la exige sobre la
    # organización: como inexistente.
    listed = api.request("GET", "/users", cookie=session)
    assert listed.status_code == 404 and _code(listed) == "not_found"
    reset = api.request("POST", f"/users/{target}/second-factor/reset", cookie=session)
    assert reset.status_code == 404 and _code(reset) == "not_found"


def test_each_provider_user_lists_only_their_own_concessions(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    first = authz.add_provider_user()
    second = authz.add_provider_user()
    mine = authz.add_concession(client.organization_id, first)
    theirs = authz.add_concession(client.organization_id, second)
    for user, expected in ((first, mine), (second, theirs)):
        session = authz.open_session(authz.provider_organization_id, user)
        listed = api.request("GET", "/provider/concessions", cookie=session)
        assert listed.status_code == 200, listed.text
        assert [item["concession_id"] for item in listed.json()["concessions"]] == [str(expected)]


def test_provider_concessions_of_answers_only_the_providers_own_session(api: Api) -> None:
    """``identity.provider_concessions_of`` (``nuc_0014``) desde otros contextos: nada."""
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    concession_id = authz.add_concession(client.organization_id, installer)
    statement = text(
        "SELECT concession_id FROM identity.provider_concessions_of(CAST(:grantee AS uuid))"
    )

    def listed(context: Any) -> list[uuid.UUID]:
        rows = api.env.run(
            authz.sessions.database.read(context, statement, {"grantee": str(installer)})
        )
        return [uuid.UUID(str(row.concession_id)) for row in rows]

    provider = api.env.session_context(authz.provider_organization_id, installer)
    assert listed(provider) == [concession_id]
    # El cliente de la concesión (u otro) no lee las concesiones del proveedor.
    assert listed(api.env.session_context(client.organization_id, client.admin_id)) == []
    # Ni el propio proveedor bajo la concesión (contexto de proveedor, BR-NUC-04).
    cookie = authz.open_session(authz.provider_organization_id, installer)
    under = api.env.run(authz.contexts.context_from_session(cookie, concession_id=concession_id))
    assert listed(under.context) == []


def test_an_expired_concession_is_shown_expired_before_the_task_runs(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    expired = authz.add_concession(
        client.organization_id,
        installer,
        granted_at=authz.now() - timedelta(days=8),
        duration=timedelta(days=7),
    )
    (row,) = api.fetch(
        "SELECT status FROM identity.provider_concession WHERE concession_id = $1", expired
    )
    assert row["status"] == "active"  # la tarea expire_concessions no ha corrido
    panel = api.request("GET", "/concessions", cookie=client.admin).json()["concessions"]
    assert [(item["concession_id"], item["status"]) for item in panel] == [
        (str(expired), "expired")
    ]
    session = authz.open_session(authz.provider_organization_id, installer)
    own = api.request("GET", "/provider/concessions", cookie=session).json()["concessions"]
    assert own[0]["status"] == "expired"
    # Revocar lo vencido: conflicto (BR-NUC-40).
    late = api.request("POST", f"/concessions/{expired}/revoke", cookie=client.admin)
    assert late.status_code == 409


def test_a_platform_operator_revokes_from_the_provider(api: Api) -> None:
    authz = api.env.authz
    client = _client(api)
    installer = authz.add_provider_user()
    concession_id = authz.add_concession(client.organization_id, installer)
    operator = authz.open_session(authz.provider_organization_id, authz.operator_id)
    revoked = api.request("POST", f"/concessions/{concession_id}/revoke", cookie=operator)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_by_side"] == "provider"
    provider = authz.open_session(authz.provider_organization_id, installer)
    after = api.request("GET", "/hierarchy", cookie=provider, concession=concession_id)
    assert after.status_code == 404
