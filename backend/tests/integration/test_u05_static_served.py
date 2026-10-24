"""La aplicación de página única servida por ``vigia-api`` (VIG-288; NFR-APP-14 y 24, REN-05).

La prueba construye un ``dist/`` con la forma que deja ``vite build`` con ``config/release.ts``
(``index.html`` con el marcador ``vigia-app``, ``version.json``, recursos con hash y sus ``.br`` y
``.gz``, y un ``.map`` que la construcción real nunca deja ahí) y lo sirve con la aplicación de
producción (``platform_units`` y toda la cadena de middleware), como en ``/app/static``:

- ``index.html`` con ``Cache-Control: no-store`` y el marcador;
- ``version.json`` con ``no-store`` y los campos que valida ``StaticSite``;
- un recurso con ``Cache-Control: public, max-age=31536000, immutable`` y ``Content-Encoding: br``
  (comparación exacta de cabeceras);
- un ``.map`` responde ``not_found``.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import brotli
import pytest
from fastapi.testclient import TestClient

from tests.api_support import World
from vigia_platform.shared.api.errors import ApiErrorBody
from vigia_platform.shared.api.static import AppRelease, StaticSite

pytestmark = pytest.mark.integration

APP_VERSION = "0123456789abcdef0123456789abcdef01234567"
VERSION = {
    "app_version": APP_VERSION,
    "contract_tag": "v1.1.0",
    "built_at": "2026-10-24T00:00:00.000Z",
}
INDEX = (
    b'<!doctype html>\n<html lang="es">\n  <head>\n    <meta charset="utf-8" />\n'
    b"    <title>Vig\xc3\xada</title>\n"
    b'    <script type="module" crossorigin src="/assets/index-DOLhPLFi.js"></script>\n'
    b'    <link rel="stylesheet" crossorigin href="/assets/index-C8sBFXX1.css">\n'
    b'  </head>\n  <body>\n    <div id="vigia-app"></div>\n  </body>\n</html>\n'
)
JS = b'const e=document.getElementById("vigia-app");console.assert(e!==null);\n' * 64
CSS = b"#vigia-app{min-height:100vh}\n" * 64
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _build_dist(root: Path) -> Path:
    """``dist/`` como lo deja ``npm run build`` (más un ``.map`` que nunca debe servirse)."""
    dist = root / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX)
    (dist / "version.json").write_text(json.dumps(VERSION) + "\n", encoding="utf-8")
    for name, content in (("index-DOLhPLFi.js", JS), ("index-C8sBFXX1.css", CSS)):
        (assets / name).write_bytes(content)
        (assets / f"{name}.br").write_bytes(brotli.compress(content, quality=11))
        (assets / f"{name}.gz").write_bytes(gzip.compress(content, compresslevel=9, mtime=0))
    (assets / "index-DOLhPLFi.js.map").write_text('{"version":3,"sources":["src/main.tsx"]}')
    return dist


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_dist(tmp_path_factory.mktemp("u05-static"))


@pytest.fixture(scope="module")
def client(dist: Path) -> TestClient:
    return TestClient(World().app(static_dir=dist))


def _raw(client: TestClient, path: str, headers: dict[str, str]) -> tuple[Any, bytes]:
    """Respuesta y cuerpo tal como viajan (sin que httpx los descomprima)."""
    with client.stream("GET", path, headers=headers) as response:
        return response, b"".join(response.iter_raw())


def test_the_built_dist_is_a_servable_static_site(dist: Path) -> None:
    site = StaticSite.load(dist)
    assert site.index == INDEX
    assert site.release == AppRelease(**VERSION)
    assert site.metric_version == APP_VERSION[:12]
    assert set(site.assets) == {"index-DOLhPLFi.js", "index-C8sBFXX1.css"}


def test_index_html_is_no_store_and_carries_the_marker(client: TestClient) -> None:
    response = client.get("/", headers=NAVIGATE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == INDEX
    assert b"vigia-app" in response.content


def test_version_json_is_no_store_with_the_release(client: TestClient) -> None:
    response = client.get("/version.json")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert response.json() == VERSION


@pytest.mark.parametrize(
    ("path", "content", "content_type"),
    [
        ("/assets/index-DOLhPLFi.js", JS, "text/javascript; charset=utf-8"),
        ("/assets/index-C8sBFXX1.css", CSS, "text/css; charset=utf-8"),
    ],
)
def test_an_asset_is_immutable_and_served_as_br(
    client: TestClient, path: str, content: bytes, content_type: str
) -> None:
    response, body = _raw(client, path, {"Accept-Encoding": "gzip, deflate, br, zstd"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-encoding"] == "br"
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["content-type"] == content_type
    assert brotli.decompress(body) == content


def test_a_gzip_only_client_receives_the_gz_variant(client: TestClient) -> None:
    response, body = _raw(client, "/assets/index-DOLhPLFi.js", {"Accept-Encoding": "gzip"})
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(body) == JS


@pytest.mark.parametrize(
    "path", ["/assets/index-DOLhPLFi.js.map", "/assets/INDEX-DOLHPLFI.JS.MAP", "/index.html.map"]
)
def test_a_source_map_is_not_found(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Accept-Encoding": "br, gzip"})
    assert response.status_code == 404
    assert ApiErrorBody.model_validate_json(response.content).code == "not_found"
