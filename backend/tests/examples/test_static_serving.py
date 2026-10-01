"""Aplicación de página única servida desde ``vigia-api`` (TASK-138; pendientes nº 10 y 12).

Con el ``dist/`` sintético de ``tests/fixtures/static/``:

- ``/assets/*``: el ``.br`` o el ``.gz`` según ``Accept-Encoding``, con ``Content-Encoding``,
  ``Vary`` y ``ETag`` fuerte, y ``Cache-Control: public, max-age=31536000, immutable``; solo lo
  catalogado (nada de ``..``, ocultos, enlaces simbólicos ni precomprimidos pedidos a mano).
- ``index.html``, ``version.json`` y ``robots.txt`` (cerrado) con ``no-store``.
- D-3: ``index.html`` solo ante navegación a una ruta de pantalla; cualquier otra petición a esa
  ruta llega a la API. Las rutas de la API tienen precedencia: ``static/me`` no oculta ``/me``.
- Lista pública cerrada de BR-NUC-91, ``/assets/*`` fuera del registro JSON y dimensión
  ``app_version`` de ``health_ready``.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from vigia_contracts.clock import SystemClock

from tests.api_support import World
from vigia_platform.shared.api.app import AppConfig, UnitRegistration
from vigia_platform.shared.api.declarations import (
    Exposure,
    UnauthenticatedRoute,
    iter_declared_routes,
    requires,
)
from vigia_platform.shared.api.errors import ApiErrorBody, ApiStartupError
from vigia_platform.shared.api.health import health_router
from vigia_platform.shared.api.static import (
    ASSET_CACHE_CONTROL,
    ROBOTS_TXT,
    SCREEN_ROUTES,
    UNKNOWN_VERSION,
    AppRelease,
    Encoding,
    StaticSite,
    choose_encoding,
    is_navigation,
    is_request_logged,
    is_screen_path,
)
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.observability.redaction import AttributePolicy

FIXTURE: Path = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ASSETS = FIXTURE / "assets"
JS = "/assets/index-3f9a1c2b.js"
CSS = "/assets/index-7d2e4f10.css"
SVG = "/assets/logo-5b8c0d1e.svg"
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
JSON = {"Accept": "application/json"}
PERMISSIONS = frozenset({"account.read", "concessions.read", "concessions.manage"})


class AllowAll:
    """Autorizador de prueba: toda petición pasa (la autorización real es de TASK-125/134)."""

    async def authorize(self, request: Request, permission: str) -> None:
        return None


def _api_unit() -> UnitRegistration:
    """Rutas de la API que comparten camino con una pantalla o con un archivo de ``static/``."""
    router = APIRouter()

    @router.get("/me", dependencies=[requires("account.read")])
    async def me() -> dict[str, str]:
        return {"source": "api", "route": "me"}

    @router.get("/concessions", dependencies=[requires("concessions.read")])
    async def concessions() -> dict[str, str]:
        return {"source": "api", "route": "concessions"}

    @router.post("/concessions", dependencies=[requires("concessions.manage")])
    async def create_concession() -> dict[str, str]:
        return {"source": "api", "route": "concessions.create"}

    return UnitRegistration("shared", routers=(health_router(), router))


def _app(static_dir: Path = FIXTURE, world: World | None = None, **runtime: Any) -> Any:
    world = world if world is not None else World()
    return world.app(
        units=(_api_unit(),),
        permissions=PERMISSIONS,
        runtime={"authorizer": AllowAll(), **runtime},
        static_dir=static_dir,
    )


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app())


def _raw(client: TestClient, path: str, headers: dict[str, str]) -> tuple[Any, bytes]:
    """Respuesta y cuerpo **tal como viaja** (sin que httpx lo descomprima)."""
    with client.stream("GET", path, headers=headers) as response:
        return response, b"".join(response.iter_raw())


def _not_found(response: Any) -> None:
    assert response.status_code == 404
    assert ApiErrorBody.model_validate_json(response.content).code == "not_found"


# --- Criterio 1: recursos con hash precomprimidos e inmutables --------------------------------


def test_a_browser_accepting_br_receives_the_br_file_with_the_immutable_header(
    client: TestClient,
) -> None:
    response, body = _raw(client, JS, {"Accept-Encoding": "gzip, deflate, br, zstd"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "br"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    assert body == (ASSETS / "index-3f9a1c2b.js.br").read_bytes()
    assert response.headers["content-length"] == str(len(body))


def test_gzip_only_clients_get_the_gz_file_and_it_decodes_to_the_asset(client: TestClient) -> None:
    response, body = _raw(client, JS, {"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert gzip.decompress(body) == (ASSETS / "index-3f9a1c2b.js").read_bytes()


def test_an_asset_without_a_br_variant_falls_back_to_gzip(client: TestClient) -> None:
    response, body = _raw(client, CSS, {"Accept-Encoding": "br, gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"] == "text/css; charset=utf-8"
    assert body == (ASSETS / "index-7d2e4f10.css.gz").read_bytes()


@pytest.mark.parametrize(
    "accept_encoding",
    [None, "", "identity", "br;q=0, gzip;q=0", "deflate", "br;q=2", "gzip;q=abc"],
)
def test_without_an_acceptable_precompressed_variant_the_asset_goes_as_is(
    client: TestClient, accept_encoding: str | None
) -> None:
    headers = {} if accept_encoding is None else {"Accept-Encoding": accept_encoding}
    if accept_encoding is None:
        headers["Accept-Encoding"] = ""  # httpx pone «gzip, deflate…» si no se le dice nada
    response, body = _raw(client, JS, headers)
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert response.headers["vary"] == "Accept-Encoding"
    assert body == (ASSETS / "index-3f9a1c2b.js").read_bytes()


def test_an_asset_without_variants_is_served_as_is_with_the_same_cache(client: TestClient) -> None:
    response, body = _raw(client, SVG, {"Accept-Encoding": "br, gzip"})
    assert "content-encoding" not in response.headers
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert body == (ASSETS / "logo-5b8c0d1e.svg").read_bytes()


def test_each_variant_has_its_own_strong_etag_and_a_matching_if_none_match_is_304(
    client: TestClient,
) -> None:
    etags = {
        encoding: _raw(client, JS, {"Accept-Encoding": encoding})[0].headers["etag"]
        for encoding in ("br", "gzip", "identity")
    }
    assert len(set(etags.values())) == 3
    assert all(tag.startswith('"') and not tag.startswith("W/") for tag in etags.values())
    for encoding, etag in etags.items():
        cached = client.get(JS, headers={"Accept-Encoding": encoding, "If-None-Match": etag})
        assert cached.status_code == 304 and cached.content == b""
        assert cached.headers["etag"] == etag
        assert cached.headers["cache-control"] == ASSET_CACHE_CONTROL
    weak = client.get(JS, headers={"Accept-Encoding": "br", "If-None-Match": f"W/{etags['br']}"})
    assert weak.status_code == 304
    other = client.get(JS, headers={"Accept-Encoding": "br", "If-None-Match": etags["gzip"]})
    assert other.status_code == 200


def test_head_of_an_asset_answers_the_headers_without_a_body(client: TestClient) -> None:
    response = client.head(JS, headers={"Accept-Encoding": "br"})
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-encoding"] == "br"
    assert response.headers["cache-control"] == ASSET_CACHE_CONTROL


@pytest.mark.parametrize(
    "path",
    [
        "/assets/",
        "/assets/missing-0000.js",
        "/assets/index-3f9a1c2b.js.br",
        "/assets/index-3f9a1c2b.js.gz",
        "/assets/%2e%2e/me",
        "/assets/..%2fme",
        "/assets/me",
        "/assets/INDEX-3F9A1C2B.JS",
        "/assets//index-3f9a1c2b.js",
        "/assets/index-3f9a1c2b.js/",
        "/assets/" + "a" * 5000,
    ],
)
def test_only_catalogued_assets_are_served(client: TestClient, path: str) -> None:
    _not_found(client.get(path, headers={"Accept-Encoding": "br, gzip"}))


async def _asgi_status(app: Any, path: str) -> int:
    """Estado de una petición con ``path`` tal cual (httpx normalizaría ``..``)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"accept-encoding", b"br, gzip")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    statuses: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await app(scope, receive, send)
    return statuses[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/assets/../me", "/assets/../index.html", "/assets/./index-3f9a1c2b.js"]
)
async def test_dot_segments_in_the_raw_path_never_leave_the_catalogue(path: str) -> None:
    assert await _asgi_status(_app(), path) == 404


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "static"
    shutil.copytree(FIXTURE, target)
    return target


def test_hidden_files_symlinks_and_files_outside_assets_are_never_served(tmp_path: Path) -> None:
    static = _copy_fixture(tmp_path)
    (static / "assets" / ".env").write_text("SECRET=sintetico\n")
    (static / "assets" / "nested").mkdir()
    (static / "assets" / "nested" / "chunk-1a2b.js").write_text("export {};\n")
    (static / "assets" / "link.js").symlink_to(static / "me")
    (static / "assets" / "linked-dir").symlink_to(static, target_is_directory=True)
    client = TestClient(_app(static))
    for path in ("/assets/.env", "/assets/link.js", "/assets/linked-dir/me", "/me.br"):
        _not_found(client.get(path))
    nested = client.get("/assets/nested/chunk-1a2b.js", headers={"Accept-Encoding": ""})
    assert nested.status_code == 200 and nested.content == b"export {};\n"


def test_source_maps_are_never_served(tmp_path: Path) -> None:
    # Seguimiento de la revisión de VIG-71: un .map en assets/ revelaría el código original.
    static = _copy_fixture(tmp_path)
    for name in ("index-3f9a1c2b.js.map", "index-7d2e4f10.css.map", "VENDOR-1a2b.JS.MAP"):
        (static / "assets" / name).write_text('{"version":3,"sources":["src/app.tsx"]}')
    (static / "assets" / "index-3f9a1c2b.js.map.br").write_bytes(b"\x0b\x02\x80{}\x03")
    (static / "assets" / "nested").mkdir()
    (static / "assets" / "nested" / "chunk-1a2b.js.map").write_text("{}")
    client = TestClient(_app(static))
    for path in (
        "/assets/index-3f9a1c2b.js.map",
        "/assets/index-7d2e4f10.css.map",
        "/assets/VENDOR-1a2b.JS.MAP",
        "/assets/nested/chunk-1a2b.js.map",
    ):
        response = client.get(path, headers={"Accept-Encoding": "br, gzip"})
        _not_found(response)
        assert "immutable" not in response.headers.get("cache-control", "")
    assert client.get(JS).status_code == 200


def test_an_assets_directory_that_is_a_symlink_serves_nothing(tmp_path: Path) -> None:
    # Seguimiento de la revisión de VIG-71 (mutación M21): ``assets/`` como enlace simbólico a un
    # directorio con recursos válidos no se recorre.
    static = _copy_fixture(tmp_path)
    real = tmp_path / "otros-recursos"
    shutil.move(static / "assets", real)
    (static / "assets").symlink_to(real, target_is_directory=True)
    with pytest.raises(ApiStartupError, match="assets/ no es un directorio"):
        _app(static)


# --- Criterio 2: index.html, version.json y robots.txt sin caché -------------------------------


def test_index_html_answers_with_no_store(client: TestClient) -> None:
    response = client.get("/", headers=NAVIGATE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == (FIXTURE / "index.html").read_bytes()
    assert b"vigia-app" in response.content


def test_version_json_answers_with_no_store_and_the_build_release(client: TestClient) -> None:
    response = client.get("/version.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert response.json() == json.loads((FIXTURE / "version.json").read_text())


def test_robots_txt_is_closed_whatever_the_build_brings(client: TestClient) -> None:
    for method in ("GET", "HEAD"):
        response = client.request(method, "/robots.txt")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert client.get("/robots.txt").content == ROBOTS_TXT == b"User-agent: *\nDisallow: /\n"
    assert (FIXTURE / "robots.txt").read_bytes() != ROBOTS_TXT


# --- Criterio 3: precedencia de la API -----------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, JSON, NAVIGATE, {"Accept": "text/html"}])
def test_get_me_still_answers_the_api_although_static_me_exists(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert (FIXTURE / "me").is_file()
    response = client.get("/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"source": "api", "route": "me"}


def test_api_routes_win_over_any_file_of_the_build(tmp_path: Path) -> None:
    static = _copy_fixture(tmp_path)
    (static / "health").mkdir()
    (static / "health" / "live").write_text("sombra\n")
    (static / "assets" / "health").mkdir()
    (static / "assets" / "health" / "live").write_text("sombra\n")
    client = TestClient(_app(static))
    for headers in ({}, NAVIGATE):
        response = client.get("/health/live", headers=headers)
        assert response.json() == {"status": "live"}


# --- Criterio 4: index.html solo a navegaciones (D-3) ------------------------------------------


def test_concessions_navigation_gets_index_html_and_json_gets_the_api(client: TestClient) -> None:
    navigation = client.get("/concessions", headers={"Sec-Fetch-Mode": "navigate"})
    assert navigation.status_code == 200
    assert navigation.content == (FIXTURE / "index.html").read_bytes()
    assert navigation.headers["cache-control"] == "no-store"
    api = client.get("/concessions", headers=JSON)
    assert api.status_code == 200
    assert api.json() == {"source": "api", "route": "concessions"}


@pytest.mark.parametrize(
    ("headers", "navigation"),
    [
        ({"Sec-Fetch-Mode": "navigate"}, True),
        ({"Sec-Fetch-Mode": "navigate", "Accept": "application/json"}, True),
        ({"Accept": "text/html"}, True),
        ({"Accept": "text/html;q=0.9, */*;q=0.8"}, True),
        ({"Accept": "TEXT/HTML"}, True),
        ({"Accept": "text/html, application/json"}, False),
        ({"Accept": "application/json;q=0.1, text/html"}, False),
        ({"Accept": "text/html, application/json;q=0"}, True),
        ({"Accept": "text/html;q=0"}, False),
        ({"Accept": "*/*"}, False),
        ({"Accept": "text/*"}, False),
        ({"Sec-Fetch-Mode": "cors"}, False),
        ({"Sec-Fetch-Mode": "no-cors", "Accept": "application/json"}, False),
        ({}, False),
    ],
)
def test_navigation_rule_decides_between_index_and_api(
    client: TestClient, headers: dict[str, str], navigation: bool
) -> None:
    assert is_navigation({key.lower(): value for key, value in headers.items()}) is navigation
    response = client.get("/concessions", headers=headers)
    assert response.status_code == 200
    if navigation:
        assert response.content == (FIXTURE / "index.html").read_bytes()
    else:
        assert response.json() == {"source": "api", "route": "concessions"}


def test_a_navigation_post_still_reaches_the_api(client: TestClient) -> None:
    # ``Sec-Fetch-Site`` lo exige la barrera anti-falsificación de la cadena (TASK-134).
    response = client.post("/concessions", headers={**NAVIGATE, "Sec-Fetch-Site": "same-origin"})
    assert response.json() == {"source": "api", "route": "concessions.create"}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/login",
        "/zones",
        "/zones/0192f0c4-7d1e-7a3b-9c4d-5e6f7a8b9c0d",
        "/fleet/nodes/abc",
        "/notifications/",
        "/invitations/abcDEF123_-",
        "/findings/f-1/closure",
        "/review-queue",
    ],
)
def test_every_screen_route_answers_index_html_to_a_navigation(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers=NAVIGATE)
    assert response.status_code == 200
    assert response.content == (FIXTURE / "index.html").read_bytes()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/zones/x", "/fleet/", "/notifications/abc"])
def test_a_non_navigation_to_a_screen_route_goes_to_the_api(client: TestClient, path: str) -> None:
    _not_found(client.get(path, headers=JSON))
    _not_found(client.get(path))


@pytest.mark.parametrize(
    "path",
    ["/unknown", "/zonesx", "/me/x", "/api/x", "/.well-known/x", "/index.html", "/assets"],
)
def test_a_navigation_outside_the_screen_list_goes_to_the_api(
    client: TestClient, path: str
) -> None:
    assert not is_screen_path(path)
    _not_found(client.get(path, headers=NAVIGATE))


def test_without_index_html_every_screen_request_reaches_the_api(tmp_path: Path) -> None:
    static = _copy_fixture(tmp_path)
    (static / "index.html").unlink()
    client = TestClient(_app(static))
    response = client.get("/concessions", headers=NAVIGATE)
    assert response.json() == {"source": "api", "route": "concessions"}
    _not_found(client.get("/", headers=NAVIGATE))
    assert client.get(JS, headers={"Accept-Encoding": "br"}).status_code == 200


def test_an_image_without_the_application_still_starts_and_serves_only_robots(
    tmp_path: Path,
) -> None:
    client = TestClient(_app(tmp_path / "no-existe"))
    assert client.get("/concessions", headers=NAVIGATE).json()["source"] == "api"
    _not_found(client.get("/version.json"))
    _not_found(client.get(JS))
    assert client.get("/robots.txt").content == ROBOTS_TXT


# --- Lista pública cerrada de BR-NUC-91 ---------------------------------------------------------


def test_static_routes_are_entries_of_the_closed_public_list() -> None:
    app = _app()
    declared = {
        route.path: route.declarations[0].unauthenticated
        for route in iter_declared_routes(app.routes)
        if route.declarations and route.declarations[0].unauthenticated is not None
    }
    entries = {
        UnauthenticatedRoute.APP_SCREEN,
        UnauthenticatedRoute.APP_ASSET,
        UnauthenticatedRoute.APP_VERSION,
        UnauthenticatedRoute.ROBOTS,
    }
    for entry in entries:
        assert declared[entry.path] is entry
        assert entry.method == "GET" and entry.exposure is Exposure.PUBLIC
    assert all(screen.startswith("/") for screen in SCREEN_ROUTES)
    assert "/concessions" in SCREEN_ROUTES and "/zones/*" in SCREEN_ROUTES


def test_static_routes_stay_out_of_the_openapi_spec() -> None:
    spec = _app().openapi()
    assert not {"/assets/{asset_path}", "/version.json", "/robots.txt", "/{screen_path}"} & set(
        spec["paths"]
    )


# --- Registro y métrica de salud ----------------------------------------------------------------


def test_assets_leave_no_line_in_the_application_json_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    def application_lines() -> list[logging.LogRecord]:
        # Lo que escribe la aplicación (``vigia.*``); asyncio y httpx son del cliente de prueba.
        return [record for record in caplog.records if record.name.startswith("vigia.")]

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    client.get(JS, headers={"Accept-Encoding": "br"})
    client.get("/assets/missing-0000.js")
    client.get(JS, headers={"If-None-Match": '"x"'})
    client.head(CSS)
    assert application_lines() == []
    client.get("/concessions", headers=NAVIGATE)
    assert [record.getMessage() for record in application_lines()] == [
        "aplicación de página única servida a una navegación",
        # Línea por petición de la cadena (TASK-134): la plantilla, nunca la ruta pedida.
        "petición atendida",
    ]
    assert all("/concessions" not in str(vars(record)) for record in application_lines())
    assert not is_request_logged(JS) and not is_request_logged("/assets/missing")
    assert is_request_logged("/") and is_request_logged("/version.json")
    assert is_request_logged("/concessions") and is_request_logged("/assetsx")


def _health_points(reader: InMemoryMetricReader) -> dict[str, float]:
    points: dict[str, float] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == MetricName.HEALTH_READY.value:
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        points[str(point.attributes["app_version"])] = float(point.value)
    return points


def _wait_started(app: Any) -> None:
    clock = SystemClock()
    deadline = clock.monotonic() + 10.0
    while not app.state.vigia_readiness.started:
        assert clock.monotonic() < deadline, "el arranque no terminó a tiempo"
        time.sleep(0.01)


def _metrics() -> tuple[PlatformMetrics, InMemoryMetricReader, AttributePolicy]:
    """Métricas en memoria con la misma política que amplía la fábrica."""
    reader = InMemoryMetricReader()
    policy = AttributePolicy()
    meter = MeterProvider(metric_readers=[reader]).get_meter("pruebas")
    return PlatformMetrics(meter, policy), reader, policy


def test_health_metric_carries_the_deployed_app_version() -> None:
    metrics, reader, policy = _metrics()
    world = World()
    app = _app(world=world, metrics=metrics, attribute_policy=policy)
    with TestClient(app) as client:
        _wait_started(app)
        assert client.get("/health/ready").status_code == 200
        assert _health_points(reader) == {"v0.1.0-test": 1.0}
        world.storage.present = False
        assert client.get("/health/ready").status_code == 503
        assert _health_points(reader) == {"v0.1.0-test": 0.0}


def test_health_metric_without_a_build_reports_unknown(tmp_path: Path) -> None:
    metrics, reader, policy = _metrics()
    app = _app(tmp_path / "vacio", metrics=metrics, attribute_policy=policy)
    client = TestClient(app)  # sin arranque: la salud profunda responde 503
    assert client.get("/health/ready").status_code == 503
    assert _health_points(reader) == {UNKNOWN_VERSION: 0.0}


@pytest.mark.parametrize(
    ("version", "dimension"),
    [
        ("v1.2.3", "v1.2.3"),
        ("0123456789abcdef0123456789abcdef01234567", "0123456789ab"),
        ("abcdefghijklmnopqrstuvwxyz0123", UNKNOWN_VERSION),
    ],
)
def test_metric_version_is_low_cardinality_and_never_token_shaped(
    version: str, dimension: str
) -> None:
    site = StaticSite(release=AppRelease(version, "v1.0.0", "2026-09-30T00:00:00.000Z"))
    assert site.metric_version == dimension


# --- Construcción no servible: no arranca --------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"[]",
        b'{"app_version": "v1", "contract_tag": "v1.0.0"}',
        b'{"app_version": NaN, "contract_tag": "v1.0.0", "built_at": "2026-09-30T00:00:00Z"}',
        b'{"app_version": "v 1", "contract_tag": "v1.0.0", "built_at": "2026-09-30T00:00:00Z"}',
        b'{"app_version": "v1", "contract_tag": "v1.0.0", "built_at": "ayer"}',
        b'{"app_version": "v1", "contract_tag": 1, "built_at": "2026-09-30T00:00:00Z"}',
        b"\xff\xfe",
        b"[" * 100_000,
        b" " * (64 * 1024 + 1),
    ],
)
def test_an_invalid_version_json_prevents_startup(tmp_path: Path, content: bytes) -> None:
    static = _copy_fixture(tmp_path)
    (static / "version.json").write_bytes(content)
    with pytest.raises(ApiStartupError) as raised:
        _app(static)
    assert "version.json" in str(raised.value)


def test_a_symlinked_index_or_a_static_dir_that_is_a_file_prevents_startup(tmp_path: Path) -> None:
    static = _copy_fixture(tmp_path)
    (static / "index.html").unlink()
    (static / "index.html").symlink_to(static / "me")
    with pytest.raises(ApiStartupError, match=r"index\.html"):
        _app(static)
    plain = tmp_path / "archivo"
    plain.write_text("no soy un directorio\n")
    with pytest.raises(ApiStartupError, match="directorio"):
        _app(plain)
    oversized = _copy_fixture(tmp_path / "grande")
    (oversized / "index.html").write_bytes(b"<!doctype html>" + b" " * (1 << 20))
    with pytest.raises(ApiStartupError, match=r"index\.html"):
        _app(oversized)


def test_static_dir_comes_from_the_environment() -> None:
    environ = {
        "VIGIA_ENVIRONMENT": "pilot",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
        "VIGIA_STATIC_DIR": os.fspath(FIXTURE),
    }
    assert AppConfig.from_environ(environ).static_dir == FIXTURE
    del environ["VIGIA_STATIC_DIR"]
    assert AppConfig.from_environ(environ).static_dir == Path("/app/static")


# --- Propiedades de la negociación ---------------------------------------------------------------

_HEADER_TEXT = st.text(
    alphabet=st.characters(codec="latin-1", exclude_characters="\r\n"), max_size=300
)
_ENCODINGS = st.frozensets(st.sampled_from([Encoding.BR, Encoding.GZIP]))


@given(header=_HEADER_TEXT, available=_ENCODINGS)
def test_choose_encoding_only_picks_an_available_variant(
    header: str, available: frozenset[Encoding]
) -> None:
    chosen = choose_encoding(header, available)
    assert chosen is Encoding.IDENTITY or chosen in available


@given(accept=_HEADER_TEXT, mode=st.one_of(st.none(), _HEADER_TEXT))
def test_is_navigation_never_raises_and_json_clients_are_never_navigations(
    accept: str, mode: str | None
) -> None:
    headers = {"accept": f"application/json, {accept}"}
    if mode is not None:
        headers["sec-fetch-mode"] = mode
    expected = mode is not None and mode.strip().lower() == "navigate"
    assert is_navigation(headers) is expected
