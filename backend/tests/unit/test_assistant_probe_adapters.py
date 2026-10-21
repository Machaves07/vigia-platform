"""VIG-272 · la sonda del cortacircuito del asistente en los adaptadores de
``AssistantProviderPort`` (PAT-LAZ-RES-04; BR-NUC-79).

- **Adaptador real** (``AnthropicAssistantProvider.probe``): con el paquete real y el doble del
  transporte, la sonda es **un** ``GET /v1/models/<modelo configurado>`` sin cuerpo (ni contexto
  ni fichas), con el tope del intento (a lo sumo 60 s); sin tiempo no sale; responde ``None`` si
  el proveedor respondió y clasifica sus errores con la tabla de ``classify``.
- **Doble** (``FakeAssistantProvider.probe``): llamadas y sondas comparten la cadena de guiones;
  ``rate_limited`` y ``overloaded`` son transitorios, ``timeout`` espera como una llamada,
  ``bad_request`` es permanente y el resto responde. Una sonda no cuenta como llamada ni lleva
  contexto.
- ``AssistantProviderOnFirstUse`` delega la sonda en el adaptador elegido.

Ninguna prueba abre la red (``no_network``); solo datos generados.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx2
import pytest

from tests.anthropic_support import (
    SYNTHETIC_KEY,
    Built,
    Scripted,
    Transport,
    context,
    error_body,
    factory,
    no_network,
)
from vigia_platform.loop.adapters.anthropic.client import DEFAULT_MODEL, AnthropicAssistantProvider
from vigia_platform.loop.adapters.assistant.fake import FakeAssistantProvider, Script
from vigia_platform.loop.assistant.port import AssistantFailure, AssistantProviderPort, FailureKind
from vigia_platform.loop.registration import AssistantProviderOnFirstUse
from vigia_platform.shared.clock import SimulatedClock

pytestmark = pytest.mark.usefixtures("no_network")
__all__ = ["no_network"]

ATTEMPT: Final = timedelta(seconds=60)
MODEL_BODY: Final = {
    "id": DEFAULT_MODEL,
    "type": "model",
    "display_name": "Modelo sintético",
    "created_at": "2026-01-01T00:00:00Z",
}
T: Final = FailureKind.TRANSIENT
P: Final = FailureKind.PERMANENT


def _probe(monkeypatch: pytest.MonkeyPatch, script: Scripted) -> tuple[Any, Transport]:
    transport = Transport(script).install(monkeypatch)
    provider = AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY)
    return asyncio.run(provider.probe(timeout=ATTEMPT)), transport


# --- Adaptador real ------------------------------------------------------------------------------


def test_the_real_probe_is_one_get_of_the_configured_model_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, transport = _probe(monkeypatch, Scripted(body=MODEL_BODY))
    assert outcome is None
    (request,) = transport.requests
    assert (request.method, request.url.path) == ("GET", f"/v1/models/{DEFAULT_MODEL}")
    assert request.content == b""  # ni contexto ni fichas


@pytest.mark.parametrize(
    ("status", "kind"),
    [(529, T), (503, T), (500, T), (429, T), (408, T), (401, P), (403, P), (404, P), (402, P)],
)
def test_the_real_probe_classifies_errors_by_the_adapter_table(
    monkeypatch: pytest.MonkeyPatch, status: int, kind: FailureKind
) -> None:
    error_types = {401: "authentication_error", 403: "permission_error", 404: "not_found_error"}
    body = error_body(error_types.get(status, "api_error"))
    outcome, transport = _probe(monkeypatch, Scripted(status=status, body=body))
    assert isinstance(outcome, AssistantFailure)
    assert (outcome.kind, outcome.status_code) == (kind, status)
    assert len(transport.requests) == 1  # max_retries = 0: el paquete no reintenta


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (httpx2.ConnectError("conexión rechazada"), FailureKind.TRANSIENT),
        (httpx2.ReadTimeout("lectura agotada"), FailureKind.TIMEOUT),
    ],
)
def test_the_real_probe_classifies_transport_failures(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, kind: FailureKind
) -> None:
    outcome, transport = _probe(monkeypatch, Scripted(raises=raised))
    assert outcome == AssistantFailure(kind=kind)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("given", "sent"),
    [(timedelta(seconds=90), 60.0), (ATTEMPT, 60.0), (timedelta(seconds=12.5), 12.5)],
)
def test_the_real_probe_timeout_is_at_most_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch, given: timedelta, sent: float
) -> None:
    built = Built()
    provider = AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY, constructor=built)
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def retrieve(*args: Any, **kwargs: Any) -> object:
        seen.append((args, kwargs))
        return object()

    monkeypatch.setattr(built.client.models, "retrieve", retrieve)
    assert asyncio.run(provider.probe(timeout=given)) is None
    assert seen == [((DEFAULT_MODEL,), {"timeout": sent})]
    for left in (timedelta(0), timedelta(seconds=-1)):
        assert asyncio.run(provider.probe(timeout=left)) == AssistantFailure(
            kind=FailureKind.TIMEOUT
        )
    assert len(seen) == 1  # sin tiempo, no sale


def test_the_first_use_provider_delegates_the_probe() -> None:
    chosen = FakeAssistantProvider(Script.OVERLOADED, latency=timedelta(0))
    provider = AssistantProviderOnFirstUse(lambda: chosen)
    outcome = asyncio.run(provider.probe(timeout=ATTEMPT))
    assert outcome == AssistantFailure(kind=FailureKind.TRANSIENT, status_code=529)
    assert chosen.probe_timeouts == [ATTEMPT]
    assert isinstance(provider, AssistantProviderPort)


# --- Doble ----------------------------------------------------------------------------------------


class Waits:
    """``sleep`` inyectado del doble: anota la espera y avanza el reloj simulado."""

    def __init__(self, clock: SimulatedClock) -> None:
        self.clock = clock
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        self.clock.advance(seconds)


def _fake(scripts: Any, **changes: Any) -> tuple[FakeAssistantProvider, Waits]:
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    waits = Waits(clock)
    return FakeAssistantProvider(scripts, clock=clock, sleep=waits, **changes), waits


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (Script.OVERLOADED, AssistantFailure(kind=T, status_code=529)),
        (
            Script.RATE_LIMITED,
            AssistantFailure(kind=T, status_code=429, retry_after=timedelta(seconds=7)),
        ),
        (Script.BAD_REQUEST, AssistantFailure(kind=P, status_code=400)),
        (Script.VALID_WITH_CITATIONS, None),
        (Script.VALID_INSUFFICIENT_EVIDENCE, None),
        (Script.SCHEMA_INVALID, None),
        (Script.CITATION_OUTSIDE_LIST, None),
        (Script.REFUSAL, None),
        (Script.MAX_TOKENS, None),
    ],
)
def test_the_fake_probe_answers_by_its_script_without_a_call(
    script: Script, expected: AssistantFailure | None
) -> None:
    provider, waits = _fake(script)
    assert asyncio.run(provider.probe(timeout=ATTEMPT)) == expected
    assert provider.call_count == 0 and provider.contexts == []
    assert provider.probe_timeouts == [ATTEMPT]
    assert waits.seconds == [2.0]  # la latencia del doble, del reloj simulado


def test_the_fake_probe_of_a_hung_provider_waits_its_timeout() -> None:
    provider, waits = _fake(Script.TIMEOUT, hang_until=timedelta(seconds=90))
    first = asyncio.run(provider.probe(timeout=ATTEMPT))
    assert first == AssistantFailure(kind=FailureKind.TIMEOUT)
    assert asyncio.run(provider.probe(timeout=ATTEMPT)) is None  # a los 60 s quedan 30
    assert waits.seconds == [60.0, 30.0]


def test_calls_and_probes_share_the_script_chain() -> None:
    provider, _ = _fake([Script.OVERLOADED, Script.OVERLOADED, Script.VALID_WITH_CITATIONS])
    assert asyncio.run(provider.probe(timeout=ATTEMPT)) == AssistantFailure(kind=T, status_code=529)
    called = asyncio.run(provider.complete(context(), timeout=ATTEMPT))
    assert called == AssistantFailure(kind=T, status_code=529)
    assert asyncio.run(provider.probe(timeout=ATTEMPT)) is None
    assert provider.call_count == 1 and len(provider.probe_timeouts) == 2
