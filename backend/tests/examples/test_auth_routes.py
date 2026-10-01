"""Rutas de sesión, invitación y ``GET /me`` de extremo a extremo (TASK-135; §10.2; adenda A-15).

La aplicación real (``create_app`` con la cadena fija de middleware y las rutas de
``platform_units()``) contra PostgreSQL 16 real como ``vigia_app``: inicio de sesión, segundo
factor real (TOTP con cifrado de sobre sobre un KMS en memoria), sesiones, invitaciones, aviso de
tratamiento y ``GET /me``. Las peticiones van por ``httpx.ASGITransport`` en el mismo bucle de la
base. Dobles solo donde el módulo real tiene sus propias propiedades: el hash de contraseñas
(``fake$`` más la contraseña, sin Argon2id de 64 MB) y, en la activación, el segundo factor y la
política (``hierarchy_support``).

- **H-58 / BR-NUC-22**: un administrador sin segundo factor inscrito, tras la contraseña, no obtiene
  ninguna respuesta útil fuera de la inscripción: toda ruta con sesión responde
  ``unauthenticated``, el segundo paso ``second_factor_required``; la inscripción (QR, primer
  TOTP) completa el inicio de sesión y entonces ``GET /me`` responde.
- **Dos pasos**: con el segundo factor inscrito, la contraseña deja la sesión pendiente y solo un
  TOTP válido la completa.
- **BR-NUC-23**: correo desconocido, contraseña incorrecta y cuenta desactivada responden el mismo
  cuerpo; el código incorrecto, también ``unauthenticated``.
- **BR-NUC-25**: cookie ``__Host-`` con ``Secure; HttpOnly; SameSite=Strict; Path=/`` y sin
  ``Max-Age``; ``Cache-Control: no-store``.
- **Invitación** usada, vencida, cancelada, inexistente o mal formada: la misma respuesta
  ``not_found``.
- **``GET /me``** (nº 5 y 7): vencimientos, versión del aviso y ``api_version`` (campo y cabecera
  ``X-Vigia-Api-Version``), permisos efectivos de la matriz y nada sensible (ni el identificador de
  sesión, ni su hash, ni el de la contraseña).
- **Aviso** (seguimiento de VIG-75): con la versión por defecto de ``AppRuntime``, sesión nueva →
  ``privacy_notice_required`` → aceptación → la ruta responde.
- **BR-NUC-26 y 27**: sesiones propias sin identificadores, cerrar las demás, cambio de contraseña
  que revoca las demás y cierre de sesión que invalida en el servidor.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest

from tests.api_support import World
from tests.hierarchy_support import (
    GOOD_PASSWORD,
    REJECTED_PASSWORD,
    HierarchyEnvironment,
    hierarchy_environment,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.second_factor_support import FakeKms
from tests.session_support import ORIGIN_KEY
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.http.me import API_VERSION_HEADER
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.hierarchy import GenesisRequest, PlantSpec
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import AssignmentRequest
from vigia_platform.identity.application.users import InviteRequest
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.passwords import (
    BreachSource,
    PasswordHash,
    PolicyResult,
    PolicyViolation,
    VerifyResult,
)
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import (
    SESSION_COOKIE_NAME,
    SessionCookie,
    SessionService,
)
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher

pytestmark = pytest.mark.integration

STATIC: Final = Path(__file__).resolve().parents[1] / "fixtures" / "static"
APP_VERSION: Final = "v0.1.0-test"
"""La ``app_version`` del ``version.json`` de ``STATIC``."""
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
NEW_PASSWORD: Final = "otra-clave-sintetica-larga"  # noqa: S105 - dato sintético de prueba
SESSION_ROUTES: Final = (
    ("GET", "/me", None),
    ("GET", "/auth/sessions", None),
    ("POST", "/auth/sessions/close-others", None),
    ("POST", "/auth/password", {"current_password": GOOD_PASSWORD, "new_password": NEW_PASSWORD}),
    ("POST", "/privacy-notice/accept", {"notice_version": CURRENT_PRIVACY_NOTICE_VERSION}),
)
"""Las rutas de ``SessionRoute`` con un cuerpo válido."""


class Passwords:
    """``PasswordService`` determinista: hash ``fake$`` (el de ``hierarchy_support``) y política
    que rechaza ``REJECTED_PASSWORD`` y lo de menos de 8 caracteres."""

    async def verify(self, password: str, encoded: str) -> VerifyResult:
        return VerifyResult(ok=encoded == f"fake${password}", needs_rehash=False)

    async def hash(self, password: str) -> PasswordHash:
        return PasswordHash(encoded=f"fake${password}", algorithm_version=1)

    async def check_policy(self, password: str, email: str) -> PolicyResult:
        if password == REJECTED_PASSWORD or len(password) < 8:
            return PolicyResult((PolicyViolation.BREACHED,), BreachSource.LOCAL_LIST)
        return PolicyResult((), BreachSource.REMOTE)


@dataclass
class Api:
    env: HierarchyEnvironment
    client: httpx.AsyncClient

    def request(
        self,
        method: str,
        path: str,
        *,
        cookie: SessionCookie | None = None,
        json: Any = None,
    ) -> httpx.Response:
        headers = dict(SAME_ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        response: httpx.Response = self.env.run(
            self.client.request(method, path, headers=headers, json=json)
        )
        return response

    def login(self, email: str, password: str) -> tuple[httpx.Response, SessionCookie | None]:
        response = self.request("POST", "/auth/login", json={"email": email, "password": password})
        return response, cookie_of(response)

    @property
    def clock(self) -> Any:
        return self.env.authz.sessions.clock


def cookie_of(response: httpx.Response) -> SessionCookie | None:
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name == SESSION_COOKIE_NAME:
            return SessionCookie.parse(rest.split(";", 1)[0])
    return None


def _keys(value: Any) -> set[str]:
    """Todas las claves de un JSON, a cualquier profundidad."""
    if isinstance(value, dict):
        return set(value) | {k for item in value.values() for k in _keys(item)}
    if isinstance(value, list):
        return {k for item in value for k in _keys(item)}
    return set()


def _body_without_correlation(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    body.pop("correlation_id")
    return body


@pytest.fixture(scope="module")
def api(postgres_endpoint: PostgresEndpoint) -> Iterator[Api]:
    with hierarchy_environment(postgres_endpoint, "auth_routes") as env:
        authz = env.authz
        sessions = authz.sessions
        pool = CpuPool(sessions.clock, max_workers=2)
        store = PostgresSessionStore(sessions.database, sessions.audit, sessions.outbox)
        passwords = Passwords()
        second_factor = SecondFactorService(
            PostgresSecondFactorStore(sessions.database, sessions.audit),
            EnvelopeCipher(FakeKms(), "alias/vigia-secrets", sessions.clock),
            pool,
            sessions.clock,
        )
        provider = authz.provider_organization_id
        identity = IdentityHttp(
            login=LoginService(
                store=store,
                sessions=store,
                passwords=passwords,
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
                passwords=passwords,
                throttle=store,
                contexts=authz.contexts,
                clock=sessions.clock,
            ),
            me=MeService(
                sessions.database, contexts=authz.contexts, provider_organization_id=provider
            ),
            provider_organization_id=provider,
        )
        # La versión del aviso no se pasa: se prueba el valor por defecto de ``AppRuntime``.
        app = World(clock=sessions.clock).app(
            units=None,
            permissions=frozenset(key.value for key in PermissionKey),
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit, provider_organization_id=provider
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
            yield Api(env, client)
        finally:
            env.run(client.aclose())
            pool.shutdown()


# --- Datos --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    password: str


def _person(api: Api, *, notice: str | None = CURRENT_PRIVACY_NOTICE_VERSION) -> Person:
    """Una persona activa de una organización cliente, coordinadora SST de toda la organización."""
    authz = api.env.authz
    site = authz.add_site(plants=1, zones_per_plant=1)
    user = authz.sessions.add_user(site.organization_id, privacy_notice=notice)
    authz.assign(site.organization_id, user.user_id, Role.COORDINATOR_SST)
    return Person(user.user_id, user.organization_id, user.email, user.password)


@dataclass(frozen=True)
class Organization:
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    admin_id: uuid.UUID
    admin_email: str


def _organization(api: Api) -> Organization:
    """Organización de la génesis con su administrador activado **sin** inscribir el segundo
    factor en la base (el doble de la activación no lo guarda): el caso de H-58."""
    env = api.env
    email = new_email("admin")
    result = env.run(
        env.genesis().create_client_organization(
            env.operator_context(),
            GenesisRequest(
                code=new_code("ORG"),
                name="Organización sintética",
                plant=PlantSpec(
                    new_code("PL"), "Planta sintética", "CO", "us-east-1", "America/Bogota"
                ),
                administrator_email=email,
                administrator_display_name="Administración sintética",
            ),
        )
    )
    link = result.invitation.link
    assert link is not None
    response = api.request(
        "POST",
        f"/invitations/{link.split('#', 1)[1]}/accept",
        json={
            "step": "complete",
            "password": GOOD_PASSWORD,
            "notice_version": CURRENT_PRIVACY_NOTICE_VERSION,
            "second_factor_code": "246810",
        },
    )
    assert response.status_code == 200, response.text
    return Organization(
        result.organization_id, result.plant_id, result.administrator_user_id, email
    )


def _admin_context(api: Api, org: Organization) -> ScopeContext:
    return api.env.session_context(org.organization_id, org.admin_id)


def _invitation_token(api: Api, org: Organization) -> str:
    outcome = api.env.run(
        api.env.users().invite_user(
            _admin_context(api, org),
            InviteRequest(
                email=new_email(),
                display_name="Persona sintética",
                assignments=(
                    AssignmentRequest(Role.COORDINATOR_SST, ScopeLevel.PLANT, org.plant_id),
                ),
                disclose_link=True,
            ),
        )
    )
    assert outcome.link is not None
    token: str = outcome.link.split("#", 1)[1]
    return token


def _totp(api: Api, provisioning_uri: str) -> pyotp.TOTP:
    secret = parse_qs(urlparse(provisioning_uri).query)["secret"][0]
    return pyotp.TOTP(secret)


# --- H-58: el administrador sin segundo factor solo puede inscribirse ---------------------------


def test_h58_an_administrator_without_second_factor_gets_nothing_but_enrollment(api: Api) -> None:
    org = _organization(api)
    response, pending = api.login(org.admin_email, GOOD_PASSWORD)
    assert response.status_code == 200
    assert response.json() == {"status": "second_factor_enrollment_required"}
    assert pending is not None
    # Ninguna ruta con sesión responde con la sesión pendiente.
    for method, path, body in SESSION_ROUTES:
        denied = api.request(method, path, cookie=pending, json=body)
        assert denied.status_code == 401, (path, denied.text)
        assert denied.json()["code"] == "unauthenticated"
    # El segundo paso tampoco: primero hay que inscribirse.
    second = api.request("POST", "/auth/second-factor", cookie=pending, json={"code": "123456"})
    assert second.status_code == 401 and second.json()["code"] == "second_factor_required"
    # La inscripción sí: QR, URI y diez códigos de recuperación, una vez y sin caché.
    started = api.request("POST", "/auth/second-factor/enroll", cookie=pending, json={})
    assert started.status_code == 200, started.text
    assert started.headers["cache-control"] == "no-store"
    enrollment = started.json()["enrollment"]
    assert started.json()["status"] == "enrollment_started"
    assert enrollment["qr_svg"].startswith("<?xml") or "<svg" in enrollment["qr_svg"]
    assert len(enrollment["recovery_codes"]) == 10
    totp = _totp(api, enrollment["provisioning_uri"])
    wrong = api.request(
        "POST", "/auth/second-factor/enroll", cookie=pending, json={"code": "000000"}
    )
    assert wrong.status_code == 401 and wrong.json()["code"] == "unauthenticated"
    confirmed = api.request(
        "POST",
        "/auth/second-factor/enroll",
        cookie=pending,
        json={"code": totp.at(api.clock.now())},
    )
    assert confirmed.status_code == 200 and confirmed.json() == {"status": "authenticated"}
    # La misma sesión ya sirve.
    me = api.request("GET", "/me", cookie=pending)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["user_id"] == str(org.admin_id)


def test_two_step_login_with_an_enrolled_second_factor(api: Api) -> None:
    org = _organization(api)
    _, pending = api.login(org.admin_email, GOOD_PASSWORD)
    assert pending is not None
    started = api.request("POST", "/auth/second-factor/enroll", cookie=pending, json={})
    totp = _totp(api, started.json()["enrollment"]["provisioning_uri"])
    api.request(
        "POST",
        "/auth/second-factor/enroll",
        cookie=pending,
        json={"code": totp.at(api.clock.now())},
    )
    # Inscrito: la contraseña deja la sesión pendiente del segundo paso.
    api.env.advance(31)
    response, second_pending = api.login(org.admin_email, GOOD_PASSWORD)
    assert response.json() == {"status": "second_factor_required"}
    assert second_pending is not None and second_pending != pending
    assert api.request("GET", "/me", cookie=second_pending).status_code == 401
    # Inscribirse otra vez no está al alcance de la sesión pendiente.
    again = api.request("POST", "/auth/second-factor/enroll", cookie=second_pending, json={})
    assert again.status_code == 401 and again.json()["code"] == "unauthenticated"
    wrong = api.request(
        "POST", "/auth/second-factor", cookie=second_pending, json={"code": "000000"}
    )
    assert wrong.status_code == 401 and wrong.json()["code"] == "unauthenticated"
    good = api.request(
        "POST",
        "/auth/second-factor",
        cookie=second_pending,
        json={"code": totp.at(api.clock.now())},
    )
    assert good.status_code == 200 and good.json() == {"status": "authenticated"}
    assert api.request("GET", "/me", cookie=second_pending).status_code == 200


# --- Inicio de sesión: cookie y mensajes uniformes -----------------------------------------------


def test_login_sets_the_host_cookie_without_max_age(api: Api) -> None:
    person = _person(api)
    response, cookie = api.login(person.email, person.password)
    assert response.status_code == 200 and response.json() == {"status": "authenticated"}
    assert cookie is not None
    (header,) = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE_NAME)
    ]
    attributes = {part.strip() for part in header.split(";")[1:]}
    assert attributes == {"Secure", "HttpOnly", "SameSite=Strict", "Path=/"}
    assert response.headers["cache-control"] == "no-store"


def test_login_failures_are_indistinguishable(api: Api) -> None:
    person = _person(api)
    deactivated = _person(api)
    api.env.authz.set_user_status(deactivated.user_id, "deactivated")
    bodies = []
    for email, password in (
        (new_email("nadie"), person.password),
        (person.email, "clave-equivocada-1"),
        (deactivated.email, deactivated.password),
        ("no-es-un-correo", person.password),
    ):
        response, cookie = api.login(email, password)
        assert response.status_code == 401 and cookie is None
        bodies.append(_body_without_correlation(response))
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["code"] == "unauthenticated"
    for body in bodies:
        assert person.email not in str(body) and person.password not in str(body)


# --- GET /me (pendientes nº 5 y 7) ---------------------------------------------------------------


def test_me_exposes_the_additive_fields_and_nothing_sensitive(api: Api) -> None:
    person = _person(api)
    login_at = api.clock.now()
    _, cookie = api.login(person.email, person.password)
    assert cookie is not None
    api.env.advance(60)
    response = api.request("GET", "/me", cookie=cookie)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "user",
        "organization",
        "assignments",
        "effective_permissions",
        "concession_id",
        "idle_expires_at",
        "absolute_expires_at",
        "privacy_notice_version",
        "api_version",
    }
    assert body["user"] == {
        "user_id": str(person.user_id),
        "display_name": "Persona sintética",
        "email": person.email,
    }
    assert body["organization"]["organization_id"] == str(person.organization_id)
    assert body["organization"]["kind"] == "client"
    assert body["assignments"] == [
        {
            "role": "coordinator_sst",
            "scope_level": "organization",
            "scope_id": str(person.organization_id),
        }
    ]
    assert body["effective_permissions"] == sorted(k.value for k in MATRIX[Role.COORDINATOR_SST])
    assert body["concession_id"] is None
    # nº 5: vencimientos (el de inactividad, prolongado por esta petición) y versión del aviso.
    idle = login_at + timedelta(seconds=60) + timedelta(minutes=30)
    absolute = login_at + timedelta(hours=12)
    assert body["idle_expires_at"] == idle.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert body["absolute_expires_at"] == absolute.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    assert body["privacy_notice_version"] == CURRENT_PRIVACY_NOTICE_VERSION
    # nº 7: versión de la release, en el campo y en la cabecera equivalente.
    assert body["api_version"] == APP_VERSION
    assert response.headers[API_VERSION_HEADER] == APP_VERSION
    assert response.headers["cache-control"] == "no-store"
    # Nada sensible: ni el identificador de sesión, ni su hash, ni el de la contraseña.
    text = response.text
    for secret in (cookie.token, cookie.value, cookie.session_id_hash, "fake$", person.password):
        assert secret not in text
    assert not {
        key for key in _keys(body) if "session" in key or "password" in key or "token" in key
    }


def test_me_requires_a_usable_session(api: Api) -> None:
    person = _person(api)
    assert api.request("GET", "/me").status_code == 401
    _, cookie = api.login(person.email, person.password)
    assert cookie is not None
    api.env.advance(timedelta(minutes=31).total_seconds())
    expired = api.request("GET", "/me", cookie=cookie)
    assert expired.status_code == 401 and expired.json()["code"] == "unauthenticated"


# --- Aviso de tratamiento (seguimiento de VIG-75) ----------------------------------------------


def test_new_session_requires_the_notice_then_accepting_it_serves_the_route(api: Api) -> None:
    person = _person(api, notice=None)
    response, cookie = api.login(person.email, person.password)
    assert response.status_code == 200 and cookie is not None
    required = api.request("GET", "/me", cookie=cookie)
    assert required.status_code == 403 and required.json()["code"] == "privacy_notice_required"
    other = api.request(
        "POST", "/privacy-notice/accept", cookie=cookie, json={"notice_version": "v9"}
    )
    assert other.status_code == 400 and other.json()["code"] == "invalid_request"
    accepted = api.request(
        "POST",
        "/privacy-notice/accept",
        cookie=cookie,
        json={"notice_version": CURRENT_PRIVACY_NOTICE_VERSION},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "notice_version": CURRENT_PRIVACY_NOTICE_VERSION,
        "newly_accepted": True,
    }
    served = api.request("GET", "/me", cookie=cookie)
    assert served.status_code == 200, served.text
    rows = api.env.fetch(
        "SELECT notice_version FROM identity.privacy_notice_acceptance WHERE user_id = $1",
        person.user_id,
    )
    assert [r["notice_version"] for r in rows] == [CURRENT_PRIVACY_NOTICE_VERSION]


# --- Invitación: usada, vencida o cancelada igual que inexistente ---------------------------------


def test_invitation_used_expired_or_unknown_respond_like_a_missing_one(api: Api) -> None:
    org = _organization(api)
    used = _invitation_token(api, org)
    begin = api.request("POST", f"/invitations/{used}/accept", json={"step": "begin"})
    assert begin.status_code == 200, begin.text
    started = begin.json()
    assert started["status"] == "pending" and started["second_factor_required"] is False
    assert started["notice"]["version"] == CURRENT_PRIVACY_NOTICE_VERSION
    assert started["notice"]["pending_legal_text"] is True
    complete = {
        "step": "complete",
        "password": GOOD_PASSWORD,
        "notice_version": CURRENT_PRIVACY_NOTICE_VERSION,
    }
    rejected = api.request(
        "POST", f"/invitations/{used}/accept", json={**complete, "password": REJECTED_PASSWORD}
    )
    assert rejected.status_code == 400 and rejected.json()["code"] == "invalid_request"
    activated = api.request("POST", f"/invitations/{used}/accept", json=complete)
    assert activated.status_code == 200 and activated.json() == {"status": "activated"}
    expired = _invitation_token(api, org)
    api.env.advance(timedelta(hours=72).total_seconds())
    bodies = []
    for token in (used, expired, "A" * 43, "corto", "a.b"):
        for body in ({"step": "begin"}, complete):
            response = api.request("POST", f"/invitations/{token}/accept", json=body)
            assert response.status_code == 404, (token, response.text)
            bodies.append(_body_without_correlation(response))
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["code"] == "not_found"


# --- Sesiones propias, cerrar las demás, contraseña y cierre ------------------------------------


def test_sessions_list_hides_identifiers_and_close_others_revokes_them(api: Api) -> None:
    person = _person(api)
    _, first = api.login(person.email, person.password)
    _, second = api.login(person.email, person.password)
    assert first is not None and second is not None
    listed = api.request("GET", "/auth/sessions", cookie=first)
    assert listed.status_code == 200, listed.text
    sessions = listed.json()["sessions"]
    assert len(sessions) == 2 and sorted(s["current"] for s in sessions) == [False, True]
    assert all(
        set(s) == {"created_at", "last_seen_at", "client_hint", "current", "usable"}
        for s in sessions
    )
    for secret in (first.token, second.token, first.session_id_hash, second.session_id_hash):
        assert secret not in listed.text
    closed = api.request("POST", "/auth/sessions/close-others", cookie=first)
    assert closed.status_code == 200 and closed.json() == {"sessions_closed": 1}
    assert api.request("GET", "/me", cookie=second).status_code == 401
    assert api.request("GET", "/me", cookie=first).status_code == 200


def test_password_change_revokes_the_other_sessions(api: Api) -> None:
    person = _person(api)
    _, current = api.login(person.email, person.password)
    _, other = api.login(person.email, person.password)
    assert current is not None and other is not None
    wrong = api.request(
        "POST",
        "/auth/password",
        cookie=current,
        json={"current_password": "no-es-la-actual", "new_password": NEW_PASSWORD},
    )
    assert wrong.status_code == 400 and wrong.json()["code"] == "invalid_request"
    weak = api.request(
        "POST",
        "/auth/password",
        cookie=current,
        json={"current_password": person.password, "new_password": REJECTED_PASSWORD},
    )
    assert weak.status_code == 400 and weak.json()["code"] == "invalid_request"
    changed = api.request(
        "POST",
        "/auth/password",
        cookie=current,
        json={"current_password": person.password, "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200 and changed.json() == {"sessions_closed": 1}
    assert api.request("GET", "/me", cookie=other).status_code == 401
    assert api.request("GET", "/me", cookie=current).status_code == 200
    assert api.login(person.email, person.password)[0].status_code == 401
    assert api.login(person.email, NEW_PASSWORD)[0].status_code == 200
    audit = api.env.fetch(
        "SELECT outcome FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'password_changed' ORDER BY chain_sequence",
        person.organization_id,
    )
    assert [r["outcome"] for r in audit] == ["denied", "success"]


def test_password_change_attempts_count_in_the_account_throttle(api: Api) -> None:
    # BR-NUC-24: una sesión robada no prueba contraseñas sin límite.
    person = _person(api)
    _, cookie = api.login(person.email, person.password)
    assert cookie is not None
    attempt = {"current_password": "no-es-la-actual", "new_password": NEW_PASSWORD}
    for _ in range(5):
        response = api.request("POST", "/auth/password", cookie=cookie, json=attempt)
        assert response.status_code == 400, response.text
    held = api.request(
        "POST",
        "/auth/password",
        cookie=cookie,
        json={"current_password": person.password, "new_password": NEW_PASSWORD},
    )
    assert held.status_code == 429 and held.json()["code"] == "throttled"
    assert held.headers["retry-after"] == "30"
    # El retardo es el de la cuenta: el inicio de sesión también queda retenido.
    assert api.login(person.email, person.password)[0].json()["code"] == "throttled"


def test_logout_invalidates_on_the_server_and_clears_the_cookie(api: Api) -> None:
    person = _person(api)
    _, cookie = api.login(person.email, person.password)
    assert cookie is not None
    response = api.request("POST", "/auth/logout", cookie=cookie)
    assert response.status_code == 204
    assert any(
        h.startswith(f"{SESSION_COOKIE_NAME}=;") and "Max-Age=0" in h
        for h in response.headers.get_list("set-cookie")
    )
    assert api.request("GET", "/me", cookie=cookie).status_code == 401
    # Sin cookie o con una ya cerrada, la misma respuesta.
    assert api.request("POST", "/auth/logout").status_code == 204
    assert api.request("POST", "/auth/logout", cookie=cookie).status_code == 204
