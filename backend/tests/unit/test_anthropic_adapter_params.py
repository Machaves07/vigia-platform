"""TASK-344 · la petición y la lectura de la respuesta del adaptador ``loop.adapters.anthropic``
(LC-LAZ-14; tech-stack-decisions U-04 §2.1 a §2.4).

- **Petición**: un doble del paquete sustituye ``messages.parse`` (en el espacio ``beta`` y con
  respuesta en bruto, el único que admite ``inference_geo``, ``betas`` y ``fallbacks``) y captura
  sus argumentos: son **exactamente** ``declared(model)`` (la tabla del PR). Con el doble del
  transporte, el paquete **real** pone cada parámetro en su nivel del cuerpo (``inference_geo`` y
  ``fallbacks`` en el primero, ``effort`` y ``format`` dentro de ``output_config``) y la cabecera
  ``anthropic-beta``; sin ``temperature``, ``top_p``, ``top_k`` ni prefijo de respuesta.
- **Cliente**: la fábrica de U-04 lo construye con ``timeout = 60`` y ``max_retries = 0``.
- **Modelo**: por configuración (``VIGIA_ASSISTANT_MODEL``), ``claude-opus-5`` por omisión o
  ``claude-sonnet-5``; un identificador con sufijo de fecha u otro modelo no se admite.
- **Respuesta**: ``stop_reason`` antes que el contenido: un rechazo (200 con ``refusal``) o un
  tope de fichas no tocan el contenido (que explota si se lee) y no producen borrador; una
  respuesta válida devuelve el modelo **de la respuesta** y su bloque de uso.
- **Margen de límite de tasa** (NFR-LAZ-42): las cabeceras de cada respuesta fijan
  ``assistant_rate_limit_headroom_ratio``.

Ninguna prueba abre la red (``no_network``); solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from datetime import timedelta
from typing import Any, Final

import anthropic
import pydantic
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.anthropic_support import (
    ALLOWLIST,
    REQUEST_ID,
    SYNTHETIC_KEY,
    Built,
    Scripted,
    Transport,
    context,
    factory,
    message,
    no_network,
)
from tests.runtime_support import runtime_environ
from vigia_platform.loop.adapters.anthropic.client import (
    CLIENT_MAX_RETRIES,
    CLIENT_TIMEOUT,
    CODE_PATH,
    DEFAULT_MODEL,
    MODELS,
    AnthropicAssistantProvider,
    build_request,
    rate_limit_headroom,
)
from vigia_platform.loop.assistant.context import SYSTEM_INSTRUCTIONS_ES, canonical_json
from vigia_platform.loop.assistant.port import (
    AssistantCompletion,
    AssistantFailure,
    FailureKind,
    TokenUsage,
    completion_failure,
)
from vigia_platform.loop.assistant.schema import AssistantOutput, parse_output
from vigia_platform.loop.http.client_factory import HttpClientFactory
from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics
from vigia_platform.shared.observability.metrics import METER_NAME
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid

pytestmark = pytest.mark.usefixtures("no_network")
__all__ = ["no_network"]

ATTEMPT: Final = timedelta(seconds=60)


def declared(model: str, *, timeout: float = 60.0) -> dict[str, Any]:
    """La tabla de tech-stack §2.2 a §2.4 tal como la declara el PR, escrita a mano."""
    return {
        "model": model,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "output_format": AssistantOutput,
        "inference_geo": "us",
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
        "system": [
            {
                "type": "text",
                "text": SYSTEM_INSTRUCTIONS_ES,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": canonical_json(context())}],
        "timeout": timeout,
    }


# --- Dobles del paquete ---------------------------------------------------------------------------


class Untouchable(list[object]):
    """Un contenido que falla si alguien lo lee."""

    def __iter__(self) -> Iterator[object]:
        raise AssertionError("se leyó el contenido antes que el motivo de parada")

    def __len__(self) -> int:
        raise AssertionError("se leyó el contenido antes que el motivo de parada")

    def __getitem__(self, index: Any) -> Any:
        raise AssertionError("se leyó el contenido antes que el motivo de parada")


class Raw:
    """Doble de la respuesta en bruto del paquete (``AsyncAPIResponse``)."""

    def __init__(self, body: object, headers: Mapping[str, str] | None = None) -> None:
        self._body = body
        self.headers = dict(headers or {})
        self.request_id = REQUEST_ID

    async def json(self) -> object:
        return self._body

    async def parse(self, **_: Any) -> Any:
        raise AssertionError("el adaptador nunca valida la respuesta con parse()")


class Parse:
    """Doble de ``messages.parse`` (con respuesta en bruto): guarda sus argumentos."""

    def __init__(self, raw: Raw) -> None:
        self.raw = raw
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Raw:
        self.calls.append(kwargs)
        return self.raw


def provider_with_parse(
    monkeypatch: pytest.MonkeyPatch,
    raw: Raw,
    *,
    model: str = DEFAULT_MODEL,
    metrics: LoopMetrics | None = None,
) -> tuple[AnthropicAssistantProvider, Parse, Built]:
    built = Built()
    provider = AnthropicAssistantProvider(
        factory(), model=model, api_key=SYNTHETIC_KEY, metrics=metrics, constructor=built
    )
    namespace = built.client.beta.messages.with_raw_response
    assert built.client.beta.messages.with_raw_response is namespace  # el mismo objeto
    double = Parse(raw)
    monkeypatch.setattr(namespace, "parse", double)
    return provider, double, built


def complete(provider: AnthropicAssistantProvider, timeout: timedelta = ATTEMPT) -> Any:
    return asyncio.run(provider.complete(context(), timeout=timeout))


def valid_text() -> str:
    first = context().citable[0]
    return json.dumps(
        {
            "draft_text": "La señal de energía siguió activa durante el episodio.",
            "citations": [{"kind": first.kind.value, "id": first.alias, "quoted_fact_es": "x"}],
            "insufficient_evidence_declared": False,
            "proposed_cause_category": "process",
        },
        ensure_ascii=False,
    )


# --- La petición ---------------------------------------------------------------------------------


@pytest.mark.parametrize("model", sorted(MODELS))
def test_the_request_is_exactly_the_declared_table(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    raw = Raw(message(content=[{"type": "text", "text": valid_text()}]))
    provider, parse, _ = provider_with_parse(monkeypatch, raw, model=model)
    complete(provider)
    (request,) = parse.calls
    assert request == declared(model)
    for retired in ("temperature", "top_p", "top_k", "output_format_json", "effort"):
        assert retired not in request
    assert [entry["role"] for entry in request["messages"]] == ["user"]  # sin prefijo


@pytest.mark.parametrize(
    ("given", "sent"),
    [(timedelta(seconds=90), 60.0), (CLIENT_TIMEOUT, 60.0), (timedelta(seconds=37.5), 37.5)],
)
def test_the_attempt_timeout_is_at_most_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch, given: timedelta, sent: float
) -> None:
    provider, parse, _ = provider_with_parse(monkeypatch, Raw(message()))
    complete(provider, given)
    (request,) = parse.calls
    assert request == declared(DEFAULT_MODEL, timeout=sent)
    assert build_request(context(), model=DEFAULT_MODEL, timeout=given)["timeout"] == (
        given.total_seconds()
    )


def test_an_attempt_without_time_left_does_not_call(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, parse, _ = provider_with_parse(monkeypatch, Raw(message()))
    for left in (timedelta(0), timedelta(seconds=-1)):
        assert complete(provider, left) == AssistantFailure(kind=FailureKind.TIMEOUT)
    assert parse.calls == []


def test_the_client_has_no_retries_and_a_sixty_second_timeout() -> None:
    built = Built()
    AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY, constructor=built)
    (options,) = built.options
    assert options == {
        "base_url": f"https://{ALLOWLIST.assistant_host}",
        "api_key": SYNTHETIC_KEY,
        "auth_token": None,
        "timeout": 60.0,
        "max_retries": 0,
    }
    assert built.client.max_retries == CLIENT_MAX_RETRIES == 0
    assert built.client.timeout == 60.0


def test_the_client_is_built_once_by_the_factory_with_the_adapter_code_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, dict[str, Any]]] = []
    original = HttpClientFactory.assistant_client

    def spy(self: HttpClientFactory, constructor: Any, code_path: str, **options: Any) -> Any:
        built.append((code_path, options))
        return original(self, constructor, code_path, **options)

    monkeypatch.setattr(HttpClientFactory, "assistant_client", spy)
    provider, parse, client = provider_with_parse(
        monkeypatch, Raw(message(content=[{"type": "text", "text": valid_text()}]))
    )
    complete(provider)
    complete(provider)
    assert [code for code, _ in built] == [CODE_PATH]
    assert len(client.clients) == 1 and len(parse.calls) == 2


def test_the_wire_request_puts_each_parameter_at_its_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = Transport(
        Scripted(body=message(content=[{"type": "text", "text": valid_text()}]))
    ).install(monkeypatch)
    provider = AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY)
    outcome = complete(provider)
    assert isinstance(outcome, AssistantCompletion)
    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url.host == ALLOWLIST.assistant_host
    assert request.url.path == "/v1/messages"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert request.headers["x-api-key"] == SYNTHETIC_KEY
    body = transport.body()
    assert set(body) == {
        "model",
        "max_tokens",
        "thinking",
        "output_config",
        "inference_geo",
        "fallbacks",
        "system",
        "messages",
    }
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 16000
    assert body["thinking"] == {"type": "adaptive"}
    assert body["inference_geo"] == "us"
    assert body["fallbacks"] == "default"
    assert set(body["output_config"]) == {"effort", "format"}
    assert body["output_config"]["effort"] == "medium"
    schema_format = body["output_config"]["format"]
    assert schema_format["type"] == "json_schema"
    schema = schema_format["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(AssistantOutput.model_fields)
    assert body["system"] == [
        {"type": "text", "text": SYSTEM_INSTRUCTIONS_ES, "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": canonical_json(context())}]


# --- El modelo, por configuración -----------------------------------------------------------------


@pytest.mark.parametrize("model", sorted(MODELS))
def test_the_model_variable_accepts_the_two_models(model: str) -> None:
    config = RuntimeConfig.from_environ(runtime_environ(VIGIA_ASSISTANT_MODEL=model))
    assert config.assistant_model == model
    assert RuntimeConfig.from_environ(runtime_environ()).assistant_model is None


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-20260401",
        "claude-sonnet-5-20260401",
        "claude-opus-4-8",
        "claude-haiku-4-5",
        "Claude-Opus-5",
        " claude-opus-5",
        "claude-opus-5\n",
        "claude-opus-5x",
        "gpt-5",
    ],
)
def test_a_dated_or_other_model_is_refused(model: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as error:
        RuntimeConfig.from_environ(runtime_environ(VIGIA_ASSISTANT_MODEL=model))
    assert error.value.variable == "VIGIA_ASSISTANT_MODEL"
    with pytest.raises(ValueError):
        AnthropicAssistantProvider(factory(), model=model, api_key=SYNTHETIC_KEY)


def test_the_adapter_is_not_built_without_a_key() -> None:
    for key in (None, ""):
        with pytest.raises(ValueError):
            AnthropicAssistantProvider(factory(), api_key=key, constructor=Built())


# --- La respuesta: motivo de parada antes que el contenido ----------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "kind"),
    [("refusal", FailureKind.REFUSAL), ("max_tokens", FailureKind.BUDGET_EXCEEDED)],
)
def test_a_refusal_or_a_token_cap_never_reads_the_content(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, kind: FailureKind
) -> None:
    body = message(stop_reason=stop_reason, content=Untouchable())
    provider, _, _ = provider_with_parse(monkeypatch, Raw(body))
    outcome = complete(provider)
    assert isinstance(outcome, AssistantCompletion)
    assert outcome.stop_reason == stop_reason
    assert outcome.content_json is None
    assert outcome.usage == TokenUsage(1_234, 321, 512, "us")
    failure = completion_failure(outcome)
    assert failure is not None and failure.kind is kind and failure.status_code == 200


def test_a_truncated_draft_through_the_real_package_is_a_token_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truncated = [{"type": "text", "text": '{"draft_text": "La señal de ener'}]
    transport = Transport(
        Scripted(body=message(stop_reason="max_tokens", content=truncated))
    ).install(monkeypatch)
    outcome = complete(AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY))
    assert isinstance(outcome, AssistantCompletion)
    assert (outcome.stop_reason, outcome.content_json) == ("max_tokens", None)
    failure = completion_failure(outcome)
    assert failure is not None and failure.kind is FailureKind.BUDGET_EXCEEDED
    assert len(transport.requests) == 1


def test_why_the_package_parse_is_never_called_on_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El ``parse()`` de la respuesta del paquete valida el texto **antes** de devolver nada: un
    tope de fichas lanzaría un error de validación sin dejar ver el motivo ni el uso."""
    truncated = [{"type": "text", "text": '{"draft_text": "La señal de ener'}]
    Transport(Scripted(body=message(stop_reason="max_tokens", content=truncated))).install(
        monkeypatch
    )
    client = anthropic.AsyncAnthropic(api_key=SYNTHETIC_KEY, max_retries=0)

    async def through_parse() -> object:
        raw = await client.beta.messages.with_raw_response.parse(
            **{k: v for k, v in declared(DEFAULT_MODEL).items() if k != "timeout"}
        )
        return await raw.parse()

    with pytest.raises(pydantic.ValidationError):
        asyncio.run(through_parse())


def test_a_valid_response_returns_the_response_model_and_its_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = {
        "input_tokens": 2_000,
        "output_tokens": 640,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 0,
        "inference_geo": "us",
        "iterations": [{"type": "fallback_message", "model": "claude-sonnet-5"}],
    }
    body = message(
        model="claude-sonnet-5",
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "fallback", "from_model": "claude-opus-5"},
            {"type": "text", "text": valid_text()},
        ],
        usage=usage,
    )
    body["context_management"] = {"applied_edits": []}  # campo nuevo: se tolera
    transport = Transport(Scripted(body=body, headers={"request-id": REQUEST_ID})).install(
        monkeypatch
    )
    outcome = complete(AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY))
    assert len(transport.requests) == 1
    assert outcome == AssistantCompletion(
        stop_reason="end_turn",
        content_json=valid_text(),
        model_name="claude-sonnet-5",
        model_version="claude-sonnet-5",
        usage=TokenUsage(2_000, 640, 900, "us"),
        provider_request_id=REQUEST_ID,
    )
    assert completion_failure(outcome) is None
    assert parse_output(outcome.content_json) is not None


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "thinking", "thinking": "", "signature": "s"}],
        [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}],
        [{"type": "text", "text": None}],
        "texto suelto",
    ],
)
def test_without_exactly_one_text_block_there_is_no_content(
    monkeypatch: pytest.MonkeyPatch, content: object
) -> None:
    provider, _, _ = provider_with_parse(monkeypatch, Raw(message(content=content)))
    outcome = complete(provider)
    assert isinstance(outcome, AssistantCompletion)
    assert outcome.content_json is None
    assert parse_output(outcome.content_json) is None  # el consumidor lo descarta


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "texto",
        {"stop_reason": "end_turn", "usage": {}},
        {**message(), "model": None},
        {**message(), "model": ""},
        {**message(), "model": "x" * 200},
        {**message(), "model": "claude opus"},
        {**message(), "stop_reason": None},
        {**message(), "usage": None},
        {**message(), "usage": {"input_tokens": "12", "output_tokens": 1}},
        {**message(), "usage": {"input_tokens": True, "output_tokens": 1}},
        {**message(), "usage": {"input_tokens": 2**41, "output_tokens": 1}},
        {**message(), "usage": {"input_tokens": -1, "output_tokens": 1}},
        {**message(), "usage": {"input_tokens": 1.5, "output_tokens": 1}},
        {
            **message(),
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": "9"},
        },
    ],
)
def test_a_malformed_body_is_a_permanent_failure_without_draft(
    monkeypatch: pytest.MonkeyPatch, body: object
) -> None:
    provider, _, _ = provider_with_parse(monkeypatch, Raw(body))
    assert complete(provider) == AssistantFailure(
        kind=FailureKind.PERMANENT, status_code=200, provider_request_id=REQUEST_ID
    )


@pytest.mark.parametrize("geo", ["global", "us", "eu", None])
def test_the_inference_region_of_the_response_is_kept(
    monkeypatch: pytest.MonkeyPatch, geo: str | None
) -> None:
    usage = {"input_tokens": 1, "output_tokens": 2, "inference_geo": geo}
    provider, _, _ = provider_with_parse(monkeypatch, Raw(message(usage=usage)))
    outcome = complete(provider)
    assert isinstance(outcome, AssistantCompletion)
    assert outcome.usage == TokenUsage(1, 2, 0, geo)


@pytest.mark.parametrize("geo", ["US EAST", "a" * 40, 7, "", "us​"])
def test_a_malformed_region_is_not_kept(monkeypatch: pytest.MonkeyPatch, geo: object) -> None:
    usage = {"input_tokens": 1, "output_tokens": 2, "inference_geo": geo}
    provider, _, _ = provider_with_parse(monkeypatch, Raw(message(usage=usage)))
    outcome = complete(provider)
    assert isinstance(outcome, AssistantCompletion)
    assert outcome.usage.inference_geo is None


# --- Margen de límite de tasa (NFR-LAZ-42) -------------------------------------------------------


class Gauges:
    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(metric_readers=[self.reader])
        self.metrics = LoopMetrics(self.provider.get_meter(METER_NAME))

    def headroom(self) -> float | None:
        data = self.reader.get_metrics_data()
        values: list[float] = []
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != LoopMetricName.ASSISTANT_RATE_LIMIT_HEADROOM_RATIO.value:
                        continue
                    for point in metric.data.data_points:
                        assert isinstance(point, NumberDataPoint)
                        values.append(float(point.value))
        return values[-1] if values else None


@pytest.fixture
def gauges() -> Iterator[Gauges]:
    found = Gauges()
    yield found
    found.provider.shutdown()


RATE_HEADERS: Final = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "45",
    "anthropic-ratelimit-input-tokens-limit": "100000",
    "anthropic-ratelimit-input-tokens-remaining": "15000",
    "anthropic-ratelimit-output-tokens-limit": "20000",
    "anthropic-ratelimit-output-tokens-remaining": "19000",
}


def test_each_response_sets_the_rate_limit_headroom(
    monkeypatch: pytest.MonkeyPatch, gauges: Gauges
) -> None:
    raw = Raw(message(content=[{"type": "text", "text": valid_text()}]), RATE_HEADERS)
    provider, _, _ = provider_with_parse(monkeypatch, raw, metrics=gauges.metrics)
    complete(provider)
    assert gauges.headroom() == pytest.approx(0.15)


def test_a_rate_limited_response_also_sets_the_headroom(
    monkeypatch: pytest.MonkeyPatch, gauges: Gauges
) -> None:
    headers = {
        **RATE_HEADERS,
        "anthropic-ratelimit-requests-remaining": "0",
        "retry-after": "7",
    }
    Transport(Scripted(status=429, body={}, headers=headers)).install(monkeypatch)
    provider = AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY, metrics=gauges.metrics)
    outcome = complete(provider)
    assert isinstance(outcome, AssistantFailure) and outcome.status_code == 429
    assert gauges.headroom() == 0.0


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        (RATE_HEADERS, 0.15),
        (
            {
                "anthropic-ratelimit-tokens-limit": "10",
                "anthropic-ratelimit-tokens-remaining": "10",
            },
            1.0,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "10",
                "anthropic-ratelimit-tokens-remaining": "99",
            },
            1.0,
        ),
        (
            {"anthropic-ratelimit-tokens-limit": "0", "anthropic-ratelimit-tokens-remaining": "0"},
            None,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "10",
                "anthropic-ratelimit-tokens-remaining": "-1",
            },
            None,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "1e9",
                "anthropic-ratelimit-tokens-remaining": "1",
            },
            None,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "9" * 40,
                "anthropic-ratelimit-tokens-remaining": "1",
            },
            None,
        ),
        ({"anthropic-ratelimit-tokens-limit": "10"}, None),
    ],
)
def test_the_headroom_is_the_lowest_valid_ratio(
    headers: Mapping[str, str], expected: float | None
) -> None:
    assert rate_limit_headroom(headers) == (
        pytest.approx(expected) if expected is not None else None
    )
