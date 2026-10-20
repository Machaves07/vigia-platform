"""TASK-344 · clasificación de los errores del proveedor por las excepciones tipadas del paquete
(tech-stack-decisions U-04 §2.4).

- **Tabla, paramétrica**: cada excepción tipada del paquete (y ``APIStatusError`` sin subclase
  para 402 y 408) es ``transient``, ``permanent`` o ``timeout`` según la tabla del módulo.
- **Por el paquete real**: con el doble del transporte, cada código de estado llega como la
  excepción que el paquete construye y se clasifica igual; con ``max_retries = 0`` el paquete
  envía **una sola** petición (los reintentos son de la bandeja de salida). La conexión rota es
  transitoria y el tiempo agotado es ``timeout``.
- **429**: devuelve la espera de su cabecera (``retry-after-ms`` o ``retry-after``); una cabecera
  no numérica, negativa, no finita o desmedida no se toma.
- **Sin cadenas**: el mismo tipo con cualquier mensaje y cualquier cuerpo generados se clasifica
  igual, y ``errors.py`` no lee el mensaje, el cuerpo ni los argumentos de la excepción.

Ninguna prueba abre la red (``no_network``); solo datos generados.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import anthropic
import httpx2
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.anthropic_support import (
    SYNTHETIC_KEY,
    Scripted,
    Transport,
    context,
    error_body,
    factory,
    no_network,
)
from vigia_platform.loop.adapters.anthropic import errors as errors_module
from vigia_platform.loop.adapters.anthropic.client import AnthropicAssistantProvider
from vigia_platform.loop.adapters.anthropic.errors import MAX_RETRY_AFTER, classify, retry_after
from vigia_platform.loop.assistant.port import AssistantFailure, FailureKind

pytestmark = pytest.mark.usefixtures("no_network")
__all__ = ["no_network"]

T, P = FailureKind.TRANSIENT, FailureKind.PERMANENT
TABLE: Final[list[tuple[type[anthropic.APIStatusError], int, FailureKind]]] = [
    (anthropic.RateLimitError, 429, T),
    (anthropic.InternalServerError, 500, T),
    (anthropic.InternalServerError, 502, T),
    (anthropic.ServiceUnavailableError, 503, T),
    (anthropic.DeadlineExceededError, 504, T),
    (anthropic.OverloadedError, 529, T),
    (anthropic.ConflictError, 409, T),
    (anthropic.APIStatusError, 408, T),
    (anthropic.BadRequestError, 400, P),
    (anthropic.AuthenticationError, 401, P),
    (anthropic.APIStatusError, 402, P),
    (anthropic.PermissionDeniedError, 403, P),
    (anthropic.NotFoundError, 404, P),
    (anthropic.RequestTooLargeError, 413, P),
    (anthropic.UnprocessableEntityError, 422, P),
    (anthropic.APIStatusError, 418, P),
]
"""Excepción del paquete, código y clase (tech-stack §2.4)."""


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status, headers=headers or {}, request=request)


def _status_error(
    cls: type[anthropic.APIStatusError],
    status: int,
    *,
    message: str = "mensaje sintético",
    body: object = None,
    headers: dict[str, str] | None = None,
) -> anthropic.APIStatusError:
    return cls(message, response=_response(status, headers), body=body)


@pytest.mark.parametrize(("cls", "status", "kind"), TABLE)
def test_each_typed_exception_is_classified_by_the_table(
    cls: type[anthropic.APIStatusError], status: int, kind: FailureKind
) -> None:
    failure = classify(_status_error(cls, status, headers={"request-id": "req_x"}))
    assert (failure.kind, failure.status_code) == (kind, status)
    assert failure.provider_request_id == "req_x"


def test_timeout_and_connection_errors_are_classified_by_type() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assert classify(anthropic.APITimeoutError(request=request)) == AssistantFailure(
        kind=FailureKind.TIMEOUT
    )
    assert classify(anthropic.APIConnectionError(request=request)) == AssistantFailure(
        kind=FailureKind.TRANSIENT
    )


def test_any_other_package_error_is_permanent() -> None:
    response = _response(200)
    for error in (
        anthropic.AnthropicError("sin credencial"),
        anthropic.APIResponseValidationError(response=response, body={}),
    ):
        assert classify(error) == AssistantFailure(kind=FailureKind.PERMANENT)


# --- Por el paquete real, con el doble del transporte --------------------------------------------

ERROR_TYPES: Final = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    404: "not_found_error",
    408: "timeout_error",
    409: "conflict_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    503: "api_error",
    504: "timeout_error",
    529: "overloaded_error",
}
EXPECTED: Final = {
    status: (P if status in {400, 401, 402, 403, 404, 413, 422} else T) for status in ERROR_TYPES
}


def _complete(monkeypatch: pytest.MonkeyPatch, script: Scripted) -> tuple[Any, Transport]:
    transport = Transport(script).install(monkeypatch)
    provider = AnthropicAssistantProvider(factory(), api_key=SYNTHETIC_KEY)
    outcome = asyncio.run(provider.complete(context(), timeout=timedelta(seconds=60)))
    return outcome, transport


@pytest.mark.parametrize("status", sorted(ERROR_TYPES))
def test_each_status_through_the_package_is_classified_without_package_retries(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    outcome, transport = _complete(
        monkeypatch,
        Scripted(status=status, body=error_body(ERROR_TYPES[status]), headers={"request-id": "r"}),
    )
    assert isinstance(outcome, AssistantFailure)
    assert (outcome.kind, outcome.status_code) == (EXPECTED[status], status)
    assert outcome.provider_request_id == "r"
    assert len(transport.requests) == 1  # max_retries = 0: el paquete no reintenta


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (httpx2.ConnectError("conexión rechazada"), FailureKind.TRANSIENT),
        (httpx2.RemoteProtocolError("conexión cortada"), FailureKind.TRANSIENT),
        (httpx2.ReadTimeout("lectura agotada"), FailureKind.TIMEOUT),
        (httpx2.ConnectTimeout("conexión agotada"), FailureKind.TIMEOUT),
    ],
)
def test_transport_failures_through_the_package(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, kind: FailureKind
) -> None:
    outcome, transport = _complete(monkeypatch, Scripted(raises=raised))
    assert outcome == AssistantFailure(kind=kind)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("headers", "wait"),
    [
        ({"retry-after": "7"}, timedelta(seconds=7)),
        ({"retry-after": "0.5"}, timedelta(seconds=0.5)),
        ({"retry-after-ms": "1500"}, timedelta(seconds=1.5)),
        ({"retry-after-ms": "1500", "retry-after": "9"}, timedelta(seconds=1.5)),
        ({"retry-after-ms": "x", "retry-after": "9"}, timedelta(seconds=9)),
        ({}, None),
    ],
)
def test_a_429_returns_the_wait_of_its_header(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], wait: timedelta | None
) -> None:
    outcome, _ = _complete(
        monkeypatch, Scripted(status=429, body=error_body("rate_limit_error"), headers=headers)
    )
    assert isinstance(outcome, AssistantFailure)
    assert (outcome.kind, outcome.status_code, outcome.retry_after) == (
        FailureKind.TRANSIENT,
        429,
        wait,
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "abc",
        "-1",
        "nan",
        "inf",
        "-inf",
        "1e400",
        "9" * 40,
        "Wed, 21 Oct 2026 07:28:00 GMT",
    ],
)
def test_an_unusable_retry_after_is_ignored(value: str) -> None:
    assert retry_after({"retry-after": value}) is None
    assert retry_after({"retry-after-ms": value}) is None


def test_the_retry_after_bounds() -> None:
    assert retry_after({"retry-after": "0"}) == timedelta(0)
    assert retry_after({"retry-after": "3600"}) == MAX_RETRY_AFTER
    assert retry_after({"retry-after": "3600.001"}) is None
    assert retry_after({"retry-after-ms": "3600000"}) == MAX_RETRY_AFTER
    assert retry_after({"retry-after-ms": "3600001"}) is None
    assert retry_after(None) is None


def test_only_a_429_carries_a_wait() -> None:
    for cls, status in ((anthropic.OverloadedError, 529), (anthropic.InternalServerError, 500)):
        failure = classify(_status_error(cls, status, headers={"retry-after": "7"}))
        assert failure.retry_after is None


# --- Ninguna clasificación compara cadenas -------------------------------------------------------

_WORDS: Final = st.sampled_from(
    ["overloaded", "rate limit", "timeout", "invalid", "billing", "retry", "529", "429", "400"]
)


@given(
    row=st.sampled_from(TABLE),
    message=st.one_of(st.text(max_size=200), _WORDS),
    body=st.one_of(
        st.none(),
        st.dictionaries(st.text(max_size=10), st.text(max_size=30), max_size=3),
        st.builds(error_body, _WORDS),
    ),
)
def test_the_message_and_body_never_change_the_class(
    row: tuple[type[anthropic.APIStatusError], int, FailureKind], message: str, body: object
) -> None:
    cls, status, kind = row
    assert classify(_status_error(cls, status, message=message, body=body)).kind is kind


def test_the_classifier_never_reads_the_message_body_or_arguments() -> None:
    tree = ast.parse(Path(errors_module.__file__).read_text(encoding="utf-8"))
    read = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
    }
    assert not read & {"message", "body", "args", "__str__", "text"}
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"str", "repr", "format"}
