"""``/plants/*`` y ``/login/*`` en ``SCREEN_ROUTES`` (VIG-288; Q1 del dueño, 2026-10-23; D-3).

- Una navegación a una pantalla de planta (SCR-04, SCR-05 y SCR-07) o a un paso de SCR-01
  (``/login/second-factor``, ``/login/enroll``) recibe ``index.html``; antes, recargar en esas
  pantallas respondía ``not_found``.
- Cualquier otra petición a esas rutas sigue en la API: ``GET /plants/{plant_id}/admissions`` con
  ``Accept: application/json`` llega a su ruta (sin sesión responde ``unauthenticated``, no
  ``not_found`` ni ``index.html``).

La aplicación es la de producción (``platform_units``) con el ``dist/`` sintético de
``tests/fixtures/static/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api_support import World
from vigia_platform.shared.api.errors import ApiErrorBody
from vigia_platform.shared.api.static import SCREEN_ROUTES, is_screen_path

FIXTURE: Path = Path(__file__).resolve().parents[1] / "fixtures" / "static"
PLANT = "0192f0c4-7d1e-7a3b-9c4d-5e6f7a8b9c0d"
NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
RELOAD = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
JSON = {"Accept": "application/json"}

PLANT_SCREENS = ("catalog", "admissions", "gates", "policy", "fleet-thresholds")
LOGIN_STEPS = ("/login/second-factor", "/login/enroll")


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(World().app(static_dir=FIXTURE))


def _error_code(response: Any) -> str:
    return ApiErrorBody.model_validate_json(response.content).code


def test_screen_routes_include_plants_and_the_login_steps() -> None:
    assert "/plants/*" in SCREEN_ROUTES
    assert "/login/*" in SCREEN_ROUTES
    assert "/login" not in SCREEN_ROUTES  # `/login/*` ya cubre `/login`


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/login/",
        *LOGIN_STEPS,
        "/plants",
        f"/plants/{PLANT}",
        *(f"/plants/{PLANT}/{screen}" for screen in PLANT_SCREENS),
    ],
)
def test_plant_and_login_paths_are_screens(path: str) -> None:
    assert is_screen_path(path)


@pytest.mark.parametrize(
    "path",
    ["/plantsx", "/plants-x/1", "/loginx", "/login-x", "/plants/../me", "/plants/.x", "/login//x"],
)
def test_lookalike_paths_are_not_screens(path: str) -> None:
    assert not is_screen_path(path)


@pytest.mark.parametrize("headers", [NAVIGATE, RELOAD], ids=["navigate", "reload"])
@pytest.mark.parametrize("path", [*(f"/plants/{PLANT}/{s}" for s in PLANT_SCREENS), *LOGIN_STEPS])
def test_a_navigation_to_a_plant_screen_or_a_login_step_receives_index_html(
    client: TestClient, path: str, headers: dict[str, str]
) -> None:
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.content == (FIXTURE / "index.html").read_bytes()
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [JSON, {}], ids=["json", "sin-accept"])
def test_a_json_request_to_admissions_still_reaches_the_api(
    client: TestClient, headers: dict[str, str]
) -> None:
    response = client.get(f"/plants/{PLANT}/admissions", headers=headers)
    # La ruta de la API resolvió (sin sesión: `unauthenticated`); una ruta inexistente daría
    # `not_found` y la pantalla, `index.html`.
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"
    assert _error_code(response) == "unauthenticated"


@pytest.mark.parametrize("path", [f"/plants/{PLANT}/catalog", *LOGIN_STEPS])
def test_a_json_request_to_a_screen_without_api_route_is_not_found(
    client: TestClient, path: str
) -> None:
    response = client.get(path, headers=JSON)
    assert response.status_code == 404
    assert _error_code(response) == "not_found"


def test_a_navigation_post_to_admissions_never_receives_index_html(client: TestClient) -> None:
    response = client.post(
        f"/plants/{PLANT}/admissions",
        headers={**NAVIGATE, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"},
        content=b"{}",
    )
    assert response.headers["content-type"] == "application/json"
    assert response.status_code != 200
