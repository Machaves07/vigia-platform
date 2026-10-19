"""Perfiles de Hypothesis y semillas de las pruebas (NFR-NUC-46, herencia de NFR-CTR-30).

- ``ci`` (por defecto): 200 ejemplos por propiedad; cada propiedad corre dos veces, con la
  semilla fija ``CI_FIXED_SEED`` y con una semilla aleatoria de la sesión.
- ``pr``: el de los pull_request en ``ci.yml`` (main sigue con ``ci``): igual que ``ci`` (las dos
  semillas, los mismos identificadores y, por tanto, los mismos grupos y tiempos de pytest-split)
  pero con 50 ejemplos por propiedad.
- ``nightly``: 2 000 ejemplos por propiedad con la semilla aleatoria de la sesión.

El perfil se elige con ``--hypothesis-profile=ci|pr|nightly``. Las semillas se imprimen en la
cabecera y en el resumen; un fallo se reproduce con ``--hypothesis-seed=<semilla>``.

Las pruebas marcadas ``nightly`` se omiten salvo con ``--hypothesis-profile=nightly``: el mismo
comando con una ruta explícita sirve en los dos perfiles y nunca pasa sin ejecutar nada. Las
marcadas ``integration`` necesitan Docker (testcontainers) y se seleccionan con ``-m``.

Las fixtures de sesión ``postgres_endpoint`` y ``localstack_endpoint`` se declaran aquí, con los
generadores de ``tests/integration/conftest.py``, para que las pruebas de ``tests/integration/`` y
las propiedades de ``tests/properties/`` que usan la base compartan un solo contenedor.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import secrets
from collections.abc import Hashable, Iterator
from typing import Any

import pytest
from _pytest.logging import LogCaptureHandler
from hypothesis import is_hypothesis_test, seed, settings

from tests.integration.conftest import localstack_endpoint_session, postgres_endpoint_session
from vigia_platform.loop.assistant.port import provider_credentials_present

postgres_endpoint = pytest.fixture(scope="session", name="postgres_endpoint")(
    postgres_endpoint_session
)
localstack_endpoint = pytest.fixture(scope="session", name="localstack_endpoint")(
    localstack_endpoint_session
)

CI_PROFILE = "ci"
PR_PROFILE = "pr"
NIGHTLY_PROFILE = "nightly"
CI_PROFILES = frozenset({CI_PROFILE, PR_PROFILE})
"""Perfiles de CI: semilla fija y semilla de la sesión, y presupuestos de ``ci`` en las pruebas."""
CI_FIXED_SEED = 20260929
SESSION_SEED_ID = "seed=sesion"
"""Identificador estable de la parametrización con la semilla de la sesión (VIG-198)."""

settings.register_profile(CI_PROFILE, max_examples=200, deadline=None, print_blob=True)
settings.register_profile(PR_PROFILE, max_examples=50, deadline=None, print_blob=True)
settings.register_profile(NIGHTLY_PROFILE, max_examples=2000, deadline=None, print_blob=True)

_MISSING = object()
_SEED_ATTRIBUTES = ("_hypothesis_internal_use_seed", "_hypothesis_internal_use_settings")
_session_seed: Hashable = 0


def _active_profile() -> str:
    return settings.get_current_profile_name()


def pytest_configure(config: pytest.Config) -> None:
    """Carga ``ci`` si no se pidió perfil y fija la semilla aleatoria de la sesión.

    Antes que nada, la prueba de arranque de NFR-LAZ-47 (TASK-316): con una credencial del
    proveedor del asistente en el entorno, la suite **no arranca** (ninguna prueba usa una clave
    real ni llama a la interfaz del proveedor; el doble ``FakeAssistantProvider`` es el valor por
    omisión)."""
    present = provider_credentials_present(os.environ)
    if present:
        raise pytest.UsageError(
            "credencial real del proveedor del asistente en la configuración de pruebas"
            f" ({', '.join(present)}): ninguna prueba la usa (NFR-LAZ-47); quítala del entorno"
        )
    global _session_seed
    if not config.getoption("hypothesis_profile"):
        settings.load_profile(CI_PROFILE)
    requested = config.getoption("hypothesis_seed")
    if requested is None:
        _session_seed = secrets.randbits(32)
    else:
        try:
            _session_seed = int(requested)
        except ValueError:
            _session_seed = requested


def _seeds_for_profile() -> list[Hashable]:
    if _active_profile() in CI_PROFILES:
        return [CI_FIXED_SEED, _session_seed]
    return [_session_seed]


def _seed_banner() -> str:
    profile = _active_profile()
    if profile in CI_PROFILES:
        return (
            f"hypothesis perfil {profile!r}: semilla fija {CI_FIXED_SEED}, "
            f"semilla aleatoria {_session_seed} (reproducir: --hypothesis-seed={_session_seed})"
        )
    return (
        f"hypothesis perfil {profile!r}: semilla {_session_seed} "
        f"(reproducir: --hypothesis-seed={_session_seed})"
    )


def pytest_report_header(config: pytest.Config) -> str:
    return _seed_banner()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametriza cada propiedad con las semillas del perfil activo.

    El identificador de la semilla de la sesión es fijo (``seed=sesion``), no su valor: cambia en
    cada proceso, y los grupos de pytest-split de ``ci.yml`` (VIG-198) ordenan y reparten por
    identificador y buscan los tiempos de ``.test_durations`` por él. Con el valor en el
    identificador, cada grupo repartía distinto (pruebas en dos grupos y pruebas en ninguno). El
    valor se sigue imprimiendo en la cabecera y en el resumen para reproducir un fallo.
    """
    if is_hypothesis_test(metafunc.function) and "_hypothesis_seed" in metafunc.fixturenames:
        seeds = _seeds_for_profile()
        metafunc.parametrize(
            "_hypothesis_seed",
            seeds,
            indirect=True,
            ids=[f"seed={s}" if s == CI_FIXED_SEED else SESSION_SEED_ID for s in seeds],
        )


@pytest.fixture(autouse=True)
def _hypothesis_seed(request: pytest.FixtureRequest) -> Iterator[None]:
    """Aplica a la propiedad la semilla de su parametrización y la restaura después."""
    value: Hashable | None = getattr(request, "param", None)
    if value is None:
        yield
        return
    test: Any = getattr(request.function, "__func__", request.function)
    saved = {name: getattr(test, name, _MISSING) for name in _SEED_ATTRIBUTES}
    seed(value)(test)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                delattr(test, name)
            else:
                setattr(test, name, previous)


@pytest.fixture(autouse=True)
def _release_captured_logs_between_examples(request: pytest.FixtureRequest) -> Iterator[None]:
    """Suelta los registros que capturó pytest al terminar cada ejemplo de una propiedad.

    Los manejadores de captura de pytest guardan cada ``LogRecord`` hasta el final de la prueba, y
    un registro con ``exc_info`` retiene su traza: los marcos, la aplicación y el cliente de ese
    ejemplo. Con 2 000 ejemplos (perfil ``nightly``), una propiedad que hace fallar una ruta por
    ejemplo acumulaba ~8,5 MB por ejemplo hasta agotar la memoria del runner (VIG-190). Se vacía
    solo la lista de registros: el texto capturado se conserva para el informe de un fallo, y el
    ejemplo mínimo que reproduce Hypothesis al final conserva también los suyos."""
    test: Any = getattr(request.function, "__func__", request.function)
    if not is_hypothesis_test(test):
        yield
        return
    handle = test.hypothesis
    inner = handle.inner_test
    if inspect.iscoroutinefunction(inner):

        @functools.wraps(inner)
        async def released_async(*args: Any, **kwargs: Any) -> Any:
            try:
                return await inner(*args, **kwargs)
            finally:
                _drop_captured_records()

        handle.inner_test = released_async
    else:

        @functools.wraps(inner)
        def released(*args: Any, **kwargs: Any) -> Any:
            try:
                return inner(*args, **kwargs)
            finally:
                _drop_captured_records()

        handle.inner_test = released
    try:
        yield
    finally:
        handle.inner_test = inner


def _drop_captured_records() -> None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, LogCaptureHandler):
            handler.records.clear()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Omite las pruebas ``nightly`` salvo con ``--hypothesis-profile=nightly``."""
    if _active_profile() == NIGHTLY_PROFILE:
        return
    skip = pytest.mark.skip(reason="solo corre con --hypothesis-profile=nightly")
    for item in items:
        if item.get_closest_marker("nightly") is not None:
            item.add_marker(skip)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    terminalreporter.write_line(_seed_banner())
