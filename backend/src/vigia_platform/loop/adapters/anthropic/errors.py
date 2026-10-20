"""Clasificación de los errores del paquete del proveedor (tech-stack U-04 §2.4; TASK-344).

``classify(error)`` traduce una excepción del paquete ``anthropic`` en el ``AssistantFailure`` del
puerto **por su tipo**, nunca comparando cadenas del mensaje:

- ``APITimeoutError``: ``timeout`` (el intento llegó a su tope);
- ``APIConnectionError``: ``transient``;
- ``RateLimitError`` (429): ``transient``, con la espera de su cabecera;
- ``InternalServerError`` (500 y ≥ 500), ``ServiceUnavailableError`` (503),
  ``DeadlineExceededError`` (504), ``OverloadedError`` (529) y ``ConflictError`` (409):
  ``transient``;
- ``BadRequestError`` (400), ``AuthenticationError`` (401), ``PermissionDeniedError`` (403),
  ``NotFoundError`` (404), ``RequestTooLargeError`` (413) y ``UnprocessableEntityError`` (422):
  ``permanent``;
- ``APIStatusError`` sin subclase propia: 408 y ≥ 500, ``transient``; 402 y cualquier otro,
  ``permanent``;
- cualquier otro ``AnthropicError`` (respuesta que no valida, credencial…): ``permanent``.

El paquete reintenta por su cuenta 408, 409, 429, ≥ 500 y la conexión; con ``max_retries = 0``
esos reintentos son de la bandeja de salida (``loop.assistant.consumer``: dos intentos con 5 s y
20 s de retroceso), y por eso 408 y 409 son transitorios aquí. La serie 1.x **no tiene clase propia
para 402 ni 408** (``anthropic._exceptions``; ``_make_status_error`` los deja en
``APIStatusError``): para ellos se lee el **entero** ``status_code`` de la excepción, nunca su
mensaje.

``retry_after(headers)`` lee la espera de un 429: ``retry-after-ms`` (milisegundos) o
``retry-after`` (segundos), números finitos entre 0 y ``MAX_RETRY_AFTER``; la forma de fecha HTTP
y cualquier otro valor se ignoran (el consumidor aplica su propio retroceso).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import timedelta
from typing import Final

import anthropic

from vigia_platform.loop.assistant.port import AssistantFailure, FailureKind

__all__ = ["MAX_RETRY_AFTER", "PERMANENT_STATUS", "TRANSIENT_STATUS", "classify", "retry_after"]

MAX_RETRY_AFTER: Final = timedelta(hours=1)
"""Espera más larga que se toma de una cabecera `[objetivo propio]`: más allá, el tope duro de
120 s de la solicitud ya la ha vencido (NFR-LAZ-35)."""

_TRANSIENT_TYPES: Final[tuple[type[anthropic.APIStatusError], ...]] = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.ServiceUnavailableError,
    anthropic.DeadlineExceededError,
    anthropic.OverloadedError,
    anthropic.ConflictError,
)
_PERMANENT_TYPES: Final[tuple[type[anthropic.APIStatusError], ...]] = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.RequestTooLargeError,
    anthropic.UnprocessableEntityError,
)
TRANSIENT_STATUS: Final = frozenset({408})
"""Códigos sin clase propia en el paquete que son transitorios (408, tiempo de la petición)."""
PERMANENT_STATUS: Final = frozenset({402})
"""Códigos sin clase propia en el paquete que son permanentes (402, facturación); cualquier
otro código sin clase propia por debajo de 500 también lo es."""


def _seconds(raw: str | None, scale: float) -> timedelta | None:
    if raw is None or len(raw) > 32:
        return None
    try:
        value = float(raw.strip()) * scale
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    wait = timedelta(seconds=value)
    return wait if wait <= MAX_RETRY_AFTER else None


def retry_after(headers: Mapping[str, str] | None) -> timedelta | None:
    """La espera que pide la cabecera de un 429; ``None`` si no hay una válida."""
    if headers is None:
        return None
    wait = _seconds(headers.get("retry-after-ms"), 0.001)
    return wait if wait is not None else _seconds(headers.get("retry-after"), 1.0)


def _status_failure(error: anthropic.APIStatusError) -> AssistantFailure:
    status = error.status_code
    request_id = error.request_id
    if isinstance(error, anthropic.RateLimitError):
        return AssistantFailure(
            kind=FailureKind.TRANSIENT,
            status_code=status,
            retry_after=retry_after(error.response.headers),
            provider_request_id=request_id,
        )
    if isinstance(error, _TRANSIENT_TYPES):
        kind = FailureKind.TRANSIENT
    elif isinstance(error, _PERMANENT_TYPES):
        kind = FailureKind.PERMANENT
    elif status in TRANSIENT_STATUS or status >= 500:
        kind = FailureKind.TRANSIENT
    else:
        kind = FailureKind.PERMANENT
    return AssistantFailure(kind=kind, status_code=status, provider_request_id=request_id)


def classify(error: anthropic.AnthropicError) -> AssistantFailure:
    """El fallo del puerto que corresponde a ``error`` (por tipo; ver la tabla del módulo)."""
    if isinstance(error, anthropic.APITimeoutError):
        return AssistantFailure(kind=FailureKind.TIMEOUT)
    if isinstance(error, anthropic.APIConnectionError):
        return AssistantFailure(kind=FailureKind.TRANSIENT)
    if isinstance(error, anthropic.APIStatusError):
        return _status_failure(error)
    return AssistantFailure(kind=FailureKind.PERMANENT)
