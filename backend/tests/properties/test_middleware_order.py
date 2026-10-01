"""Orden fijo de la cadena de middleware, clase de ruta, sesión, aviso y correlación (TASK-134).

- PAT-NUC-SEG-06 y pendiente nº 37: el orden es exactamente el del diseño; intercambiar dos
  eslabones **cualesquiera** (los 55 pares, incluido el de clase de ruta y la autorización) impide
  arrancar y, aunque alguien se salte la comprobación de arranque, ninguna petición pasa.
- Clase de ruta: ``/api/nodes/*`` usa el pool ``node``; ``/me``, el pool ``person``.
- Sesión y contexto (paso 6), aviso de tratamiento (paso 9, NFR-NUC-29) y autorización por ruta
  (paso 10, con la auditoría caída: seguimiento de VIG-73).
- PR-NUC-55: el ``correlation_id`` del contexto del manejador, de la auditoría y de la respuesta es
  el generado por la plataforma, nunca el que manda el cliente.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from starlette.websockets import WebSocketDisconnect

import vigia_platform.shared.api.app as app_module
from tests.middleware_support import (
    CLIENT_ORG,
    NOTICE,
    PROVIDER_ORG,
    SAME_ORIGIN,
    Harness,
    cookie_header,
)
from vigia_platform.identity.authz.context import ConcessionRow
from vigia_platform.shared.api.errors import ApiErrorCode, ApiStartupError
from vigia_platform.shared.api.middleware import (
    CHAIN,
    MIDDLEWARE_CHAIN,
    ChainStep,
    install_chain,
    installed_chain,
    verify_chain,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import RouteClass

DESIGN_ORDER = (
    "correlation",
    "errors",
    "security_headers",
    "route_class",
    "body_limit",
    "origin_rate_limit",
    "session",
    "session_rate_limit",
    "csrf",
    "privacy_notice",
    "authorization",
)
"""El orden de PAT-NUC-SEG-06 con el eslabón del nº 37, escrito aquí a mano (no desde el código)."""

PAIRS = list(itertools.combinations(range(len(CHAIN)), 2))


def _installed_after_swap(i: int, j: int) -> tuple[ChainStep, ...]:
    """Los middleware que quedan instalados si se intercambian los eslabones ``i`` y ``j``.

    La autorización es una dependencia de cada ruta: lo que quede detrás de ella en el orden
    alterado se ejecutaría después de las rutas, es decir, ya no forma parte de la cadena.
    """
    steps = list(CHAIN)
    steps[i], steps[j] = steps[j], steps[i]
    return tuple(steps[: steps.index(ChainStep.AUTHORIZATION)])


def _install_swapped(monkeypatch: pytest.MonkeyPatch, i: int, j: int) -> None:
    original = app_module.install_chain
    order = _installed_after_swap(i, j)

    def install_swapped(app: FastAPI, settings: Any, **_: Any) -> None:
        original(app, settings, order=order)

    monkeypatch.setattr(app_module, "install_chain", install_swapped)


def _code(response: Any) -> str:
    return str(response.json()["code"])


# --- Orden fijo ----------------------------------------------------------------------------------


def test_the_chain_is_exactly_the_design_order() -> None:
    assert tuple(step.value for step in CHAIN) == DESIGN_ORDER
    assert CHAIN[:-1] == MIDDLEWARE_CHAIN and CHAIN[-1] is ChainStep.AUTHORIZATION
    assert len(PAIRS) == 55


def test_the_factory_installs_the_fixed_order() -> None:
    app = Harness().app()
    assert installed_chain(app) == MIDDLEWARE_CHAIN
    assert verify_chain(app) == []


@pytest.mark.parametrize(("i", "j"), PAIRS)
def test_swapping_any_two_links_prevents_startup(
    i: int, j: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_swapped(monkeypatch, i, j)
    with pytest.raises(ApiStartupError) as raised:
        Harness().app()
    assert "la cadena de middleware no tiene el orden fijo de PAT-NUC-SEG-06" in str(raised.value)


@pytest.mark.parametrize(("i", "j"), PAIRS)
def test_a_swapped_chain_lets_no_request_through_even_without_the_startup_check(
    i: int, j: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_swapped(monkeypatch, i, j)
    monkeypatch.setattr(app_module, "verify_chain", lambda app: [])
    harness = Harness()
    cookie = harness.session("orden")
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        me = client.get("/me", headers=cookie_header(cookie))
        live = client.get("/health/live")
    # La traza de la petición no es la del diseño: la autorización de la ruta deniega.
    assert me.status_code == 500 and _code(me) == ApiErrorCode.INTERNAL_ERROR
    assert live.status_code == 500 and _code(live) == ApiErrorCode.INTERNAL_ERROR
    assert harness.observed.handled == []


def test_verify_chain_names_the_expected_and_the_installed_order() -> None:
    app = FastAPI()
    install_chain(app, settings=None, order=MIDDLEWARE_CHAIN[1:])  # type: ignore[arg-type]
    (problem,) = verify_chain(app)
    assert "correlation, errors" in problem and "«errors, security_headers" in problem
    assert verify_chain(FastAPI()) != []


def test_every_request_leaves_the_complete_trace() -> None:
    harness = Harness()
    cookie = harness.session("traza")
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=cookie_header(cookie))
    assert response.status_code == 200
    assert harness.observed.handled == ["me"]


def test_a_route_resolved_differently_by_the_chain_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Si la tabla de la cadena y el enrutador discreparan (la cadena cree que ``/me`` es pública y
    # se salta la sesión), la declaración de la ruta que se ejecuta deniega con internal_error.
    from vigia_platform.shared.api.middleware import RouteTable

    original = RouteTable.resolve

    def confused(self: RouteTable, scope: Any) -> Any:
        if scope.get("path") == "/me":
            return original(self, {**scope, "path": "/health/live", "method": "GET"})
        return original(self, scope)

    monkeypatch.setattr(RouteTable, "resolve", confused)
    harness = Harness()
    cookie = harness.session("confundida")
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        response = client.get("/me", headers=cookie_header(cookie))
    assert response.status_code == 500 and _code(response) == ApiErrorCode.INTERNAL_ERROR
    assert harness.observed.handled == []


def test_websockets_never_bypass_the_chain() -> None:
    with (
        TestClient(Harness().app()) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/me"),
    ):
        pass


# --- Clase de ruta (pendiente nº 37) -------------------------------------------------------------


def test_node_routes_use_the_node_pool_and_person_routes_the_person_pool() -> None:
    harness = Harness()
    cookie = harness.session("pool")
    with TestClient(harness.app()) as client:
        node = client.get("/api/nodes/x")
        person = client.get("/me", headers=cookie_header(cookie))
        heartbeat = client.post("/api/nodes/heartbeat", json={"sequence": 1})
    assert node.status_code == person.status_code == heartbeat.status_code == 200
    assert harness.observed.route_classes == [RouteClass.NODE, RouteClass.PERSON, RouteClass.NODE]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/nodes", RouteClass.NODE),
        ("/api/nodes/", RouteClass.NODE),
        ("/api/nodes/enrollment", RouteClass.NODE),
        ("/api/nodesx", RouteClass.PERSON),
        ("/api/node/x", RouteClass.PERSON),
        ("/me", RouteClass.PERSON),
        ("/", RouteClass.PERSON),
    ],
)
def test_route_class_boundaries(path: str, expected: RouteClass) -> None:
    from vigia_platform.shared.api.middleware import route_class_of

    assert route_class_of(path) is expected


def test_node_routes_skip_session_csrf_and_privacy_notice_by_construction() -> None:
    harness = Harness(privacy_notice_version=None)
    with TestClient(harness.app()) as client:
        # Sin cookie, sin Sec-Fetch-Site y sin aviso vigente: el nodo no pasa por esos eslabones.
        response = client.post("/api/nodes/heartbeat", json={"sequence": 7})
    assert response.status_code == 200 and response.json() == {"sequence": 7}
    assert harness.csrf_audit.total == 0 and harness.store.lookups == []


# --- Sesión y contexto (paso 6) ------------------------------------------------------------------


def test_a_valid_session_builds_the_context_of_its_organization() -> None:
    harness = Harness()
    cookie = harness.session("valida")
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=cookie_header(cookie))
    assert response.json() == {"organization_id": str(CLIENT_ORG)}


@pytest.mark.parametrize(
    "cookie",
    [
        None,
        "__Host-vigia_session=basura",
        "__Host-vigia_session=",
        f"__Host-vigia_session={uuid.uuid4()}.{'A' * 43}",
    ],
)
def test_without_a_usable_session_the_route_answers_unauthenticated(cookie: str | None) -> None:
    harness = Harness()
    headers = {} if cookie is None else {"Cookie": cookie}
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=headers)
    assert response.status_code == 401 and _code(response) == ApiErrorCode.UNAUTHENTICATED
    assert harness.observed.handled == []


def test_a_repeated_session_cookie_is_ambiguous_and_denied() -> None:
    harness = Harness()
    first = harness.session("una")
    second = harness.session("otra")
    header = f"__Host-vigia_session={first.value}; __Host-vigia_session={second.value}"
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers={"Cookie": header})
    assert _code(response) == ApiErrorCode.UNAUTHENTICATED
    assert harness.store.lookups == []


@pytest.mark.parametrize("value", ["no-es-uuid", str(uuid.uuid4()).upper(), "1" * 36])
def test_an_unreadable_concession_header_is_not_found(value: str) -> None:
    harness = Harness()
    cookie = harness.session("concesion")
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers={**cookie_header(cookie), "X-Vigia-Concession": value})
    assert response.status_code == 404 and _code(response) == ApiErrorCode.NOT_FOUND


def test_a_valid_concession_builds_the_client_context() -> None:
    harness = Harness()
    concession_id = uuid.UUID("0192f0c4-0000-7000-8000-00000000c001")
    from datetime import timedelta

    cookie = harness.session(
        "instalador",
        role=Role.PROVIDER_INSTALLER,
        organization_id=PROVIDER_ORG,
        concession=ConcessionRow(
            concession_id=concession_id,
            organization_id=CLIENT_ORG,
            scope_level=ScopeLevel.ORGANIZATION,
            scope_id=CLIENT_ORG,
            expires_at=harness.world.clock.now() + timedelta(days=1),
        ),
    )
    headers = {**cookie_header(cookie), "X-Vigia-Concession": str(concession_id)}
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=headers)
    assert response.json() == {"organization_id": str(CLIENT_ORG)}


def test_public_routes_never_touch_the_session_store() -> None:
    harness = Harness()
    cookie = harness.session("publica")
    with TestClient(harness.app()) as client:
        client.get("/health/live", headers=cookie_header(cookie))
    assert harness.store.lookups == []


# --- Aviso de tratamiento (paso 9) ---------------------------------------------------------------


@pytest.mark.parametrize("accepted", [None, "v0-anterior", NOTICE.upper(), NOTICE + " "])
def test_without_the_current_notice_only_the_acceptance_route_answers(accepted: str | None) -> None:
    harness = Harness()
    cookie = harness.session("aviso", accepted=accepted)
    headers = {**cookie_header(cookie), **SAME_ORIGIN}
    with TestClient(harness.app()) as client:
        me = client.get("/me", headers=headers)
        users = client.post(
            "/users",
            headers=headers,
            json={"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
        )
        accept = client.post("/privacy-notice/accept", headers=headers)
    assert me.status_code == users.status_code == 403
    assert _code(me) == _code(users) == ApiErrorCode.PRIVACY_NOTICE_REQUIRED
    assert accept.status_code == 200
    assert harness.observed.handled == ["accept_notice"]


def test_with_the_current_notice_every_route_answers() -> None:
    harness = Harness()
    cookie = harness.session("vigente", accepted=NOTICE)
    with TestClient(harness.app()) as client:
        assert client.get("/me", headers=cookie_header(cookie)).status_code == 200


def test_without_a_declared_current_version_the_notice_fails_closed() -> None:
    harness = Harness(privacy_notice_version=None)
    cookie = harness.session("sin-version", accepted=NOTICE)
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=cookie_header(cookie))
        login = client.post(
            "/auth/login", headers=SAME_ORIGIN, json={"email": "a@b.co", "password": "x" * 12}
        )
    assert _code(response) == ApiErrorCode.PRIVACY_NOTICE_REQUIRED
    assert login.status_code == 200


# --- Autorización por ruta (paso 10) -------------------------------------------------------------


def test_a_route_without_the_key_answers_not_found_and_is_audited() -> None:
    harness = Harness()
    cookie = harness.session("sin-clave", role=Role.COPASST)
    with TestClient(harness.app()) as client:
        response = client.post(
            "/users",
            headers={**cookie_header(cookie), **SAME_ORIGIN},
            json={"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
        )
    assert response.status_code == 404 and _code(response) == ApiErrorCode.NOT_FOUND
    ((context, key, resource),) = harness.authz_audit.denied
    assert key.value == "users.manage" and resource.organization_id == CLIENT_ORG
    assert context.organization_id == CLIENT_ORG
    assert harness.observed.handled == []


def test_a_denial_with_the_audit_down_is_still_not_found() -> None:
    # Seguimiento de VIG-73: ante una base caída, «sin permiso» y «no existe» responden igual.
    harness = Harness()
    harness.authz_audit.fail = True
    cookie = harness.session("auditoria-caida", role=Role.COPASST)
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        denied = client.post(
            "/users",
            headers={**cookie_header(cookie), **SAME_ORIGIN},
            json={"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
        )
        missing = client.get("/no-existe", headers=cookie_header(cookie))
    assert denied.status_code == missing.status_code == 404
    assert _code(denied) == _code(missing) == ApiErrorCode.NOT_FOUND


@pytest.mark.parametrize("role", [Role.PLATFORM_OPERATOR, Role.PROVIDER_INSTALLER])
def test_provider_only_roles_grant_nothing_in_a_client_organization(role: Role) -> None:
    # Un rol de la proveedora asignado (por error) en un cliente no concede rutas allí (BR-NUC-04).
    harness = Harness()
    cookie = harness.session(f"rol-{role.value}", role=role)
    with TestClient(harness.app()) as client:
        response = client.get("/me", headers=cookie_header(cookie))
    assert response.status_code == 404 and _code(response) == ApiErrorCode.NOT_FOUND


def test_platform_keys_are_denied_outside_the_provider() -> None:
    from tests.factories import make_context
    from vigia_platform.identity.authz.authorize import route_role

    context = make_context(organization_id=CLIENT_ORG)
    role = route_role(context, "platform.keys.rotate", provider_organization_id=PROVIDER_ORG)
    assert role is None


# --- PR-NUC-55 · correlation_id generado ---------------------------------------------------------

CLIENT_VALUES = st.one_of(
    st.uuids(version=4).map(str),
    st.uuids().map(lambda value: str(uuid.UUID(int=(value.int & ~(0xF << 76)) | (0x7 << 76)))),
    st.text(alphabet="0123456789abcdef-", min_size=1, max_size=40),
)


@given(sent=CLIENT_VALUES)
def test_pr_nuc_55_the_correlation_id_is_always_the_platform_one(sent: str) -> None:
    harness = Harness()
    allowed = harness.session("correlacion-ok")
    denied = harness.session("correlacion-no", role=Role.COPASST)
    client_headers = {
        "X-Correlation-Id": sent,
        "X-Request-Id": sent,
        "Correlation-Id": sent,
        "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
    }
    with TestClient(harness.app(), raise_server_exceptions=False) as client:
        client.cookies.set("correlation_id", sent)
        ok = client.get(
            "/me",
            headers={**client_headers, **cookie_header(allowed)},
            params={"correlation_id": sent},
        )
        refused = client.post(
            "/users",
            headers={**client_headers, **cookie_header(denied), **SAME_ORIGIN},
            json={"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4()),
                  "correlation_id": sent},
        )  # fmt: skip
        forged = client.post("/users", headers={**client_headers, **cookie_header(allowed)})
    assert ok.status_code == 200
    (handler_id,) = harness.observed.correlation_ids
    # El contexto del manejador y la consulta de la sesión llevan el mismo id generado (v7).
    assert handler_id.version == 7 and harness.store.lookups[0] == handler_id
    # Denegación: el id de la auditoría es el de la respuesta, y es de la plataforma.
    ((audit_context, _, _),) = harness.authz_audit.denied
    assert str(audit_context.correlation_id) == refused.json()["correlation_id"]
    # Barrera anti-falsificación: igual con csrf_rejected.
    ((csrf_context, _),) = harness.csrf_audit.with_session
    assert str(csrf_context.correlation_id) == forged.json()["correlation_id"]
    generated = {handler_id, audit_context.correlation_id, csrf_context.correlation_id}
    assert len(generated) == 3, "cada petición tiene su propio correlation_id"
    assert all(str(value) != sent.strip().lower() for value in generated)
    assert all(value.version == 7 for value in generated)
