"""``FakeAssistantProvider``: el doble guionizado del proveedor del asistente (tech-stack-decisions
§2.5; NFR-LAZ-47; LC-LAZ-14; TASK-316).

Implementa ``AssistantProviderPort`` sin red, sin clave y sin gasto, y es el **adaptador por
omisión** en pruebas y cuando no hay clave (``FakeAssistantProvider.unconfigured()``: declara
``configured = False`` y el consumidor responde ``no_api_key`` sin llamarlo). Los diez guiones
(``Script``):

1. ``valid_with_citations``: borrador válido que cita los alias del contexto;
2. ``valid_insufficient_evidence``: borrador sin citas que declara qué falta;
3. ``schema_invalid``: contenido que no valida contra el esquema estricto;
4. ``citation_outside_list``: cita un alias que no está en la lista cerrada;
5. ``refusal``: rechazo del clasificador, **código 200** con ``stop_reason = refusal``;
6. ``max_tokens``: tope de fichas alcanzado (contenido truncado);
7. ``rate_limited``: 429 con la espera de la cabecera (``retry_after``);
8. ``overloaded``: 529;
9. ``timeout``: el proveedor no responde hasta ``hang_until`` (segundos desde la primera
   llamada): una llamada que empieza antes espera hasta entonces o hasta su tope;
10. ``bad_request``: 400 permanente.

Los guiones se encadenan por llamada (``scripts``: el último se repite). La espera simulada usa el
``sleep`` inyectado, el mismo temporizador que el consumidor (en pruebas, uno que avanza el reloj
simulado): **ningún tope de pared**. ``calls`` cuenta las llamadas y ``contexts`` guarda el
objeto de cada una, para comprobar que nada sale sin llamada y qué salió.
"""

from __future__ import annotations

import asyncio
import enum
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.loop.assistant.context import AssistantContext
from vigia_platform.loop.assistant.port import (
    END_TURN,
    MAX_TOKENS,
    REFUSAL,
    AssistantCompletion,
    AssistantFailure,
    AssistantOutcome,
    FailureKind,
    TokenUsage,
)
from vigia_platform.shared.clock import Clock

__all__ = [
    "FAKE_MODEL",
    "FAKE_MODEL_VERSION",
    "FakeAssistantProvider",
    "Script",
    "Sleep",
    "draft_for",
]

FAKE_MODEL: Final = "fake-assistant"
FAKE_MODEL_VERSION: Final = "fake-1"
FAKE_GEO: Final = "us"
FAKE_USAGE: Final = TokenUsage(input_tokens=1_200, output_tokens=300, inference_geo=FAKE_GEO)

type Sleep = Callable[[float], Awaitable[None]]


class Script(enum.StrEnum):
    """Los diez guiones de tech-stack-decisions §2.5."""

    VALID_WITH_CITATIONS = "valid_with_citations"
    VALID_INSUFFICIENT_EVIDENCE = "valid_insufficient_evidence"
    SCHEMA_INVALID = "schema_invalid"
    CITATION_OUTSIDE_LIST = "citation_outside_list"
    REFUSAL = "refusal"
    MAX_TOKENS = "max_tokens"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"


DRAFT_TEXT: Final = (
    "La señal de energía siguió activa mientras se registraba presencia en el perímetro: el"
    " procedimiento de bloqueo de la zona no exige comprobar la energía antes del acceso."
)
MISSING_TEXT: Final = (
    "Falta el registro de la secuencia de bloqueo de la máquina para proponer una causa: la"
    " evidencia de la zona no muestra si el procedimiento se aplicó."
)


def draft_for(
    context: AssistantContext,
    *,
    draft_text: str = DRAFT_TEXT,
    quoted_fact_es: str = "Señal de energía activa durante el episodio",
) -> str:
    """El contenido JSON de un borrador válido que cita el primer alias del contexto."""
    first = context.citable[0]
    return json.dumps(
        {
            "draft_text": draft_text,
            "citations": [
                {"kind": first.kind.value, "id": first.alias, "quoted_fact_es": quoted_fact_es}
            ],
            "insufficient_evidence_declared": False,
            "proposed_cause_category": "process",
        },
        ensure_ascii=False,
    )


@dataclass
class _Call:
    context: AssistantContext
    timeout: timedelta


class FakeAssistantProvider:
    """El doble de ``AssistantProviderPort`` (por omisión en pruebas y sin clave)."""

    def __init__(
        self,
        scripts: Sequence[Script] | Script = Script.VALID_WITH_CITATIONS,
        *,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        latency: timedelta = timedelta(seconds=2),
        retry_after: timedelta = timedelta(seconds=7),
        hang_until: timedelta = timedelta(seconds=90),
        content: str | None = None,
        configured: bool = True,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._scripts = (scripts,) if isinstance(scripts, Script) else tuple(scripts)
        if not self._scripts:
            raise ValueError("el doble necesita al menos un guion")
        self._clock = clock
        self._sleep: Sleep = sleep if sleep is not None else asyncio.sleep
        self._latency = latency
        self._retry_after = retry_after
        self._hang_until = hang_until
        self._content = content
        self._configured = configured
        self._gate = gate
        self._first_call_at: datetime | None = None
        self.calls: list[_Call] = []

    @classmethod
    def unconfigured(cls) -> FakeAssistantProvider:
        """El adaptador por omisión sin clave: nunca se llama (``no_api_key``)."""
        return cls(configured=False)

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def contexts(self) -> list[AssistantContext]:
        return [call.context for call in self.calls]

    def _script(self) -> Script:
        index = min(len(self.calls) - 1, len(self._scripts) - 1)
        return self._scripts[index]

    async def _wait(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleep(seconds)

    async def complete(self, context: AssistantContext, *, timeout: timedelta) -> AssistantOutcome:
        self.calls.append(_Call(context, timeout))
        if self._gate is not None:
            await self._gate.wait()
        script = self._script()
        if script is Script.TIMEOUT:
            return await self._hang(context, timeout)
        if self._latency >= timeout:
            await self._wait(timeout.total_seconds())
            return AssistantFailure(kind=FailureKind.TIMEOUT)
        await self._wait(self._latency.total_seconds())
        return self._respond(script, context)

    async def _hang(self, context: AssistantContext, timeout: timedelta) -> AssistantOutcome:
        """No responde hasta ``hang_until`` desde la primera llamada; después, un borrador."""
        if self._clock is None:
            await self._wait(timeout.total_seconds())
            return AssistantFailure(kind=FailureKind.TIMEOUT)
        now = self._clock.now()
        if self._first_call_at is None:
            self._first_call_at = now
        remaining = self._first_call_at + self._hang_until - now
        if remaining >= timeout:
            await self._wait(timeout.total_seconds())
            return AssistantFailure(kind=FailureKind.TIMEOUT)
        await self._wait(max(remaining, self._latency).total_seconds())
        return self._respond(Script.VALID_WITH_CITATIONS, context)

    def _respond(self, script: Script, context: AssistantContext) -> AssistantOutcome:
        completion = _completion(self._content)
        match script:
            case Script.VALID_WITH_CITATIONS:
                return completion(END_TURN, draft_for(context))
            case Script.VALID_INSUFFICIENT_EVIDENCE:
                return completion(
                    END_TURN,
                    json.dumps(
                        {
                            "draft_text": MISSING_TEXT,
                            "citations": [],
                            "insufficient_evidence_declared": True,
                            "proposed_cause_category": "process",
                        },
                        ensure_ascii=False,
                    ),
                )
            case Script.SCHEMA_INVALID:
                return completion(
                    END_TURN, json.dumps({"draft_text": DRAFT_TEXT, "confidence": "alta"})
                )
            case Script.CITATION_OUTSIDE_LIST:
                return completion(
                    END_TURN,
                    json.dumps(
                        {
                            "draft_text": DRAFT_TEXT,
                            "citations": [
                                {"kind": "record", "id": "F-00000000", "quoted_fact_es": "Hecho"}
                            ],
                            "insufficient_evidence_declared": False,
                            "proposed_cause_category": "engineering",
                        },
                        ensure_ascii=False,
                    ),
                )
            case Script.REFUSAL:
                return completion(REFUSAL, None)
            case Script.MAX_TOKENS:
                return completion(MAX_TOKENS, '{"draft_text": "La señal de ener')
            case Script.RATE_LIMITED:
                return AssistantFailure(
                    kind=FailureKind.TRANSIENT, status_code=429, retry_after=self._retry_after
                )
            case Script.OVERLOADED:
                return AssistantFailure(kind=FailureKind.TRANSIENT, status_code=529)
            case Script.BAD_REQUEST:
                return AssistantFailure(kind=FailureKind.PERMANENT, status_code=400)
            case Script.TIMEOUT:  # pragma: no cover - lo atiende ``_hang``
                return AssistantFailure(kind=FailureKind.TIMEOUT)


def _completion(override: str | None) -> Callable[[str, str | None], AssistantCompletion]:
    """Una respuesta 200 del doble; ``override`` sustituye el contenido de un ``end_turn``."""

    def build(stop_reason: str, content: str | None) -> AssistantCompletion:
        if override is not None and stop_reason == END_TURN:
            content = override
        return AssistantCompletion(
            stop_reason=stop_reason,
            content_json=content,
            model_name=FAKE_MODEL,
            model_version=FAKE_MODEL_VERSION,
            usage=FAKE_USAGE,
            provider_request_id="fake-request",
        )

    return build
