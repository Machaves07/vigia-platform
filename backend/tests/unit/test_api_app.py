"""Fábrica de la aplicación, arranque supervisado y salud con dobles (TASK-133; LC-NUC-19).

- Arranque (PAT-NUC-RES-02): la lista fija corre en orden; mientras no termina, ``live`` 200 y
  ``ready`` 503; lo que falla se reintenta y, pasado el plazo de 60 s, el proceso termina con
  código 3 sin haber quedado listo. Un esquema anterior al mínimo o la seguridad a nivel de fila
  sin efecto impiden arrancar.
- Salud (NFR-NUC-13): con la base o el almacén caídos o colgados, ``ready`` falla en menos de 2 s
  y ``live`` responde 200; una clave de firma que falta en memoria también la hace fallar; una
  dependencia colgada no acumula sondas.
- ``/docs`` y ``/openapi.json`` (NFR-NUC-23): ``not_found`` en ``pilot`` y ``staging-<n>``.
- ``AppConfig``: modelo estricto leído una vez del entorno.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vigia_contracts.clock import SystemClock

from tests.api_support import Mode, World, config
from vigia_platform.shared.api.app import (
    STARTUP_FAILURE_EXIT_CODE,
    AppConfig,
    StartupSupervisor,
)
from vigia_platform.shared.api.errors import ApiErrorBody
from vigia_platform.shared.api.health import READINESS_BUDGET_SECONDS
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION
from vigia_platform.shared.signing.keys import SigningPurpose

WALL_CLOCK = SystemClock()
"""Reloj real para medir duraciones de verdad."""
WAIT_SECONDS = 10.0


def _supervisor(app: Any) -> StartupSupervisor:
    supervisor = app.state.vigia_readiness
    assert isinstance(supervisor, StartupSupervisor)
    return supervisor


def _wait(predicate: Any) -> None:
    deadline = WALL_CLOCK.monotonic() + WAIT_SECONDS
    while not predicate():
        if WALL_CLOCK.monotonic() > deadline:
            raise AssertionError("la condición no se cumplió a tiempo")
        time.sleep(0.01)


def _started(app: Any) -> None:
    _wait(lambda: _supervisor(app).started)


def _generic(response: Any, code: str) -> ApiErrorBody:
    body = ApiErrorBody.model_validate_json(response.content)
    assert body.code == code
    assert body.correlation_id.version == 7
    return body


# --- Arranque ----------------------------------------------------------------------------------


def test_everything_up_the_process_becomes_ready_after_the_fixed_checks() -> None:
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        assert client.get("/health/live").json() == {"status": "live"}
        ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json() == {"status": "ready"}
    assert ready.headers["cache-control"] == "no-store"
    assert world.signing.ready and world.signing.refreshes_started == 1
    assert world.synchronized == ["registries"]
    assert world.kms.generate_calls == 1 and world.kms.decrypt_calls == 1
    assert world.exits == []


def test_live_answers_and_ready_refuses_while_startup_has_not_finished() -> None:
    world = World()
    world.signing.mode = Mode.HUNG
    app = world.app()
    with TestClient(app) as client:
        _wait(lambda: world.signing.in_flight == 1)
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        _generic(ready, "temporarily_unavailable")
        assert ready.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    "breaks",
    [
        pytest.param(lambda w: setattr(w.signing, "mode", Mode.DOWN), id="gestor-de-secretos"),
        pytest.param(lambda w: setattr(w.database, "mode", Mode.DOWN), id="base"),
        pytest.param(lambda w: setattr(w.storage, "present", False), id="sin-centinela"),
        pytest.param(lambda w: setattr(w.kms, "down", True), id="kms"),
        pytest.param(lambda w: setattr(w, "registry_failures", 10**6), id="registros"),
        pytest.param(
            lambda w: setattr(w.database, "schema_version", MINIMUM_SCHEMA_VERSION - 1),
            id="esquema-viejo",
        ),
        pytest.param(lambda w: setattr(w.database, "schema_version", None), id="sin-migrar"),
        pytest.param(
            lambda w: setattr(w.database, "visible_organizations", 1), id="rls-sin-efecto"
        ),
        pytest.param(
            lambda w: w.signing.missing.add(SigningPurpose.CHECKPOINT), id="falta-una-clave"
        ),
    ],
)
def test_a_failing_startup_check_never_becomes_ready_and_exits_after_the_deadline(
    breaks: Any,
) -> None:
    world = World()
    breaks(world)
    app = world.app()
    with TestClient(app) as client:
        _wait(lambda: world.exits != [])
        assert world.exits == [STARTUP_FAILURE_EXIT_CODE]
        assert _supervisor(app).failed and not _supervisor(app).started
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
    # Se reintentó cada 5 s hasta agotar los 60 s, sin pasarse del plazo.
    assert sum(world.sleeps) == pytest.approx(60.0)
    assert all(seconds <= 5.0 for seconds in world.sleeps)
    assert world.signing.refreshes_started == 0


def test_a_check_that_recovers_within_the_deadline_lets_the_process_start() -> None:
    world = World(registry_failures=3)
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        assert client.get("/health/ready").status_code == 200
    assert world.exits == [] and world.sleeps == [5.0, 5.0, 5.0]
    assert world.synchronized == ["registries"]


def test_registries_synchronize_only_once() -> None:
    world = World()
    world.storage.present = False
    app = world.app(startup_deadline_seconds=12.0)
    with TestClient(app):
        _wait(lambda: world.exits != [])
    assert world.synchronized == ["registries"]


# --- Salud tras arrancar -----------------------------------------------------------------------


@pytest.mark.parametrize("dependency", ["database", "storage"])
@pytest.mark.parametrize("mode", [Mode.DOWN, Mode.HUNG])
def test_ready_fails_within_two_seconds_and_live_answers_when_a_dependency_falls(
    dependency: str, mode: Mode
) -> None:
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        assert client.get("/health/ready").status_code == 200
        getattr(world, dependency).mode = mode
        start = WALL_CLOCK.monotonic()
        ready = client.get("/health/ready")
        elapsed = WALL_CLOCK.monotonic() - start
        live = client.get("/health/live")
        getattr(world, dependency).mode = Mode.UP
    assert ready.status_code == 503 and elapsed < READINESS_BUDGET_SECONDS
    body = _generic(ready, "temporarily_unavailable")
    assert dependency not in body.message_es
    assert live.status_code == 200 and live.json() == {"status": "live"}


def test_a_hung_dependency_does_not_accumulate_probes() -> None:
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        world.storage.mode = Mode.HUNG
        world.database.mode = Mode.HUNG
        for _ in range(4):
            assert client.get("/health/ready").status_code == 503
        assert world.storage.in_flight == 1 and world.database.in_flight == 1
        world.storage.mode = world.database.mode = Mode.UP


@pytest.mark.parametrize(
    "breaks",
    [
        pytest.param(lambda w: setattr(w.database, "visible_organizations", 3), id="rls"),
        pytest.param(lambda w: setattr(w.database, "schema_version", 0), id="esquema"),
        pytest.param(lambda w: setattr(w.storage, "present", False), id="centinela"),
        pytest.param(lambda w: w.signing.missing.add(SigningPurpose.GATE), id="clave"),
    ],
)
def test_ready_fails_when_a_check_fails_after_startup(breaks: Any) -> None:
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        breaks(world)
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200


def test_ready_recovers_when_the_dependency_comes_back() -> None:
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        world.database.mode = Mode.DOWN
        assert client.get("/health/ready").status_code == 503
        world.database.mode = Mode.UP
        assert client.get("/health/ready").status_code == 200


def test_ready_never_calls_the_secrets_manager() -> None:
    """Con el gestor caído en operación se sigue firmando con lo de memoria (FS-NUC-05 b): la
    salud comprueba las claves en memoria, no el gestor."""
    world = World()
    app = world.app()
    with TestClient(app) as client:
        _started(app)
        calls = world.signing.calls
        world.signing.mode = Mode.DOWN
        assert client.get("/health/ready").status_code == 200
    assert world.signing.calls == calls


# --- /docs y /openapi.json --------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["pilot", "staging-1", "staging-42"])
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"])
def test_docs_answer_not_found_in_production_configuration(environment: str, path: str) -> None:
    with TestClient(World().app(environment)) as client:
        response = client.get(path)
    assert response.status_code == 404
    _generic(response, "not_found")


@pytest.mark.parametrize("environment", ["local", "test"])
def test_docs_exist_outside_production(environment: str) -> None:
    with TestClient(World().app(environment)) as client:
        assert client.get("/docs").status_code == 200
        spec = client.get("/openapi.json")
    assert spec.status_code == 200 and "/health/live" in spec.json()["paths"]
    assert "/health/ready" not in spec.json()["paths"]


# --- Configuración -----------------------------------------------------------------------------


def test_config_is_read_once_from_the_environment() -> None:
    loaded = AppConfig.from_environ(
        {
            "VIGIA_ENVIRONMENT": "pilot",
            "VIGIA_SECRETS_KEY_ARN": "arn:aws:kms:us-east-1:000000000000:key/abc-123",
            "VIGIA_HEALTH_SENTINEL_KEY": "health/sentinel",
        }
    )
    assert loaded.environment == "pilot" and not loaded.docs_enabled
    assert loaded.health_sentinel_key == "health/sentinel"
    assert AppConfig.from_environ(
        {"VIGIA_ENVIRONMENT": "local", "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets"}
    ).docs_enabled


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"VIGIA_ENVIRONMENT": "production", "VIGIA_SECRETS_KEY_ARN": "alias/x"},
        {"VIGIA_ENVIRONMENT": "staging-0", "VIGIA_SECRETS_KEY_ARN": "alias/x"},
        {"VIGIA_ENVIRONMENT": "Local", "VIGIA_SECRETS_KEY_ARN": "alias/x"},
        {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_SECRETS_KEY_ARN": ""},
        {"VIGIA_ENVIRONMENT": "pilot", "VIGIA_SECRETS_KEY_ARN": "alias/x y"},
        {
            "VIGIA_ENVIRONMENT": "pilot",
            "VIGIA_SECRETS_KEY_ARN": "alias/x",
            "VIGIA_HEALTH_SENTINEL_KEY": "../fuera",
        },
    ],
)
def test_config_rejects_invalid_environment(environ: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AppConfig.from_environ(environ)


def test_config_is_strict_and_frozen() -> None:
    with pytest.raises(ValidationError):
        AppConfig(**{**config().model_dump(), "extra": 1})
    with pytest.raises(ValidationError):
        AppConfig(**{**config().model_dump(), "startup_deadline_seconds": "60"})
    with pytest.raises(ValidationError):
        config().environment = "pilot"  # type: ignore[misc]
