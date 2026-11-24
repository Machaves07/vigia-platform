"""Ruta del relevo firmado del almacén: ``GET``, ``HEAD`` y ``PUT /store-relay/object`` (TASK-607).

Solo existe con ``VIGIA_STORE_RELAY=1`` (``local`` e ``integration``; A-130): ``create_app`` la
incluye únicamente si ``AppRuntime.store_relay`` trae el ``StoreRelayStorage``. Sin él, la ruta no
existe y responde como cualquier ruta desconocida. Va fuera de la especificación
(``include_in_schema=False``): ``app.yaml`` es la misma en todos los entornos.

Las dos entradas de la lista cerrada (``STORE_RELAY_READ`` y ``STORE_RELAY_WRITE``, BR-NUC-91) no
leen la cookie de sesión ni escriben auditoría: la URL es la capacidad, como una de S3, y la
auditoría es la de la emisión (``evidence_read_granted`` y ``closure_attachment_read_granted``).

- **Token** (``?token=…``, nunca en la ruta): inválido, alterado, vencido, de otro método, de otro
  depósito o de una clave ``ca/`` → 403 genérico, sin distinguir la causa (paridad con
  ``AccessDenied``); la causa va a ``store_relay_requests_total``. Un objeto o versión que no
  existe también es 403 (S3 sin ``s3:ListBucket``).
- **GET y HEAD**: la versión del token, en trozos de 64 KiB (memoria acotada), con **un** rango
  ``bytes=a-b``, ``bytes=a-`` o ``bytes=-n`` (206 y ``Content-Range``; 416 fuera del objeto; un
  rango mal formado o varios se ignoran, RFC 9110 §14.2), ``Accept-Ranges: bytes``, el tipo del
  objeto (o el del token en los adjuntos), ``Cache-Control: private, no-store`` y la disposición
  del token. ``X-Content-Type-Options: nosniff`` lo pone la cadena en toda respuesta.
- **PUT**: ``Content-Type``, ``x-amz-checksum-sha256`` y cada metadato firmados, iguales y una sola
  vez (si no, 403); cuerpo de a lo sumo ``MAX_ATTACHMENT_BYTES`` (413 de la cadena); sube con la
  suma y los metadatos firmados. Bytes que no son los de la suma: 400 y ninguna versión nueva.
  Éxito: 200 con ``ETag``, como S3, para que ``DirectUploadField`` no cambie. Pasa la barrera
  anti-falsificación de la cadena: el navegador lo envía desde el mismo origen.

Ninguna línea de registro ni métrica lleva el token ni la clave del objeto.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from vigia_platform.loop.record_types import MAX_ATTACHMENT_BYTES
from vigia_platform.shared.api.declarations import (
    UnauthenticatedRoute,
    body_limit,
    unauthenticated,
)
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.storage import StorageUnavailable
from vigia_platform.shared.storage_relay import (
    RELAY_PATH,
    TOKEN_PARAMETER,
    RelayBadDigest,
    RelayGrant,
    RelayMethod,
    RelayObjectMissing,
    RelayOutcome,
    RelayTokenRejected,
    StoreRelayStorage,
)

__all__ = [
    "STORE_RELAY_STATE_KEY",
    "RangeNotSatisfiable",
    "byte_range",
    "store_relay_router",
]

STORE_RELAY_STATE_KEY: Final = "vigia_store_relay"
CACHE_CONTROL: Final = "private, no-store"
_RANGE: Final = re.compile(r"bytes=(?:([0-9]{1,19})-([0-9]{0,19})|-([0-9]{1,19}))")


class RangeNotSatisfiable(Exception):
    """El rango pedido no cae dentro del objeto: 416 con ``Content-Range: bytes */<tamaño>``."""


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``(primero, último)`` del único rango de ``header``; ``None`` para servir el objeto entero.

    Un rango mal formado, varios rangos o uno invertido se ignoran (RFC 9110 §14.2); uno que
    empieza fuera del objeto, o un sufijo vacío, es ``RangeNotSatisfiable``.
    """
    if header is None:
        return None
    match = _RANGE.fullmatch(header.strip())
    if match is None:
        return None
    if match.group(3) is not None:
        suffix = int(match.group(3))
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable
        return max(size - suffix, 0), size - 1
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else size - 1
    if match.group(2) and last < first:
        return None
    if first >= size:
        raise RangeNotSatisfiable
    return first, min(last, size - 1)


def _relay(request: Request) -> StoreRelayStorage:
    relay = getattr(request.app.state, STORE_RELAY_STATE_KEY, None)
    if not isinstance(relay, StoreRelayStorage):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return relay


def _single(values: list[str]) -> str | None:
    return values[0] if len(values) == 1 else None


def _verified(relay: StoreRelayStorage, request: Request, method: RelayMethod) -> RelayGrant:
    """El token de la consulta, verificado; si no vale, la métrica y 403 sin decir por qué."""
    try:
        return relay.tokens.verify(_single(request.query_params.getlist(TOKEN_PARAMETER)), method)
    except RelayTokenRejected as rejected:
        relay.record(request.method, rejected.outcome)
        raise ApiError(ApiErrorCode.FORBIDDEN) from None


def _denied(relay: StoreRelayStorage, method: str, outcome: RelayOutcome) -> ApiError:
    relay.record(method, outcome)
    return ApiError(ApiErrorCode.FORBIDDEN)


async def _read(request: Request) -> Response:
    relay = _relay(request)
    method = request.method
    grant = _verified(relay, request, RelayMethod.GET)
    try:
        found = await relay.relay_head(grant)
    except RelayObjectMissing:
        raise _denied(relay, method, RelayOutcome.DENIED) from None
    except StorageUnavailable:
        relay.record(method, RelayOutcome.UNAVAILABLE)
        raise
    size = found.size_bytes
    headers = {"accept-ranges": "bytes", "cache-control": CACHE_CONTROL}
    try:
        span = byte_range(_single(request.headers.getlist("range")), size)
    except RangeNotSatisfiable:
        relay.record(method, RelayOutcome.DENIED)
        return Response(status_code=416, headers={**headers, "content-range": f"bytes */{size}"})
    first, last = span if span is not None else (0, size - 1)
    headers["content-type"] = (
        grant.response_content_type or found.content_type or "application/octet-stream"
    )
    headers["content-length"] = str(max(last - first + 1, 0))
    if grant.disposition is not None:
        headers["content-disposition"] = grant.disposition
    status = 200
    if span is not None:
        status = 206
        headers["content-range"] = f"bytes {first}-{last}/{size}"
    outcome = RelayOutcome.PARTIAL if span is not None else RelayOutcome.SERVED
    if method == "HEAD":
        relay.record(method, outcome)
        return Response(status_code=status, headers=headers)
    try:
        body = await relay.relay_open(grant, span)
    except RelayObjectMissing:
        raise _denied(relay, method, RelayOutcome.DENIED) from None
    except StorageUnavailable:
        relay.record(method, RelayOutcome.UNAVAILABLE)
        raise
    relay.record(method, outcome)
    return StreamingResponse(_chunks(relay, body), status_code=status, headers=headers)


async def _chunks(relay: StoreRelayStorage, body: Any) -> AsyncIterator[bytes]:
    """Los trozos del cuerpo; el cuerpo se cierra siempre (también si el cliente se va)."""
    try:
        while chunk := await relay.relay_read(body):
            yield chunk
    finally:
        body.close()


async def _write(request: Request) -> Response:
    relay = _relay(request)
    method = request.method
    grant = _verified(relay, request, RelayMethod.PUT)
    for name, expected in grant.signed_headers().items():
        if request.headers.getlist(name) != [expected]:
            raise _denied(relay, method, RelayOutcome.DENIED)
    body = await request.body()
    try:
        etag = await relay.relay_put(grant, body)
    except RelayBadDigest:
        relay.record(method, RelayOutcome.BAD_DIGEST)
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    except StorageUnavailable:
        relay.record(method, RelayOutcome.UNAVAILABLE)
        raise
    relay.record(method, RelayOutcome.SERVED)
    headers = {"cache-control": CACHE_CONTROL}
    if etag is not None:
        headers["etag"] = etag
    return Response(status_code=200, headers=headers)


def store_relay_router() -> APIRouter:
    """Las dos rutas del relevo; ``create_app`` solo las incluye con el relevo activo."""
    router = APIRouter()
    router.add_api_route(
        RELAY_PATH,
        _read,
        methods=["GET", "HEAD"],
        include_in_schema=False,
        dependencies=[unauthenticated(UnauthenticatedRoute.STORE_RELAY_READ)],
    )
    router.add_api_route(
        RELAY_PATH,
        _write,
        methods=["PUT"],
        include_in_schema=False,
        dependencies=[
            unauthenticated(UnauthenticatedRoute.STORE_RELAY_WRITE),
            body_limit(MAX_ATTACHMENT_BYTES),
        ],
    )
    return router
