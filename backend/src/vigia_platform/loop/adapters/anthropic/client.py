"""``AnthropicAssistantProvider``: el adaptador real de ``AssistantProviderPort`` (LC-LAZ-14;
tech-stack-decisions U-04 §2.1 a §2.4; PAT-LAZ-SEG-01 y SEG-08; TASK-344).

Corre **solo en ``vigia-worker``**: ``loop.registration`` lo importa en la primera entrega y solo
si el proceso tiene la clave del proveedor (NFR-LAZ-26); ``vigia-api`` nunca lo importa. Sin
clave, el registro deja ``FakeAssistantProvider.unconfigured()``.

**Cliente** (§2.1, §2.4): ``AsyncAnthropic`` construido **una vez** por la fábrica de U-04
(``HttpClientFactory.assistant_client``, lista cerrada de destinos, LC-LAZ-39) con sus valores
por omisión de transporte (ningún objeto de ``httpx``: la serie 1.x usa ``httpx2``),
``timeout = 60`` s y ``max_retries = 0``: los reintentos son de la bandeja de salida.

**Petición** (``build_request``, §2.2 y §2.3), exactamente:

- ``model``: el de la configuración (``VIGIA_ASSISTANT_MODEL``: ``claude-opus-5`` por omisión o
  ``claude-sonnet-5``, sin sufijo de fecha);
- ``max_tokens``: 16 000; ``thinking``: ``{"type": "adaptive"}``;
- ``output_config``: ``{"effort": "medium"}``, al que el paquete añade ``format`` desde
  ``output_format`` = ``AssistantOutput`` (el modelo Pydantic estricto de
  ``loop.assistant.schema``);
- ``inference_geo``: ``"us"``, en el primer nivel;
- ``betas``: ``["server-side-fallback-2026-07-01"]`` con ``fallbacks``: ``"default"``;
- ``system``: un bloque con ``SYSTEM_INSTRUCTIONS_ES`` y ``cache_control: {"type":
  "ephemeral"}``; ``messages``: un mensaje de usuario con el contexto de la frontera
  (``canonical_json``), **después** del punto de caché;
- ``timeout``: el tope del intento, a lo sumo 60 s.

Sin ``temperature``, ``top_p`` ni ``top_k`` (retirados: 400) y sin prefijo de respuesta. La
llamada es ``client.beta.messages.with_raw_response.parse(...)``: ``inference_geo``, ``betas`` y
``fallbacks`` solo existen en el espacio ``beta`` del paquete, y la respuesta en bruto da las
cabeceras de límite de tasa (NFR-LAZ-42) y el identificador de la petición.

**Respuesta** (§2.3): el cuerpo se lee como JSON y se mira ``stop_reason`` **antes** que el
contenido; **nunca** se llama a ``parse()`` de la respuesta, que valida el texto contra el esquema
antes de devolver nada (``anthropic.lib._parse._response.parse_beta_response``) y fallaría con un
borrador truncado sin dejar ver el motivo de parada ni el uso. Con ``end_turn`` se toma el único
bloque de texto como ``content_json`` y lo valida el consumidor con ``parse_output`` (esquema
estricto, sin coerción); con cualquier otro motivo, el contenido no se lee. El modelo y el uso
(fichas, lectura de caché y región) son **los de la respuesta**: con el respaldo del servidor, el
modelo que respondió puede no ser el pedido (nota fechada de DE §2.11). Las cabeceras
``anthropic-ratelimit-*`` de cada respuesta (también de un error) fijan
``assistant_rate_limit_headroom_ratio`` (el margen más bajo de los cupos informados).

**Errores**: ``loop.adapters.anthropic.errors.classify``, por tipo. El adaptador nunca lanza por
un fallo del proveedor; ``EgressBlocked`` (destino fuera de la lista) sí sale: es un defecto.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, Final

import anthropic

from vigia_platform.loop.adapters.anthropic.errors import classify
from vigia_platform.loop.assistant.context import (
    SYSTEM_INSTRUCTIONS_ES,
    AssistantContext,
    canonical_json,
)
from vigia_platform.loop.assistant.port import (
    END_TURN,
    AssistantCompletion,
    AssistantFailure,
    AssistantOutcome,
    FailureKind,
    TokenUsage,
)
from vigia_platform.loop.assistant.schema import AssistantOutput
from vigia_platform.loop.http.client_factory import GuardedClient, HttpClientFactory
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics, get_loop_metrics
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "ALTERNATIVE_MODEL",
    "CLIENT_MAX_RETRIES",
    "CLIENT_TIMEOUT",
    "CODE_PATH",
    "DEFAULT_MODEL",
    "EFFORT",
    "FALLBACK_BETA",
    "INFERENCE_GEO",
    "MAX_TOKENS",
    "MODELS",
    "AnthropicAssistantProvider",
    "build_request",
    "rate_limit_headroom",
    "read_response",
]

_log = get_logger("loop.assistant.anthropic")

CODE_PATH: Final = "vigia_platform.loop.adapters.anthropic.client"
"""La ruta de código que la fábrica nombra si el cliente apunta fuera de la lista."""
DEFAULT_MODEL: Final = "claude-opus-5"
ALTERNATIVE_MODEL: Final = "claude-sonnet-5"
MODELS: Final = frozenset({DEFAULT_MODEL, ALTERNATIVE_MODEL})
"""Los dos modelos admitidos, **sin sufijo de fecha** (§2.2): elegirlos es decisión del dueño."""
MAX_TOKENS: Final = 16_000
EFFORT: Final = "medium"
INFERENCE_GEO: Final = "us"
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
CLIENT_TIMEOUT: Final = timedelta(seconds=60)
CLIENT_MAX_RETRIES: Final = 0

_RESPONSE_MODEL: Final = re.compile(r"[a-z0-9][a-z0-9.@_-]{0,127}")
"""Forma admitida del modelo que nombra la respuesta (se guarda en ``assistant_draft``)."""
_GEO: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_RATE_LIMIT_KINDS: Final = ("requests", "tokens", "input-tokens", "output-tokens")


def build_request(context: AssistantContext, *, model: str, timeout: timedelta) -> dict[str, Any]:
    """Los argumentos de ``messages.parse`` para ``context`` (tabla del módulo)."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "output_format": AssistantOutput,
        "inference_geo": INFERENCE_GEO,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": [
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTIONS_ES,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": canonical_json(context)}],
        "timeout": timeout.total_seconds(),
    }


def _tokens(usage: Mapping[str, Any], key: str) -> int | None:
    value = usage.get(key)
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= 2**40:
        return None
    return value


def _usage(raw: object) -> TokenUsage | None:
    """El bloque de uso de la respuesta; ``None`` si no tiene la forma esperada."""
    if not isinstance(raw, Mapping):
        return None
    counts = [_tokens(raw, key) for key in ("input_tokens", "output_tokens")]
    cache_read = _tokens(raw, "cache_read_input_tokens")
    if counts[0] is None or counts[1] is None or cache_read is None:
        return None
    geo = raw.get("inference_geo")
    return TokenUsage(
        input_tokens=counts[0],
        output_tokens=counts[1],
        cache_read_tokens=cache_read,
        inference_geo=geo if isinstance(geo, str) and _GEO.fullmatch(geo) else None,
    )


def _single_text(content: object) -> str | None:
    """El texto del único bloque ``text`` del contenido; ``None`` si no hay exactamente uno."""
    if not isinstance(content, list):
        return None
    texts = [
        block.get("text")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        return None
    return texts[0]


def read_response(body: object, *, request_id: str | None) -> AssistantOutcome:
    """El resultado de una respuesta 200: ``stop_reason`` primero; el contenido solo si es
    ``end_turn``. Un cuerpo sin la forma esperada es un fallo permanente (sin borrador)."""
    malformed = AssistantFailure(
        kind=FailureKind.PERMANENT, status_code=200, provider_request_id=request_id
    )
    if not isinstance(body, Mapping):
        return malformed
    stop_reason = body.get("stop_reason")
    model = body.get("model")
    usage = _usage(body.get("usage"))
    if (
        not isinstance(stop_reason, str)
        or not isinstance(model, str)
        or _RESPONSE_MODEL.fullmatch(model) is None
        or usage is None
    ):
        return malformed
    content: str | None = None
    if stop_reason == END_TURN:
        content = _single_text(body.get("content"))
    else:
        _log.warning("respuesta del proveedor sin borrador", stop_reason=stop_reason)
    return AssistantCompletion(
        stop_reason=stop_reason,
        content_json=content,
        model_name=model,
        model_version=model,
        usage=usage,
        provider_request_id=request_id,
    )


def rate_limit_headroom(headers: Mapping[str, str] | None) -> float | None:
    """El margen más bajo (``remaining / limit``, entre 0 y 1) de los cupos que informan las
    cabeceras ``anthropic-ratelimit-*``; ``None`` si no informan ninguno válido."""
    if headers is None:
        return None
    ratios: list[float] = []
    for kind in _RATE_LIMIT_KINDS:
        limit = headers.get(f"anthropic-ratelimit-{kind}-limit")
        remaining = headers.get(f"anthropic-ratelimit-{kind}-remaining")
        if limit is None or remaining is None or len(limit) > 20 or len(remaining) > 20:
            continue
        if not (limit.isdecimal() and remaining.isdecimal()) or int(limit) == 0:
            continue
        ratios.append(min(1.0, int(remaining) / int(limit)))
    return min(ratios) if ratios else None


class AnthropicAssistantProvider:
    """``AssistantProviderPort`` contra la interfaz del proveedor (solo ``vigia-worker``)."""

    def __init__(
        self,
        factory: HttpClientFactory,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        auth_token: str | None = None,
        metrics: LoopMetrics | None = None,
        constructor: Callable[..., anthropic.AsyncAnthropic] = anthropic.AsyncAnthropic,
    ) -> None:
        if model not in MODELS:
            raise ValueError("modelo del asistente fuera de la lista cerrada")
        if not (api_key or auth_token):
            raise ValueError("el adaptador real solo se construye con la clave del proveedor")
        self.model = model
        self._metrics = metrics if metrics is not None else get_loop_metrics()
        self._client: GuardedClient[anthropic.AsyncAnthropic] = factory.assistant_client(
            constructor,
            CODE_PATH,
            api_key=api_key,
            auth_token=auth_token,
            timeout=CLIENT_TIMEOUT.total_seconds(),
            max_retries=CLIENT_MAX_RETRIES,
        )

    @property
    def configured(self) -> bool:
        return True

    def _record_headroom(self, headers: Mapping[str, str] | None) -> None:
        headroom = rate_limit_headroom(headers)
        if headroom is not None:
            self._metrics.gauge(LoopMetricName.ASSISTANT_RATE_LIMIT_HEADROOM_RATIO).set(headroom)

    async def complete(self, context: AssistantContext, *, timeout: timedelta) -> AssistantOutcome:
        attempt = min(timeout, CLIENT_TIMEOUT)
        if attempt <= timedelta(0):
            return AssistantFailure(kind=FailureKind.TIMEOUT)
        request = build_request(context, model=self.model, timeout=attempt)
        try:
            async with asyncio.timeout(attempt.total_seconds()):
                raw = await self._client.call(
                    lambda client: client.beta.messages.with_raw_response.parse(**request)
                )
                self._record_headroom(raw.headers)
                body = await raw.json()
        except TimeoutError:
            return AssistantFailure(kind=FailureKind.TIMEOUT)
        except anthropic.APIStatusError as error:
            self._record_headroom(error.response.headers)
            return classify(error)
        except anthropic.AnthropicError as error:
            return classify(error)
        except ValueError:  # cuerpo que no es JSON (``json.JSONDecodeError``)
            return AssistantFailure(kind=FailureKind.PERMANENT, status_code=200)
        return read_response(body, request_id=raw.request_id)
