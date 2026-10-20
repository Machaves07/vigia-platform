"""Apoyo de las pruebas del adaptador ``loop.adapters.anthropic`` (TASK-344).

- ``no_network``: fixture que hace fallar **cualquier** conexión saliente (``socket.connect``,
  ``connect_ex``, ``create_connection`` y la resolución de nombres) durante la prueba: ninguna
  prueba del adaptador llega a la red del proveedor.
- ``context()``: un ``AssistantContext`` fijo de datos generados (la frontera de TASK-316).
- ``Built``: un constructor que crea el ``AsyncAnthropic`` **real** del paquete con las opciones
  que le da la fábrica y lo guarda, para mirar sus opciones y sustituir su ``parse`` o su
  transporte.
- ``Transport``: doble del transporte del paquete (``httpx2.AsyncClient.send``): guarda cada
  petición y responde lo guionizado (código, cabeceras y cuerpo JSON) o lanza la excepción de
  transporte guionizada. Así el paquete real construye la petición, interpreta los códigos y
  lanza sus excepciones tipadas sin salir de la máquina.

Ninguna credencial real: la clave es una cadena sintética que se pasa por argumento, nunca por
el entorno (``tests/conftest.py`` detiene la suite si el entorno trae una).
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import anthropic
import httpx2
import pytest

from vigia_platform.loop.assistant.context import AssistantContext, build_context
from vigia_platform.loop.http.client_factory import (
    DEFAULT_ASSISTANT_HOST,
    EgressAllowlist,
    HttpClientFactory,
    default_email_host,
)

SYNTHETIC_KEY: Final = "sk-ant-sintetica-" + "0" * 24
ALLOWLIST: Final = EgressAllowlist(
    assistant_host=DEFAULT_ASSISTANT_HOST, email_host=default_email_host("us-east-1")
)
REQUEST_ID: Final = "req_sintetica_0001"

ZONE_CONTEXT: Final[Mapping[str, Any]] = {
    "family": "coexistence",
    "tier": "tier_1",
    "observability_state_at_event": "observable",
    "signals_by_role": [{"role": "energy", "value": "asserted", "valid": True}],
    "episode_durations": {"episode_ms": 42_000, "condition_ms": 40_000},
    "standard_text": "Ninguna persona dentro del perímetro con la máquina energizada.",
    "period": {"from": "2026-09-01T00:00:00.000Z", "to": "2026-10-01T00:00:00.000Z"},
    "coverage_summary_period": {
        "observable_ms": 2_000_000_000,
        "degraded_ms": 10_000,
        "not_observable_ms": 0,
        "no_communication_ms": 0,
        "never_reported_ms": 0,
        "zone_not_active_ms": 0,
    },
    "node_state": [{"communication_state": "reachable"}],
    "finding_counts_by_family_in_period": [{"family": "coexistence", "count": 3}],
    "recurrences_in_period": 2,
    "previous_actions": [],
    "previous_cause_categories": [],
}


def context() -> AssistantContext:
    """El contexto de la frontera de una solicitud generada."""
    return build_context(
        ZONE_CONTEXT,
        request_id=uuid.UUID("01890000-0000-7000-8000-000000000001"),
        anchor_record_id=uuid.UUID("01890000-0000-7000-8000-000000000002"),
    ).context


def factory() -> HttpClientFactory:
    return HttpClientFactory(ALLOWLIST)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cualquier conexión saliente o resolución de nombres falla la prueba."""

    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("una prueba del adaptador intentó abrir la red")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class Built:
    """Constructor para la fábrica: el ``AsyncAnthropic`` real, guardado con sus opciones."""

    def __init__(self) -> None:
        self.options: list[dict[str, Any]] = []
        self.clients: list[anthropic.AsyncAnthropic] = []

    def __call__(self, **options: Any) -> anthropic.AsyncAnthropic:
        client = anthropic.AsyncAnthropic(**options)
        self.options.append(options)
        self.clients.append(client)
        return client

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        (client,) = self.clients
        return client


@dataclass
class Scripted:
    """Una respuesta guionizada del transporte (o la excepción que lanza)."""

    status: int = 200
    body: object = None
    headers: Mapping[str, str] = field(default_factory=dict)
    raises: Exception | None = None


class Transport:
    """Doble de ``httpx2.AsyncClient.send``: guarda cada petición y responde ``script``."""

    def __init__(self, script: Scripted) -> None:
        self.script = script
        self.requests: list[httpx2.Request] = []

    async def send(self, request: httpx2.Request, *_: Any, **__: Any) -> httpx2.Response:
        self.requests.append(request)
        if self.script.raises is not None:
            raise self.script.raises
        content = json.dumps(self.script.body).encode("utf-8")
        return httpx2.Response(
            self.script.status,
            headers={"content-type": "application/json", **self.script.headers},
            content=content,
            request=request,
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Transport:
        transport = self

        async def send(_client: Any, request: httpx2.Request, *args: Any, **kwargs: Any) -> Any:
            return await transport.send(request, *args, **kwargs)

        monkeypatch.setattr(httpx2.AsyncClient, "send", send)
        return self

    def body(self, index: int = -1) -> dict[str, Any]:
        loaded = json.loads(self.requests[index].content)
        assert isinstance(loaded, dict)
        return loaded


def message(
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5",
    content: object = None,
    usage: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """El cuerpo de una respuesta 200 de la interfaz de mensajes (datos generados)."""
    return {
        "id": "msg_sintetico_0001",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "content": content if content is not None else [],
        "usage": dict(
            usage
            if usage is not None
            else {
                "input_tokens": 1_234,
                "output_tokens": 321,
                "cache_read_input_tokens": 512,
                "cache_creation_input_tokens": 0,
                "inference_geo": "us",
            }
        ),
    }


def error_body(kind: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": "mensaje sintético"}}
