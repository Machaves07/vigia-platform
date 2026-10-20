"""TASK-344 · el adaptador ``loop.adapters.anthropic`` solo en ``vigia-worker`` y solo con clave
(NFR-LAZ-26; LC-LAZ-14; PAT-LAZ-SEG-08).

- **Importación**: en un proceso limpio, el arranque de ``vigia-api`` con todos sus enrutadores
  y la composición del consumidor ``assistant_worker`` **no importan** el adaptador ni el
  paquete ``anthropic``, **aunque el entorno traiga una clave** (sintética): el adaptador se
  elige en la primera entrega, que solo ocurre en ``vigia-worker``. Al entregar con clave, sí.
- **Estático**: ningún módulo de ``src`` importa el paquete ``anthropic`` ni el adaptador salvo
  el propio adaptador; ``loop.registration`` lo nombra solo como cadena para ``importlib``.
- **Registro**: sin clave (o con una en blanco), ``FakeAssistantProvider.unconfigured()``; con
  ``ANTHROPIC_API_KEY`` o ``ANTHROPIC_AUTH_TOKEN``, ``AnthropicAssistantProvider`` construido por
  la fábrica de U-04 (con el destino de la configuración) y con el modelo de
  ``VIGIA_ASSISTANT_MODEL``; se elige **una vez** por proceso.
- **Sin red**: la fixture ``no_network`` hace fallar cualquier conexión saliente.

Solo datos generados: la clave es una cadena sintética, nunca una real.
"""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from tests.anthropic_support import SYNTHETIC_KEY, context, no_network
from tests.runtime_support import runtime_environ
from vigia_platform.loop.adapters.anthropic.client import (
    CODE_PATH,
    DEFAULT_MODEL,
    AnthropicAssistantProvider,
)
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider
from vigia_platform.loop.assistant.port import (
    PROVIDER_CREDENTIAL_VARIABLES,
    AssistantFailure,
    AssistantOutcome,
    AssistantProviderPort,
    FailureKind,
)
from vigia_platform.loop.http.client_factory import EgressBlocked, HttpClientFactory
from vigia_platform.loop.registration import AssistantProviderOnFirstUse, assistant_provider
from vigia_platform.shared.runtime.config import RuntimeConfig

pytestmark = pytest.mark.usefixtures("no_network")
__all__ = ["no_network"]

BACKEND: Final = Path(__file__).resolve().parents[2]
SOURCE: Final = BACKEND / "src" / "vigia_platform"
ADAPTER_PACKAGE: Final = "vigia_platform.loop.adapters.anthropic"


def _environment(**extra: str) -> dict[str, str]:
    clean = {k: v for k, v in os.environ.items() if k not in PROVIDER_CREDENTIAL_VARIABLES}
    return {**clean, **extra}


_COMPOSE: Final = textwrap.dedent(
    """
    import asyncio
    import sys
    from datetime import timedelta
    from types import SimpleNamespace

    import vigia_platform.shared.runtime.api  # noqa: F401 - el arranque de vigia-api
    from vigia_platform.accreditation.registration import accreditation_routers
    from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
    from vigia_platform.loop.registration import CONSUMER_REGISTRARS, loop_routers
    from vigia_platform.shared.outbox.registries import ConsumerRegistry

    def loaded():
        found = []
        if any(name == "anthropic" or name.startswith("anthropic.") for name in sys.modules):
            found.append("anthropic")
        if "vigia_platform.loop.adapters.anthropic.client" in sys.modules:
            found.append("adapter")
        return ",".join(found)

    loop_routers()
    accreditation_routers()
    registry = ConsumerRegistry()
    services = SimpleNamespace(
        database=object(), free_text=FreeTextPolicyRegistry(), clock=object(), config=None
    )
    for register in CONSUMER_REGISTRARS:
        if register.__name__ == "_register_assistant_worker":
            register(registry, services)
    assert registry.get("assistant_worker") is not None
    print("API=" + loaded())

    # La primera entrega (solo vigia-worker): el adaptador se elige aquí.
    from vigia_platform.loop.registration import AssistantProviderOnFirstUse, assistant_provider
    provider = AssistantProviderOnFirstUse(lambda: assistant_provider(services))
    print("CONFIGURED=" + str(provider.configured))
    print("WORKER=" + loaded())
    """
)


def _compose(environment: dict[str, str]) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _COMPOSE],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert SYNTHETIC_KEY not in result.stdout + result.stderr
    return result.stdout


def test_vigia_api_never_imports_the_adapter_even_with_a_key() -> None:
    output = _compose(_environment(ANTHROPIC_API_KEY=SYNTHETIC_KEY))
    assert "API=\n" in output, output
    assert "CONFIGURED=True" in output
    assert "WORKER=anthropic,adapter\n" in output, output


def test_without_a_key_not_even_the_first_delivery_imports_the_adapter() -> None:
    output = _compose(_environment())
    assert "API=\n" in output and "CONFIGURED=False" in output and "WORKER=\n" in output, output


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
    return names


def test_no_module_imports_the_package_or_the_adapter_but_the_adapter() -> None:
    adapter_dir = SOURCE / "loop" / "adapters" / "anthropic"
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        if adapter_dir in path.parents:
            continue
        for name in _imports(path):
            if name == "anthropic" or name.startswith(("anthropic.", ADAPTER_PACKAGE)):
                offenders.append(f"{path.relative_to(SOURCE)}: {name}")
    assert offenders == []
    registration = (SOURCE / "loop" / "registration.py").read_text(encoding="utf-8")
    assert f'importlib.import_module("{ADAPTER_PACKAGE}.client")' in registration


# --- Registro condicional -------------------------------------------------------------------------


def _services(**variables: str) -> Any:
    config = RuntimeConfig.from_environ(runtime_environ(**variables)) if variables else None
    return SimpleNamespace(config=config)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"ANTHROPIC_API_KEY": ""},
        {"ANTHROPIC_API_KEY": "   "},
        {"ANTHROPIC_AUTH_TOKEN": "\t"},
        {"OTRA_CLAVE": SYNTHETIC_KEY},
    ],
)
def test_without_a_key_the_registration_leaves_the_unconfigured_fake(
    environ: dict[str, str],
) -> None:
    provider = assistant_provider(_services(), environ)
    assert isinstance(provider, FakeAssistantProvider)
    assert provider.configured is False


class Spy:
    """Registra cada cliente que construye la fábrica (ruta de código y opciones)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.built: list[tuple[str, str, dict[str, Any]]] = []
        original = HttpClientFactory.assistant_client
        spy = self

        def assistant_client(
            factory: HttpClientFactory, constructor: Any, code_path: str, **options: Any
        ) -> Any:
            spy.built.append((factory.allowlist.assistant_host, code_path, options))
            return original(factory, constructor, code_path, **options)

        monkeypatch.setattr(HttpClientFactory, "assistant_client", assistant_client)


@pytest.mark.parametrize(
    ("variable", "option"),
    [("ANTHROPIC_API_KEY", "api_key"), ("ANTHROPIC_AUTH_TOKEN", "auth_token")],
)
def test_with_a_key_the_registration_builds_the_adapter_through_the_factory(
    monkeypatch: pytest.MonkeyPatch, variable: str, option: str
) -> None:
    spy = Spy(monkeypatch)
    services = _services(VIGIA_ASSISTANT_API_HOST="llm.proveedor.example")
    provider = assistant_provider(services, {variable: f" {SYNTHETIC_KEY} "})
    assert isinstance(provider, AnthropicAssistantProvider)
    assert provider.configured is True and provider.model == DEFAULT_MODEL
    ((host, code_path, options),) = spy.built
    assert (host, code_path) == ("llm.proveedor.example", CODE_PATH)
    assert options[option] == SYNTHETIC_KEY  # sin los espacios del entorno
    assert (options["timeout"], options["max_retries"]) == (60.0, 0)


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5"])
def test_the_model_comes_from_the_configuration(model: str) -> None:
    services = _services(VIGIA_ASSISTANT_MODEL=model)
    provider = assistant_provider(services, {"ANTHROPIC_API_KEY": SYNTHETIC_KEY})
    assert isinstance(provider, AnthropicAssistantProvider) and provider.model == model


class Counting:
    """Un adaptador de prueba que cuenta cuántas veces se construye."""

    built = 0

    def __init__(self) -> None:
        Counting.built += 1

    @property
    def configured(self) -> bool:
        return True

    async def complete(self, context: Any, *, timeout: timedelta) -> AssistantOutcome:
        return AssistantFailure(kind=FailureKind.TRANSIENT)


def test_the_adapter_is_chosen_once_per_process() -> None:
    Counting.built = 0

    def build() -> AssistantProviderPort:
        return Counting()

    provider = AssistantProviderOnFirstUse(build)
    assert not provider.resolved and Counting.built == 0
    for _ in range(3):
        assert provider.configured is True
        asyncio.run(provider.complete(context(), timeout=timedelta(seconds=60)))
    assert Counting.built == 1


def test_the_registered_worker_reads_the_key_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SYNTHETIC_KEY)
    provider = AssistantProviderOnFirstUse(lambda: assistant_provider(_services()))
    assert provider.configured is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert provider.configured is True  # ya elegido: una vez por proceso
    assert assistant_provider(_services()).configured is False


def test_the_adapter_call_is_blocked_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []
    original = HttpClientFactory.assistant_client

    def keep(factory: HttpClientFactory, constructor: Any, code_path: str, **options: Any) -> Any:
        guarded = original(factory, constructor, code_path, **options)
        built.append(guarded)
        return guarded

    monkeypatch.setattr(HttpClientFactory, "assistant_client", keep)
    provider = assistant_provider(_services(), {"ANTHROPIC_API_KEY": SYNTHETIC_KEY})
    (guarded,) = built
    guarded._client.base_url = "https://otro.example"
    with pytest.raises(EgressBlocked):
        asyncio.run(provider.complete(context(), timeout=timedelta(seconds=60)))


def test_the_network_fixture_refuses_any_connection() -> None:
    with pytest.raises(AssertionError):
        socket.create_connection(("api.anthropic.com", 443), timeout=10)
    with pytest.raises(AssertionError):
        socket.getaddrinfo("api.anthropic.com", 443)
    with socket.socket() as raw, pytest.raises(AssertionError):
        raw.connect(("127.0.0.1", 9))
