"""PR-NUC-38 (ampliada a cabeceras): cuerpos mutados y cabeceras de seguridad en toda respuesta.

- Para toda ruta con cuerpo o identificador del catálogo y toda mutación generada (campo extra,
  tipo cambiado, campo que falta, identificador malformado, JSON roto, tamaño excedido), la
  respuesta es ``invalid_request`` o ``payload_too_large`` con el mensaje genérico de su código y
  sin detalle interno (BR-NUC-92, 93).
- Límite de cuerpo (paso 4): 1 MB en toda ruta del catálogo, con o sin ``Content-Length``; la ruta
  que declara el suyo (``body_limit``) se rige por él.
- Cabeceras (paso 3, BR-NUC-95, NFR-NUC-18, pendientes nº 8 y 9): toda respuesta (éxito, error de
  la ruta, error de la cadena, ``429``, ``413``, ``500``, estáticos) lleva HSTS de un año con
  subdominios, ``nosniff``, ``DENY``, ``Referrer-Policy`` y la política de contenido de
  PAT-APP-SEG-01 con ``VIGIA_CSP_STORE_ORIGINS`` en ``media-src`` y ``connect-src``, sin
  ``unsafe-*`` y sin CORS.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from tests.middleware_support import (
    ORIGIN,
    SAME_ORIGIN,
    STORE_ORIGIN,
    Harness,
    UnlimitedRateLimiter,
    cookie_header,
)
from vigia_platform.shared.api.app import AppConfig, UnitRegistration, platform_units
from vigia_platform.shared.api.declarations import DeclaredRoute, iter_declared_routes, requires
from vigia_platform.shared.api.errors import ApiErrorBody, ApiErrorCode
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.api.middleware import (
    DEFAULT_BODY_LIMIT_BYTES,
    content_security_policy,
    parse_store_origins,
)
from vigia_platform.shared.api.middleware.headers import store_origin

LABELS = PlatformLabels.load()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "static"
EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    f"media-src 'self' {STORE_ORIGIN}; connect-src 'self' {STORE_ORIGIN}; "
    "frame-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; require-trusted-types-for 'script'"
)
INTERNAL_MARKERS = ("Traceback", "pydantic", "ValidationError", "loc", "input", "File ", "line ")


# --- Catálogo de rutas con cuerpo ----------------------------------------------------------------

VALID_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/users"): {"email": "a@b.co", "role": "copasst", "plant_id": str(uuid.uuid4())},
    ("PATCH", "/users/{user_id}"): {"display_name": "Nombre sintético", "active": True},
    ("POST", "/auth/login"): {"email": "a@b.co", "password": "contraseña-sintética"},
    ("POST", "/api/nodes/heartbeat"): {"sequence": 3},
}
"""Un cuerpo válido por ruta con cuerpo del catálogo de la unidad de prueba."""


def _url(path: str, user_id: str | None = None) -> str:
    return path.replace("{user_id}", user_id or str(uuid.uuid4()))


@pytest.fixture(scope="module")
def world() -> Iterator[tuple[Harness, Any, dict[str, str]]]:
    harness = Harness()
    cookie = harness.session("mutaciones")
    # La tasa no es el asunto de este archivo (lo es de test_rate_limit.py).
    app = harness.app(rate_limiter=UnlimitedRateLimiter())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield harness, client, {**cookie_header(cookie), **SAME_ORIGIN}


def _routes(client: Any) -> list[DeclaredRoute]:
    return list(iter_declared_routes(client.app.routes))


def test_the_catalog_of_body_routes_is_complete(world: tuple[Harness, Any, Any]) -> None:
    _, client, _ = world
    with_body = {
        (method, route.path)
        for route in _routes(client)
        if getattr(route.route, "body_field", None) is not None
        for method in route.methods
    }
    assert with_body == set(VALID_BODIES)


def test_the_valid_bodies_are_accepted(world: tuple[Harness, Any, dict[str, str]]) -> None:
    _, client, headers = world
    for (method, path), body in VALID_BODIES.items():
        response = client.request(method, _url(path), headers=headers, json=body)
        assert response.status_code == 200, (method, path, response.text)


MUTATIONS = st.sampled_from(
    ["extra", "type", "missing", "null", "array", "broken_json", "malformed_id", "oversized"]
)
ROUTES = st.sampled_from(sorted(VALID_BODIES))
WEIRD_VALUES = st.one_of(
    st.integers(), st.floats(allow_nan=False), st.booleans(), st.lists(st.integers(), max_size=3)
)


def _mutated(
    data: st.DataObject, mutation: str, route: tuple[str, str]
) -> tuple[str, bytes, dict[str, str]]:
    _, path = route
    body = dict(VALID_BODIES[route])
    url = _url(path)
    if mutation == "extra":
        name = data.draw(st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12))
        if name in body:
            name += "_extra"
        body[name] = data.draw(WEIRD_VALUES)
    elif mutation == "type":
        field = data.draw(st.sampled_from(sorted(body)))
        current = body[field]
        replacement = data.draw(
            WEIRD_VALUES.filter(lambda value: type(value) is not type(current))
            if not isinstance(current, str)
            else WEIRD_VALUES
        )
        body[field] = replacement
    elif mutation == "missing":
        del body[data.draw(st.sampled_from(sorted(body)))]
    elif mutation == "null":
        body[data.draw(st.sampled_from(sorted(body)))] = None
    elif mutation == "array":
        return url, json.dumps([body]).encode(), {"Content-Type": "application/json"}
    elif mutation == "broken_json":
        raw = json.dumps(body)
        cut = data.draw(st.integers(1, len(raw) - 1))
        return url, raw[:cut].encode(), {"Content-Type": "application/json"}
    elif mutation == "malformed_id":
        if "{user_id}" in path:
            bad = data.draw(st.sampled_from(["no-es-uuid", "1234", "zzzzzzzz-zzzz-zzzz-zzzz-zzzz"]))
            url = _url(path, bad)
        elif "plant_id" in body:
            body["plant_id"] = data.draw(st.sampled_from(["no-es-uuid", "", "0" * 31]))
        else:
            # Sin identificador en la ruta: el campo de texto, vacío (fuera de su longitud mínima).
            body[next(iter(body))] = "" if isinstance(next(iter(body.values())), str) else "x"
    elif mutation == "oversized":
        limit = 4096 if path.startswith("/api/nodes/") else DEFAULT_BODY_LIMIT_BYTES
        body["relleno"] = "x" * (limit + 1)
    return url, json.dumps(body).encode(), {"Content-Type": "application/json"}


def _generic_error(response: Any) -> ApiErrorBody:
    body = ApiErrorBody.model_validate_json(response.content)
    assert body.message_es == LABELS.label("api_error_code", body.code.value)
    assert set(response.json()) <= {"code", "message_es", "correlation_id", "retry_after_seconds"}
    for marker in INTERNAL_MARKERS:
        assert marker not in response.text
    return body


@given(data=st.data(), mutation=MUTATIONS, route=ROUTES)
def test_pr_nuc_38_every_mutated_body_is_a_generic_rejection(
    world: tuple[Harness, Any, dict[str, str]], data: st.DataObject, mutation: str, route: Any
) -> None:
    harness, client, headers = world
    handled = len(harness.observed.handled)
    url, content, extra = _mutated(data, mutation, route)
    response = client.request(route[0], url, headers={**headers, **extra}, content=content)
    body = _generic_error(response)
    expected = (
        {ApiErrorCode.PAYLOAD_TOO_LARGE}
        if mutation == "oversized"
        else {ApiErrorCode.INVALID_REQUEST}
    )
    assert body.code in expected, (mutation, route, response.text)
    assert len(harness.observed.handled) == handled, "el manejador no se ejecuta"
    _assert_security_headers(response)


# --- Límite de cuerpo en todo el catálogo --------------------------------------------------------


def _catalog(client: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in _routes(client):
        for method in sorted(route.methods):
            if method != "HEAD":
                path = route.path.replace("{screen_path:path}", "login").replace(
                    "{asset_path:path}", "x.js"
                )
                found.append((method, _url(path)))
    return found


def test_every_route_of_the_catalog_rejects_a_body_over_its_limit(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world
    oversized = b"x" * (DEFAULT_BODY_LIMIT_BYTES + 1)
    catalog = _catalog(client)
    assert len(catalog) >= 9
    for method, url in catalog:
        response = client.request(method, url, headers=headers, content=oversized)
        assert _generic_error(response).code is ApiErrorCode.PAYLOAD_TOO_LARGE, (method, url)
        assert response.status_code == 413
        _assert_security_headers(response)


def test_a_chunked_body_over_the_limit_is_payload_too_large(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world

    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 65_536

    response = client.post("/users", headers=headers, content=chunks())
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert _generic_error(response).code is ApiErrorCode.PAYLOAD_TOO_LARGE


def test_a_body_exactly_at_the_limit_passes_the_chain(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    _, client, headers = world
    body = dict(VALID_BODIES[("POST", "/users")])
    raw = json.dumps(body).encode()
    padded = raw[:-1] + b" " * (DEFAULT_BODY_LIMIT_BYTES - len(raw)) + b"}"
    assert len(padded) == DEFAULT_BODY_LIMIT_BYTES
    response = client.post(
        "/users", headers={**headers, "Content-Type": "application/json"}, content=padded
    )
    assert response.status_code == 200


def test_a_route_with_its_own_limit_uses_it(world: tuple[Harness, Any, dict[str, str]]) -> None:
    _, client, _ = world
    raw = json.dumps({"sequence": 1}).encode()
    at_limit = raw + b" " * (4096 - len(raw))
    over = at_limit + b" "
    json_type = {"Content-Type": "application/json"}
    assert (
        client.post("/api/nodes/heartbeat", content=at_limit, headers=json_type).status_code == 200
    )
    response = client.post("/api/nodes/heartbeat", content=over, headers=json_type)
    assert _generic_error(response).code is ApiErrorCode.PAYLOAD_TOO_LARGE
    # Un límite propio más pequeño no se aplica a las demás rutas.
    padded = json.dumps(VALID_BODIES[("POST", "/auth/login")]).encode() + b" " * 8000
    login = client.post("/auth/login", content=padded, headers={**json_type, **SAME_ORIGIN})
    assert login.status_code == 200


@pytest.mark.parametrize("value", ["abc", "-1", "1, 2", "1.5", " 12x", "99999999999999999999999"])
def test_a_malformed_content_length_is_invalid(
    world: tuple[Harness, Any, dict[str, str]], value: str
) -> None:
    _, client, _ = world
    scope_headers = [(b"content-length", value.encode()), (b"sec-fetch-site", b"same-origin")]
    response = _raw_request(client.app, "POST", "/auth/login", scope_headers, b"{}")
    assert response["status"] in (400, 413)
    _assert_raw_security_headers(response)


def _raw_request(
    app: Any, method: str, path: str, headers: list[tuple[bytes, bytes]], body: bytes
) -> dict[str, Any]:
    """Una petición ASGI directa (para cabeceras que el cliente HTTP no deja mal formar)."""
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 443),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {"status": start["status"], "headers": start["headers"]}


def _raw_stream_status(
    app: Any, path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> int:
    """``GET`` ASGI directo con el cuerpo en varios mensajes (como uno ``chunked``)."""
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.10", 50000),
        "server": ("testserver", 443),
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    status: int = start["status"]
    return status


def test_r10_one_byte_over_the_limit_while_reading_the_body(
    world: tuple[Harness, Any, dict[str, str]],
) -> None:
    # Seguimiento de VIG-78 (R10): sin Content-Length, el tope se mide al leer el cuerpo. La ruta
    # de salud responde 200 si el cuerpo pasa, así que 413 solo puede venir del límite.
    _, client, _ = world
    limit = DEFAULT_BODY_LIMIT_BYTES
    exact = [b"x" * (limit - 1), b"y"]
    over = [b"x" * (limit - 1), b"y", b"z"]
    assert _raw_stream_status(client.app, "/health/live", [], exact) == 200
    assert _raw_stream_status(client.app, "/health/live", [], over) == 413


@pytest.mark.parametrize("values", [("10", "11"), ("11", "10"), ("0", str(2**20 + 1))])
def test_r21_two_different_content_lengths_are_invalid(
    world: tuple[Harness, Any, dict[str, str]], values: tuple[str, str]
) -> None:
    # Seguimiento de VIG-78 (R21): dos Content-Length distintos no se resuelven tomando uno.
    _, client, _ = world
    headers = [(b"content-length", value.encode()) for value in values]
    assert _raw_stream_status(client.app, "/health/live", headers, [b"0123456789"]) == 400
    same = [(b"content-length", b"10"), (b"content-length", b"10")]
    assert _raw_stream_status(client.app, "/health/live", same, [b"0123456789"]) == 200


# --- Cabeceras de seguridad en toda respuesta ----------------------------------------------------


def _assert_security_headers(response: Any) -> None:
    headers = response.headers
    assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["content-security-policy"] == EXPECTED_CSP
    assert "unsafe-" not in headers["content-security-policy"]
    assert not [name for name in headers if name.lower().startswith("access-control-")]
    for name in ("content-security-policy", "x-frame-options", "strict-transport-security"):
        assert len(headers.get_list(name)) == 1


def _assert_raw_security_headers(response: dict[str, Any]) -> None:
    names = [bytes(name).lower() for name, _ in response["headers"]]
    for name in (b"strict-transport-security", b"x-content-type-options", b"x-frame-options",
                 b"referrer-policy", b"content-security-policy"):  # fmt: skip
        assert names.count(name) == 1


def _hostile_unit() -> UnitRegistration:
    router = APIRouter()

    @router.get("/hostil", dependencies=[requires("hierarchy.read")])
    async def hostile() -> JSONResponse:
        return JSONResponse(
            {"ok": True},
            headers={
                "Content-Security-Policy": "default-src * 'unsafe-inline' 'unsafe-eval'",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Credentials": "true",
                "X-Frame-Options": "ALLOWALL",
            },
        )

    @router.get("/rompe", dependencies=[requires("hierarchy.read")])
    async def broken() -> None:
        raise RuntimeError("SELECT * FROM identity.user_account")

    return UnitRegistration("hostil", routers=(router,))


def test_a_route_cannot_weaken_or_duplicate_the_security_headers() -> None:
    # Una ruta que intenta poner su propia política, CORS o marcos no rebaja nada.
    harness = Harness()
    app = harness.world.app(
        units=(*platform_units(), _hostile_unit()),
        csp_store_origins=(STORE_ORIGIN,),
        runtime={"authorizer": _AllowAll()},
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/hostil")
    assert response.status_code == 200 and response.json() == {"ok": True}
    _assert_security_headers(response)


PATHS = st.sampled_from(
    ["/", "/me", "/users", "/health/live", "/health/ready", "/no-existe", "/assets/x.js",
     "/version.json", "/robots.txt", "/api/nodes/x", "/auth/login", "/docs", "/openapi.json"]
)  # fmt: skip
METHODS = st.sampled_from(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])


@given(path=PATHS, method=METHODS, navigate=st.booleans(), same_origin=st.booleans())
def test_pr_nuc_38_every_response_carries_the_security_headers(
    world: tuple[Harness, Any, dict[str, str]],
    path: str,
    method: str,
    navigate: bool,
    same_origin: bool,
) -> None:
    _, client, headers = world
    extra = dict(headers) if same_origin else {}
    if navigate:
        extra.update({"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    response = client.request(method, path, headers=extra)
    _assert_security_headers(response)


def test_static_files_and_screens_carry_the_security_headers() -> None:
    # Seguimiento de la revisión de VIG-71: index.html y assets/ con política y nosniff.
    harness = Harness()
    cookie = harness.session("estaticos")
    app = harness.world.app(
        units=(*platform_units(),),
        static_dir=FIXTURE,
        csp_store_origins=(STORE_ORIGIN,),
        public_origin=ORIGIN,
    )
    with TestClient(app) as client:
        index = client.get("/", headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
        asset = client.get("/assets/index-3f9a1c2b.js")
        version = client.get("/version.json")
        robots = client.get("/robots.txt")
        missing = client.get("/assets/no-existe.js", headers=cookie_header(cookie))
    assert index.status_code == asset.status_code == version.status_code == 200
    assert robots.status_code == 200 and missing.status_code == 404
    for response in (index, asset, version, robots, missing):
        _assert_security_headers(response)


def test_errors_of_the_chain_and_of_the_route_carry_the_security_headers() -> None:
    harness = Harness()
    cookie = harness.session("errores")
    app = harness.world.app(
        units=(*platform_units(), _hostile_unit()),
        csp_store_origins=(STORE_ORIGIN,),
        runtime={
            "sessions": harness.contexts,
            "privacy_notice_version": "v1-prueba",
            "authorizer": _AllowAll(),
        },
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        broken = client.get("/rompe", headers=cookie_header(cookie))
        forbidden = client.post("/rompe")
    assert broken.status_code == 500 and broken.json()["code"] == "internal_error"
    assert "SELECT" not in broken.text
    assert forbidden.status_code == 403
    _assert_security_headers(broken)
    _assert_security_headers(forbidden)


class _AllowAll:
    async def authorize(self, request: Any, permission: str) -> None:
        return None


# --- Política de contenido y su variable ---------------------------------------------------------


def test_the_policy_composes_the_store_origins_without_unsafe_directives() -> None:
    other = "https://vigia-documents-000000000000-us-east-1.s3.us-east-1.amazonaws.com"
    policy = content_security_policy([STORE_ORIGIN, other])
    directives = dict(item.split(" ", 1) for item in policy.split("; "))
    assert directives["media-src"] == f"'self' {STORE_ORIGIN} {other}"
    assert directives["connect-src"] == f"'self' {STORE_ORIGIN} {other}"
    for name in ("default-src", "script-src", "style-src", "img-src", "base-uri", "form-action"):
        assert directives[name] == "'self'"
    for name in ("frame-src", "object-src", "frame-ancestors"):
        assert directives[name] == "'none'"
    assert directives["require-trusted-types-for"] == "'script'"
    assert "unsafe" not in policy and "*" not in policy and "data:" not in policy
    assert content_security_policy([]).count("media-src 'self';") == 1


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example 'unsafe-inline'",
        "https://a.example;script-src *",
        "https://*.amazonaws.com",
        "*",
        "'unsafe-eval'",
        "data:",
        "HTTPS://BUCKET.S3.AMAZONAWS.COM",
        "https://bucket.s3.amazonaws.com/",
        "https://bucket.s3.amazonaws.com/ruta",
        "http://bucket.s3.amazonaws.com",
        "https://bucket.s3.amazonaws.com:0",
        "https://bucket.s3.amazonaws.com,https://otro.example",
        "https://bu​cket.s3.amazonaws.com",
        "https://bucket.s3.amazonaws.com\t",
        "https://localhost",
        "javascript:alert(1)",
        "",
    ],
)
def test_a_hostile_store_origin_never_reaches_the_policy(value: str) -> None:
    with pytest.raises(ValueError):
        store_origin(value, allow_local=False)
    with pytest.raises(ValueError):
        AppConfig(environment="pilot", data_key_id="alias/x", csp_store_origins=(value,))


def test_the_variable_is_a_space_separated_list_read_from_the_environment() -> None:
    other = "https://otro-bucket.s3.us-east-1.amazonaws.com"
    config = AppConfig.from_environ(
        {
            "VIGIA_ENVIRONMENT": "pilot",
            "VIGIA_SECRETS_KEY_ARN": "alias/x",
            "VIGIA_CSP_STORE_ORIGINS": f"  {STORE_ORIGIN}   {other} ",
            "VIGIA_PUBLIC_ORIGIN": "https://app.vigia.example",
        }
    )
    assert config.csp_store_origins == (STORE_ORIGIN, other)
    assert config.public_origin == "https://app.vigia.example"
    assert parse_store_origins(f"{STORE_ORIGIN} {STORE_ORIGIN}", allow_local=False) == (
        STORE_ORIGIN,
    )
    with pytest.raises(ValueError):
        AppConfig(
            environment="pilot",
            data_key_id="alias/x",
            csp_store_origins=(STORE_ORIGIN, STORE_ORIGIN),
        )
    with pytest.raises(ValueError):
        AppConfig(
            environment="pilot",
            data_key_id="alias/x",
            csp_store_origins=tuple(f"https://b{i}.s3.amazonaws.com" for i in range(9)),
        )


def test_local_store_origins_are_only_admitted_in_local_and_test() -> None:
    local = "http://localhost:4566"
    assert AppConfig(environment="local", data_key_id="a", csp_store_origins=(local,))
    assert AppConfig(environment="test", data_key_id="a", public_origin="http://127.0.0.1:8000")
    for environment in ("pilot", "staging-1"):
        with pytest.raises(ValueError):
            AppConfig(environment=environment, data_key_id="a", csp_store_origins=(local,))
        with pytest.raises(ValueError):
            AppConfig(environment=environment, data_key_id="a", public_origin=local)
