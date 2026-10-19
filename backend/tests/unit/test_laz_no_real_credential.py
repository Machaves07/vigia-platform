"""NFR-LAZ-47 y NFR-LAZ-26 · ninguna prueba con credencial real y ningún adaptador en
``vigia-api`` (TASK-316; LC-LAZ-14; PAT-LAZ-SEG-08).

- **Prueba de arranque**: ``tests/conftest.py`` (``pytest_configure``) detiene la suite si el
  entorno trae una credencial del proveedor (``ANTHROPIC_API_KEY`` o ``ANTHROPIC_AUTH_TOKEN``).
  Aquí se lanza pytest en un subproceso con una credencial sintética y se comprueba que **no
  arranca** (código 4, error de uso) sin ejecutar ninguna prueba; sin ella, la misma orden pasa.
- **Ningún adaptador en ``vigia-api``**: en un proceso limpio, importar el arranque de
  ``vigia-api`` y montar todos los enrutadores de U-04 **y componer el consumidor
  ``assistant_worker``** (lo que hace la composición común en los dos procesos) no importa
  ``vigia_platform.loop.adapters.assistant`` ni el paquete del proveedor: el adaptador se elige en
  la primera entrega, que solo ocurre en ``vigia-worker`` (``AssistantProviderOnFirstUse``).
- ``FakeAssistantProvider`` es el valor por omisión sin clave: ``configured`` es falso.

Solo datos generados: la «credencial» es una cadena sintética.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest

from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider
from vigia_platform.loop.assistant.port import (
    PROVIDER_CREDENTIAL_VARIABLES,
    provider_credentials_present,
)
from vigia_platform.loop.registration import AssistantProviderOnFirstUse

BACKEND: Final = Path(__file__).resolve().parents[2]
SYNTHETIC: Final = "sk-ant-sintetica-" + "0" * 24
_TARGET: Final = "tests/unit/test_laz_no_real_credential.py::test_the_environment_has_no_credential"


def _pytest(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _TARGET],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _clean_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name not in PROVIDER_CREDENTIAL_VARIABLES
    }


def test_the_environment_has_no_credential() -> None:
    assert provider_credentials_present(os.environ) == ()


@pytest.mark.parametrize("variable", PROVIDER_CREDENTIAL_VARIABLES)
def test_the_suite_does_not_start_with_a_real_credential(variable: str) -> None:
    started = _pytest(_clean_environment())
    assert started.returncode == 0, started.stdout + started.stderr
    refused = _pytest({**_clean_environment(), variable: SYNTHETIC})
    assert refused.returncode == pytest.ExitCode.USAGE_ERROR, refused.stdout + refused.stderr
    assert variable in refused.stderr
    assert SYNTHETIC not in refused.stdout + refused.stderr  # el valor nunca se imprime
    assert "passed" not in refused.stdout


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, ()),
        ({"ANTHROPIC_API_KEY": ""}, ()),
        ({"ANTHROPIC_API_KEY": "   "}, ()),
        ({"ANTHROPIC_API_KEY": SYNTHETIC}, ("ANTHROPIC_API_KEY",)),
        ({"ANTHROPIC_AUTH_TOKEN": SYNTHETIC}, ("ANTHROPIC_AUTH_TOKEN",)),
        ({"OTRA_CLAVE": SYNTHETIC}, ()),
    ],
)
def test_which_variables_count_as_a_credential(
    environ: dict[str, str], expected: tuple[str, ...]
) -> None:
    assert provider_credentials_present(environ) == expected


_API_IMPORTS: Final = textwrap.dedent(
    """
    import sys
    from types import SimpleNamespace

    import vigia_platform.shared.runtime.api  # noqa: F401 - el arranque de vigia-api
    from vigia_platform.accreditation.registration import accreditation_routers
    from vigia_platform.loop.registration import CONSUMER_REGISTRARS, loop_routers
    from vigia_platform.shared.outbox.registries import ConsumerRegistry
    from vigia_platform.ledger.free_text import FreeTextPolicyRegistry

    loop_routers()
    accreditation_routers()
    registry = ConsumerRegistry()
    services = SimpleNamespace(
        database=object(), free_text=FreeTextPolicyRegistry(), clock=object()
    )
    for register in CONSUMER_REGISTRARS:
        if register.__name__ == "_register_assistant_worker":
            register(registry, services)
    assert registry.get("assistant_worker") is not None
    loaded = sorted(
        name
        for name in sys.modules
        if name.startswith("vigia_platform.loop.adapters.assistant")
        or name == "anthropic"
        or name.startswith("anthropic.")
    )
    print("LOADED=" + ",".join(loaded))
    """
)


def test_vigia_api_imports_no_assistant_adapter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _API_IMPORTS],
        cwd=BACKEND,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED=\n" in result.stdout, result.stdout


def test_the_provider_is_chosen_on_first_use_and_is_the_fake_without_a_key() -> None:
    provider = AssistantProviderOnFirstUse()
    assert not provider.resolved
    assert provider.configured is False
    assert provider.resolved
    assert FakeAssistantProvider.unconfigured().configured is False
    assert FakeAssistantProvider().configured is True
