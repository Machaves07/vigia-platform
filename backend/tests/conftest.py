"""Perfiles de Hypothesis y semillas de las pruebas (NFR-NUC-46, herencia de NFR-CTR-30).

- ``ci`` (por defecto): 200 ejemplos por propiedad; cada propiedad corre dos veces, con la
  semilla fija ``CI_FIXED_SEED`` y con una semilla aleatoria de la sesión.
- ``nightly``: 2 000 ejemplos por propiedad con la semilla aleatoria de la sesión.

El perfil se elige con ``--hypothesis-profile=ci|nightly``. Las semillas se imprimen en la
cabecera y en el resumen; un fallo se reproduce con ``--hypothesis-seed=<semilla>``.

Las pruebas marcadas ``nightly`` se omiten salvo con ``--hypothesis-profile=nightly``: el mismo
comando con una ruta explícita sirve en los dos perfiles y nunca pasa sin ejecutar nada. Las
marcadas ``integration`` necesitan Docker (testcontainers) y se seleccionan con ``-m``.
"""

from __future__ import annotations

import secrets
from collections.abc import Hashable, Iterator
from typing import Any

import pytest
from hypothesis import is_hypothesis_test, seed, settings

CI_PROFILE = "ci"
NIGHTLY_PROFILE = "nightly"
CI_FIXED_SEED = 20260929

settings.register_profile(CI_PROFILE, max_examples=200, deadline=None, print_blob=True)
settings.register_profile(NIGHTLY_PROFILE, max_examples=2000, deadline=None, print_blob=True)

_MISSING = object()
_SEED_ATTRIBUTES = ("_hypothesis_internal_use_seed", "_hypothesis_internal_use_settings")
_session_seed: Hashable = 0


def _active_profile() -> str:
    return settings.get_current_profile_name()


def pytest_configure(config: pytest.Config) -> None:
    """Carga ``ci`` si no se pidió perfil y fija la semilla aleatoria de la sesión."""
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
    if _active_profile() == CI_PROFILE:
        return [CI_FIXED_SEED, _session_seed]
    return [_session_seed]


def _seed_banner() -> str:
    profile = _active_profile()
    if profile == CI_PROFILE:
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
    """Parametriza cada propiedad con las semillas del perfil activo."""
    if is_hypothesis_test(metafunc.function) and "_hypothesis_seed" in metafunc.fixturenames:
        seeds = _seeds_for_profile()
        metafunc.parametrize(
            "_hypothesis_seed", seeds, indirect=True, ids=[f"seed={s}" for s in seeds]
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
