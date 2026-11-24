"""Relevo firmado del mismo origen para el depósito de evidencias (A-130; TASK-607; BR-NUC-66, 91).

Solo en ``local`` e ``integration`` (``VIGIA_STORE_RELAY=1``; ``RuntimeConfig`` impide arrancar en
cualquier otro entorno). Allí LocalStack no se publica (A-108: ``ca/root.pem`` de ``vigia-edge``
quedaría a la vista sin autenticar), así que una URL firmada contra ``http://localstack:4566`` no
la alcanza el navegador. ``StoreRelayStorage`` es el ``S3Storage`` de ``vigia-evidence`` con **otra
firma**: sus URL apuntan a ``VIGIA_PUBLIC_ORIGIN`` + ``/store-relay/object?token=…`` y la ruta
``shared.api.store_relay`` las sirve hablando con LocalStack por la red interna. En AWS no existe.

- ``presign_get`` y ``presign_attachment_get``: el mismo vencimiento (≤ 5 y ≤ 15 minutos), la misma
  ``version_id`` fijada y la misma disposición que ``S3Storage``;
- ``presign_put``: **solo** claves de adjuntos de cierre (``CLOSURE_KEY_PATTERN`` de U-04). Un clip
  del nodo o cualquier otra clave es ``StorageUnavailable``: hacia el nodo, ``storage_unavailable``,
  como hoy (A-108 no cambia);
- ``presign_part``: ``StorageUnavailable`` (el navegador no sube por partes);
- todo lo demás (``head_object``, ``get_object``, ``put_object``…) pasa sin cambios al cliente.

**El token** (``RelayTokens``): versión, depósito, clave, ``version_id``, método, vencimiento en
milisegundos (reloj inyectado), tipo de contenido, suma SHA-256, cabeceras requeridas, disposición
y tipo de la respuesta, en JSON canónico y base64url sin relleno, más su HMAC-SHA256 con la clave
de ``VIGIA_STORE_RELAY_SECRET`` (Secrets Manager, ≥ 32 bytes, la misma en las dos réplicas de la
API). La firma se compara en tiempo constante y las dos partes deben estar en su codificación
canónica. Un token alterado, vencido, de otro método, de otro depósito o de una clave ``ca/`` es
``RelayTokenRejected`` (la ruta responde 403 sin decir por qué; la causa va a la métrica).

La URL es la capacidad, como una de S3: el token va en la **consulta**, que ni el balanceador ni la
API registran, y ninguna línea de registro ni métrica lleva el token ni la clave del objeto.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from typing import Any, Final
from urllib.parse import urlencode

from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]

from vigia_platform.loop.adapters.attachments.rules import CLOSURE_KEY_PATTERN
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.storage import (
    CHECKSUM_HEADER,
    CONTENT_TYPE_HEADER,
    PRESIGN_ATTACHMENT_GET_MAX_TTL,
    PRESIGN_GET_MAX_TTL,
    PRESIGN_PUT_MAX_TTL,
    PresignedRequest,
    S3Storage,
    StorageSettings,
    StorageUnavailable,
    _http_status,
    _metadata_from_headers,
    _require_key,
    _require_ttl,
    _require_version_id,
    sha256_hex_to_b64,
)

__all__ = [
    "MIN_KEY_BYTES",
    "READ_CHUNK_BYTES",
    "REFUSED_KEY_PREFIXES",
    "RELAY_PATH",
    "TOKEN_PARAMETER",
    "TOKEN_VERSION",
    "RelayBadDigest",
    "RelayGrant",
    "RelayMethod",
    "RelayObject",
    "RelayObjectMissing",
    "RelayOutcome",
    "RelayTokenRejected",
    "RelayTokens",
    "StoreRelaySettings",
    "StoreRelayStorage",
]

RELAY_PATH: Final = "/store-relay/object"
"""Ruta del relevo en el origen de la aplicación; el token va en la consulta, nunca aquí."""
TOKEN_PARAMETER: Final = "token"  # noqa: S105 - nombre del parámetro, no un secreto
TOKEN_VERSION: Final = 1
MIN_KEY_BYTES: Final = 32
"""Longitud mínima de la clave HMAC del relevo (``store-relay.key``: 32 bytes)."""
MAX_TOKEN_CHARS: Final = 4096
"""Tope de un token; uno firmado por la plataforma ocupa menos de 1 000 caracteres."""
REFUSED_KEY_PREFIXES: Final = ("ca/",)
"""Claves que el relevo nunca sirve, aunque el token sea auténtico (A-108: la raíz de nodos)."""
READ_CHUNK_BYTES: Final = 64 * 1024
"""Trozo de lectura del cuerpo: la memoria de una descarga no crece con el objeto."""
ATTACHMENT_DISPOSITION: Final = "attachment"
ATTACHMENT_CONTENT_TYPE: Final = "application/octet-stream"
_METADATA_PREFIX: Final = "x-amz-meta-"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]{1,4096}")
_PAYLOAD_FIELDS: Final = frozenset(
    {"v", "b", "k", "vid", "m", "exp", "ct", "sha", "h", "cd", "rct"}
)
_BAD_DIGEST_CODES: Final = frozenset({"BadDigest", "InvalidDigest", "XAmzContentChecksumMismatch"})
_MISSING_HTTP_STATUS: Final = frozenset({400, 403, 404})
"""Respuestas del almacén a un objeto o versión que no se ve: el relevo las trata como 403."""

_log = get_logger("shared.storage_relay")


class RelayMethod(enum.StrEnum):
    """Método que autoriza un token: ``GET`` (también ``HEAD``) o ``PUT``."""

    GET = "GET"
    PUT = "PUT"


class RelayOutcome(enum.StrEnum):
    """``outcome`` de ``store_relay_requests_total`` (lista cerrada).

    El ``413`` lo emite la cadena (``BodyLimitStep``) antes de llegar a la ruta: se cuenta en
    ``http_server_errors_total`` con la ruta ``/store-relay/object``. Un rango fuera del objeto
    (``416``) y un objeto o versión que no existe cuentan como ``denied``.
    """

    SERVED = "served"
    PARTIAL = "partial"
    DENIED = "denied"
    EXPIRED = "expired"
    BAD_DIGEST = "bad_digest"
    UNAVAILABLE = "unavailable"


class RelayTokenRejected(Exception):
    """Token inválido, alterado, vencido, de otro método, de otro depósito o de una clave vetada.

    Hacia fuera es siempre 403 sin cuerpo útil (paridad con ``AccessDenied`` de S3); ``outcome``
    (``denied`` o ``expired``) solo va a la métrica. El mensaje no lleva el token ni la clave.
    """

    def __init__(self, outcome: RelayOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


class RelayObjectMissing(Exception):
    """El objeto o la versión del token no existe: 403, como S3 sin ``s3:ListBucket``."""


class RelayBadDigest(Exception):
    """Los bytes subidos no tienen la suma firmada: el almacén no guarda nada (400)."""


@dataclass(frozen=True, slots=True)
class RelayGrant:
    """Lo que autoriza un token ya verificado. Nunca sale a un registro (``repr`` sin la clave)."""

    bucket: str
    key: str = field(repr=False)
    version_id: str | None = field(repr=False)
    method: RelayMethod
    expires_at_ms: int
    content_type: str | None = None
    """``PUT``: el tipo de contenido firmado."""
    checksum_sha256: str | None = None
    """``PUT``: la suma firmada, en base64 (``x-amz-checksum-sha256``)."""
    metadata: Mapping[str, str] = field(default_factory=dict)
    """``PUT``: los metadatos de usuario firmados (sin ``x-amz-meta-``)."""
    disposition: str | None = None
    """``GET``: ``Content-Disposition`` de la respuesta (``attachment`` en los adjuntos)."""
    response_content_type: str | None = None
    """``GET``: el tipo que sustituye al del objeto (``application/octet-stream`` en adjuntos)."""

    def signed_headers(self) -> dict[str, str]:
        """``PUT``: las cabeceras que el cliente debe enviar **exactamente** (en minúsculas)."""
        headers: dict[str, str] = {}
        if self.content_type is not None:
            headers[CONTENT_TYPE_HEADER] = self.content_type
        if self.checksum_sha256 is not None:
            headers[CHECKSUM_HEADER] = self.checksum_sha256
        headers.update({f"{_METADATA_PREFIX}{name}": v for name, v in self.metadata.items()})
        return headers


@dataclass(frozen=True, slots=True)
class RelayObject:
    """Metadatos de la versión que sirve el relevo (``HEAD`` con la ``VersionId`` del token)."""

    size_bytes: int
    content_type: str | None
    etag: str | None


def _epoch_ms(moment: datetime) -> int:
    return (moment - _EPOCH) // _MILLISECOND


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes | None:
    """base64url sin relleno en su forma **canónica**; ``None`` si no lo es.

    Con relleno quitado, el último carácter puede llevar bits sobrantes: dos textos distintos
    decodificarían a los mismos bytes. Solo se acepta el que se vuelve a codificar igual.
    """
    if _B64URL.fullmatch(text) is None:
        return None
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    return data if _b64e(data) == text else None


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError
    return value


class RelayTokens:
    """Emite y verifica los tokens del relevo de un depósito con su clave y su reloj."""

    def __init__(self, key: bytes, clock: Clock, bucket: str) -> None:
        if not isinstance(key, bytes) or len(key) < MIN_KEY_BYTES:
            raise ValueError(f"la clave del relevo debe tener al menos {MIN_KEY_BYTES} bytes")
        self._key = key
        self._clock = clock
        self._bucket = bucket

    def __repr__(self) -> str:
        return "RelayTokens()"

    @property
    def bucket(self) -> str:
        return self._bucket

    def _mac(self, body: str) -> bytes:
        return hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest()

    def issue(self, grant: RelayGrant) -> str:
        """``<carga>.<firma>``, las dos en base64url sin relleno."""
        payload = {
            "v": TOKEN_VERSION,
            "b": grant.bucket,
            "k": grant.key,
            "vid": grant.version_id,
            "m": grant.method.value,
            "exp": grant.expires_at_ms,
            "ct": grant.content_type,
            "sha": grant.checksum_sha256,
            "h": dict(sorted(grant.metadata.items())),
            "cd": grant.disposition,
            "rct": grant.response_content_type,
        }
        body = _b64e(_canonical(payload))
        return f"{body}.{_b64e(self._mac(body))}"

    def expires_at_ms(self, seconds: int) -> tuple[datetime, int]:
        """El vencimiento de una URL que se emite ahora con ``seconds`` de vigencia."""
        expires_at = self._clock.now() + timedelta(seconds=seconds)
        return expires_at, _epoch_ms(expires_at)

    def verify(self, token: object, method: RelayMethod) -> RelayGrant:
        """El ``RelayGrant`` de ``token`` para ``method``; si no vale, ``RelayTokenRejected``."""
        denied = RelayTokenRejected(RelayOutcome.DENIED)
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise denied
        parts = token.split(".")
        if len(parts) != 2:
            raise denied
        body, signature_text = parts
        signature = _b64d(signature_text)
        if signature is None:  # otra longitud la rechaza ``compare_digest`` (32 bytes)
            raise denied
        if _B64URL.fullmatch(body) is None:
            raise denied
        if not compare_digest(self._mac(body), signature):
            raise denied
        grant = self._grant(_b64d(body))
        if grant is None:
            raise denied
        if grant.bucket != self._bucket:
            raise denied
        if grant.key.startswith(REFUSED_KEY_PREFIXES):
            raise denied
        if grant.method is not method:
            raise denied
        if _epoch_ms(self._clock.now()) >= grant.expires_at_ms:
            raise RelayTokenRejected(RelayOutcome.EXPIRED)
        return grant

    @staticmethod
    def _grant(raw: bytes | None) -> RelayGrant | None:
        """La carga ya autenticada, con su forma exacta; ``None`` si no la tiene."""
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
                return None
            if payload["v"] != TOKEN_VERSION or type(payload["exp"]) is not int:
                return None
            bucket, key = payload["b"], payload["k"]
            if not isinstance(bucket, str) or not isinstance(key, str):
                return None
            _require_key(key)
            version_id = _optional_text(payload["vid"])
            _require_version_id(version_id)
            metadata = payload["h"]
            if not isinstance(metadata, dict) or not all(
                isinstance(name, str) and isinstance(value, str) for name, value in metadata.items()
            ):
                return None
            return RelayGrant(
                bucket=bucket,
                key=key,
                version_id=version_id,
                method=RelayMethod(payload["m"]),
                expires_at_ms=payload["exp"],
                content_type=_optional_text(payload["ct"]),
                checksum_sha256=_optional_text(payload["sha"]),
                metadata=metadata,
                disposition=_optional_text(payload["cd"]),
                response_content_type=_optional_text(payload["rct"]),
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class StoreRelaySettings:
    """Lo que el relevo necesita además del depósito: su clave, el origen y las métricas."""

    key: bytes = field(repr=False)
    public_origin: str
    metrics: PlatformMetrics | None = field(default=None, repr=False)


class StoreRelayStorage(S3Storage):
    """``S3Storage`` de ``vigia-evidence`` cuyas URL firmadas apuntan al relevo del mismo origen."""

    def __init__(
        self,
        settings: StorageSettings,
        clock: Clock,
        *,
        relay: StoreRelaySettings,
        refused_buckets: frozenset[str] = frozenset(),
        client: Any | None = None,
    ) -> None:
        if settings.bucket in refused_buckets:
            raise ValueError("el relevo solo se construye para el depósito de evidencias")
        super().__init__(settings, clock, client=client)
        self.tokens = RelayTokens(relay.key, clock, settings.bucket)
        self._origin = relay.public_origin
        self._metrics = relay.metrics

    def __repr__(self) -> str:
        return "StoreRelayStorage()"

    def record(self, method: str, outcome: RelayOutcome) -> None:
        """Suma 1 a ``store_relay_requests_total{method, outcome}`` (nunca el token ni la clave)."""
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.store_relay_requests_total.add(1, {"method": method, "outcome": outcome.value})

    def _url(self, grant: RelayGrant) -> str:
        query = urlencode({TOKEN_PARAMETER: self.tokens.issue(grant)})
        return f"{self._origin}{RELAY_PATH}?{query}"

    def _read_url(
        self,
        key: str,
        version_id: str | None,
        seconds: int,
        *,
        disposition: str | None = None,
        response_content_type: str | None = None,
    ) -> PresignedRequest:
        expires_at, expires_at_ms = self.tokens.expires_at_ms(seconds)
        grant = RelayGrant(
            bucket=self.bucket,
            key=key,
            version_id=version_id,
            method=RelayMethod.GET,
            expires_at_ms=expires_at_ms,
            disposition=disposition,
            response_content_type=response_content_type,
        )
        return PresignedRequest("GET", self._url(grant), {}, expires_at)

    # --- firma: lo único que cambia ----------------------------------------------------------

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        """URL del relevo de solo lectura, vigente a lo sumo 5 minutos (BR-NUC-66)."""
        _require_key(key)
        _require_version_id(version_id)
        seconds = _require_ttl(ttl, PRESIGN_GET_MAX_TTL)
        return self._read_url(key, version_id, seconds)

    async def presign_attachment_get(
        self,
        key: str,
        ttl: timedelta = PRESIGN_ATTACHMENT_GET_MAX_TTL,
        *,
        version_id: str | None = None,
    ) -> PresignedRequest:
        """La descarga de un adjunto (``attachment`` y tipo opaco), vigente a lo sumo 15 min."""
        _require_key(key)
        _require_version_id(version_id)
        seconds = _require_ttl(ttl, PRESIGN_ATTACHMENT_GET_MAX_TTL)
        return self._read_url(
            key,
            version_id,
            seconds,
            disposition=ATTACHMENT_DISPOSITION,
            response_content_type=ATTACHMENT_CONTENT_TYPE,
        )

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest:
        """URL del relevo para un ``PUT``, **solo** de un adjunto de cierre (A-108 no cambia)."""
        _require_key(key)
        if CLOSURE_KEY_PATTERN.fullmatch(key) is None:
            _log.warning("el relevo solo firma subidas de adjuntos de cierre")
            raise StorageUnavailable("presign_put")
        seconds = _require_ttl(ttl, PRESIGN_PUT_MAX_TTL)
        checksum = sha256_hex_to_b64(checksum_sha256)
        metadata = _metadata_from_headers(required_headers)
        expires_at, expires_at_ms = self.tokens.expires_at_ms(seconds)
        grant = RelayGrant(
            bucket=self.bucket,
            key=key,
            version_id=None,
            method=RelayMethod.PUT,
            expires_at_ms=expires_at_ms,
            content_type=content_type,
            checksum_sha256=checksum,
            metadata=metadata,
        )
        return PresignedRequest("PUT", self._url(grant), grant.signed_headers(), expires_at)

    async def presign_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        checksum_sha256: str,
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest:
        """El relevo no firma subidas por partes: el navegador no las usa."""
        raise StorageUnavailable("presign_part")

    # --- lo que hace la ruta del relevo ------------------------------------------------------

    def _object(self, grant: RelayGrant) -> dict[str, Any]:
        """Depósito, clave y versión **del token** (nunca la vigente si el token fija una)."""
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": grant.key}
        if grant.version_id is not None:
            params["VersionId"] = grant.version_id
        return params

    async def relay_head(self, grant: RelayGrant) -> RelayObject:
        """Tamaño y tipo de la versión del token; ``RelayObjectMissing`` si no existe."""
        params = self._object(grant)

        def head() -> RelayObject:
            try:
                response = self._client.head_object(**params)
            except botocore_exceptions.ClientError as error:
                if _http_status(error) in _MISSING_HTTP_STATUS:
                    raise RelayObjectMissing from None
                raise
            return RelayObject(
                size_bytes=int(response["ContentLength"]),
                content_type=response.get("ContentType"),
                etag=response.get("ETag"),
            )

        return await self._call("relay_head", head)

    async def relay_open(self, grant: RelayGrant, byte_range: tuple[int, int] | None) -> Any:
        """El cuerpo de la versión del token (``StreamingBody``), entero o ``[a, b]``."""
        params = self._object(grant)
        if byte_range is not None:
            params["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"

        def open_body() -> Any:
            try:
                return self._client.get_object(**params)["Body"]
            except botocore_exceptions.ClientError as error:
                if _http_status(error) in _MISSING_HTTP_STATUS:
                    raise RelayObjectMissing from None
                raise

        return await self._call("relay_open", open_body)

    async def relay_read(self, body: Any) -> bytes:
        """El siguiente trozo del cuerpo (``b""`` al terminar), en el pool de hilos y con tope."""

        def read() -> bytes:
            chunk: bytes = body.read(READ_CHUNK_BYTES)
            return chunk

        return await self._call("relay_read", read)

    async def relay_put(self, grant: RelayGrant, body: bytes) -> str | None:
        """Sube ``body`` con la suma, el tipo y los metadatos **firmados**; devuelve el ``ETag``.

        El almacén comprueba la suma: unos bytes distintos son ``RelayBadDigest`` y no queda
        ninguna versión nueva.
        """
        params = {
            **self._object(grant),
            "Body": body,
            "ContentType": grant.content_type,
            "ChecksumSHA256": grant.checksum_sha256,
            "Metadata": dict(grant.metadata),
        }

        def put() -> str | None:
            try:
                response = self._client.put_object(**params)
            except botocore_exceptions.ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if _http_status(error) == 400 and code in _BAD_DIGEST_CODES:
                    raise RelayBadDigest from None
                raise
            etag: str | None = response.get("ETag")
            return etag

        return await self._call("relay_put", put)
