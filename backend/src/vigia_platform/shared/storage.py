"""Adaptador del almacén de objetos (LC-NUC-30; PAT-NUC-RES-03, REN-06; NFR-NUC-33, 36, 37).

``StoragePort`` es la única puerta de la plataforma hacia S3. Cada instancia queda ligada a **un**
depósito (``vigia-evidence``, ``vigia-archive`` o ``vigia-edge``) y ofrece:

- ``head_object(key)``: metadatos del objeto sin descargarlo, con ``ChecksumMode=ENABLED`` para
  obtener la suma SHA-256 que calculó el almacén (``x-amz-checksum-sha256``), el tamaño y los
  metadatos de usuario (``x-amz-meta-*``). Un objeto inexistente devuelve ``None``;
- ``presign_get(key, ttl, version_id=...)``: URL de solo lectura vigente a lo sumo 5 minutos
  (BR-NUC-66), fijada a una versión concreta del objeto si se indica;
- ``get_object(key, version_id=...)``: los bytes, solo para la muestra diaria del worker
  (NFR-NUC-33), también de una versión concreta;
- ``put_object(key, body, content_type)``: subida propia de la plataforma (archivado) con la
  suma SHA-256 y cifrado ``aws:kms`` con la clave indicada;
- ``presign_put(key, content_type, checksum_sha256, required_headers, ttl)``: la URL de la
  concesión de subida que emite U-03, vigente a lo sumo 15 minutos. La suma, el tipo de
  contenido y cada cabecera de ``required_headers`` van **firmados**: un ``PUT`` sin ellos, o
  con otro valor, no pasa la firma, y unos bytes que no coinciden con la suma dan ``BadDigest``;
- ``create_multipart``, ``presign_part``, ``complete_multipart`` y ``abort_multipart``: la
  subida por partes de U-04, con la suma SHA-256 de cada parte firmada en su URL.

No existe listado de objetos (BR-NUC-66): el puerto no lo ofrece.

Toda clave pasa por ``_require_key``: el juego de caracteres de ``StorageKey`` del contrato y,
además, segmentos no vacíos y distintos de ``.`` y ``..`` (sin ``//``, sin ``/`` al principio ni
al final): una clave nunca se parece a una ruta relativa.

Las URL se firman contra el **punto de conexión regional fijo** ``s3.us-east-1.amazonaws.com``
en estilo de host virtual (``https://<depósito>.s3.us-east-1.amazonaws.com/<clave>``), que es el
origen que ``VIGIA_CSP_STORE_ORIGINS`` declara para la política de contenido (U-05; nota del
2026-09-23 de LC-NUC-30). En pruebas, ``StorageSettings.endpoint_url`` apunta a LocalStack.

Tiempos de espera (NFR-NUC-36, ``[objetivos propios]``): conexión 5 s y lectura 10 s, **sin
reintentos** en boto3; cada llamada tiene además un tope de conexión + lectura + 1 s en el bucle
de eventos. boto3 es bloqueante: cada llamada corre en el pool de hilos. Un almacén inaccesible
(conexión rechazada, tiempo de espera, conexión cortada, 5xx o limitación) termina en
``StorageUnavailable``, transitorio, que hacia el nodo es ``storage_unavailable`` (BR-CTR-30).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import enum
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import boto3  # type: ignore[import-untyped]
from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from vigia_platform.shared.clock import Clock

__all__ = [
    "ANONYMIZED_HEADER",
    "ANONYMIZED_METADATA_KEY",
    "CHECKSUM_HEADER",
    "CONNECT_TIMEOUT_SECONDS",
    "CONTENT_TYPE_HEADER",
    "PRESIGN_GET_MAX_TTL",
    "PRESIGN_PUT_MAX_TTL",
    "READ_TIMEOUT_SECONDS",
    "REGIONAL_ENDPOINT",
    "RETRY_AFTER_SECONDS",
    "STORE_REGION",
    "AddressingStyle",
    "ChecksumType",
    "CompletedPart",
    "ObjectHead",
    "PresignedRequest",
    "S3Storage",
    "StorageCredentials",
    "StoragePort",
    "StorageSettings",
    "StorageUnavailable",
    "sha256_b64",
    "sha256_hex_to_b64",
]

STORE_REGION: Final = "us-east-1"
REGIONAL_ENDPOINT: Final = f"https://s3.{STORE_REGION}.amazonaws.com"
"""Punto de conexión regional fijo contra el que se firman las URL (nota de LC-NUC-30)."""

CONNECT_TIMEOUT_SECONDS: Final = 5.0
READ_TIMEOUT_SECONDS: Final = 10.0
CALL_TIMEOUT_MARGIN_SECONDS: Final = 1.0
"""Holgura del tope de cada llamada en el bucle de eventos sobre conexión + lectura."""
RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` de ``storage_unavailable`` ``[objetivo propio]``."""

PRESIGN_GET_MAX_TTL: Final = timedelta(minutes=5)
"""Vigencia máxima de una URL de lectura (BR-NUC-66)."""
PRESIGN_PUT_MAX_TTL: Final = timedelta(minutes=15)
"""Vigencia máxima de una URL de subida, entera o por partes (``ClipUploadGrant.expires_at``)."""

CHECKSUM_HEADER: Final = "x-amz-checksum-sha256"
CONTENT_TYPE_HEADER: Final = "content-type"
ANONYMIZED_HEADER: Final = "x-amz-meta-vigia-anonymized"
ANONYMIZED_METADATA_KEY: Final = "vigia-anonymized"
"""Nombre del metadato de usuario tal como lo devuelve ``head_object`` (sin ``x-amz-meta-``)."""
_METADATA_HEADER_PREFIX: Final = "x-amz-meta-"
_METADATA_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_METADATA_VALUE: Final = re.compile(r"[\x21-\x7e]{1,256}")

_STORAGE_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9!_.*'()/=-]{0,511}")
"""Mismo patrón que ``StorageKey`` del contrato: caracteres seguros para claves de objeto."""
_KEY_DOT_SEGMENTS: Final = frozenset({".", ".."})
_VERSION_ID: Final = re.compile(r"[\x21-\x7e]{1,1024}")
"""Identificador de versión de S3: ASCII visible, sin espacios."""
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_MAX_PART_NUMBER: Final = 10_000

_TRANSIENT_HTTP_STATUS: Final = frozenset({500, 502, 503, 504})
_TRANSIENT_ERROR_CODES: Final = frozenset(
    {
        "InternalError",
        "ServiceUnavailable",
        "SlowDown",
        "RequestTimeout",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
    }
)
_ABSENT_HTTP_STATUS: Final = frozenset({403, 404})
"""Respuestas de ``HEAD`` que significan "no hay objeto visible en esa clave".

S3 responde 403, no 404, a un ``HEAD`` sobre una clave inexistente cuando el rol no tiene
``s3:ListBucket`` sobre el depósito, que es el caso de los roles de tarea (sin listado de
objetos, BR-NUC-66; ``infrastructure-design.md`` §8). ``HEAD`` no lleva cuerpo de error, así que
no hay forma de distinguir los dos casos.
"""


# --- Errores y valores -------------------------------------------------------------------------


class StorageUnavailable(Exception):
    """El almacén no respondió a tiempo o no está accesible (NFR-NUC-37): transitorio.

    Hacia el nodo se traduce a ``storage_unavailable`` con ``retryable = true`` (BR-CTR-30); el
    mensaje no lleva claves, URL ni detalles internos.
    """

    code: Final = "storage_unavailable"
    retryable: Final = True

    def __init__(self, operation: str) -> None:
        super().__init__(f"almacén de objetos no disponible ({operation})")
        self.operation = operation
        self.retry_after_seconds = RETRY_AFTER_SECONDS


class AddressingStyle(enum.StrEnum):
    VIRTUAL = "virtual"
    PATH = "path"


class ChecksumType(enum.StrEnum):
    """``x-amz-checksum-type``: suma del objeto entero o compuesta de las partes."""

    FULL_OBJECT = "FULL_OBJECT"
    COMPOSITE = "COMPOSITE"


@dataclass(frozen=True, slots=True)
class StorageCredentials:
    """Credenciales explícitas, solo para el almacén local; en AWS, las del rol de la tarea."""

    access_key_id: str
    secret_access_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """Configuración de un depósito. Por defecto, el punto de conexión regional de AWS."""

    bucket: str
    endpoint_url: str = REGIONAL_ENDPOINT
    region: str = STORE_REGION
    addressing_style: AddressingStyle = AddressingStyle.VIRTUAL
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = READ_TIMEOUT_SECONDS
    max_pool_connections: int = 10
    credentials: StorageCredentials | None = None

    def __post_init__(self) -> None:
        if not self.bucket:
            raise ValueError("el depósito es obligatorio")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("los tiempos de espera deben ser positivos")
        if self.max_pool_connections < 1:
            raise ValueError("max_pool_connections debe ser al menos 1")

    @property
    def call_timeout_seconds(self) -> float:
        """Tope de una llamada en el bucle de eventos: conexión + lectura + 1 s."""
        timeouts = self.connect_timeout_seconds + self.read_timeout_seconds
        return timeouts + CALL_TIMEOUT_MARGIN_SECONDS

    def botocore_config(self) -> Config:
        """Tiempos de espera fijos, un solo intento y firma SigV4 (PAT-NUC-RES-03)."""
        return Config(
            region_name=self.region,
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=self.read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            max_pool_connections=self.max_pool_connections,
            signature_version="s3v4",
            s3={"addressing_style": self.addressing_style.value},
        )


@dataclass(frozen=True, slots=True)
class ObjectHead:
    """Metadatos de un objeto tal como los devuelve ``HEAD`` con ``ChecksumMode=ENABLED``.

    ``checksum_sha256`` es el valor de ``x-amz-checksum-sha256`` en base64 (con el sufijo
    ``-<partes>`` si es compuesta) o ``None`` si el objeto se subió sin suma SHA-256. Las claves
    de ``metadata`` van en minúsculas y sin el prefijo ``x-amz-meta-``.
    """

    key: str
    size_bytes: int
    checksum_sha256: str | None
    checksum_type: ChecksumType | None
    content_type: str | None
    metadata: Mapping[str, str]
    version_id: str | None

    @property
    def full_object_sha256_hex(self) -> str | None:
        """SHA-256 del objeto entero en hexadecimal, o ``None`` si el almacén no la tiene.

        Una suma compuesta (subida por partes) no es la SHA-256 de los bytes: devuelve ``None``.
        """
        if self.checksum_sha256 is None or self.checksum_type is ChecksumType.COMPOSITE:
            return None
        try:
            digest = base64.b64decode(self.checksum_sha256, validate=True)
        except (binascii.Error, ValueError):
            return None
        return digest.hex() if len(digest) == 32 else None


@dataclass(frozen=True, slots=True)
class PresignedRequest:
    """Una URL prefirmada con las cabeceras que el cliente debe enviar **exactamente**."""

    method: str
    url: str
    headers: Mapping[str, str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedPart:
    """Parte ya subida: número, ``ETag`` que devolvió el almacén y su SHA-256 en hexadecimal."""

    part_number: int
    etag: str
    checksum_sha256: str


# --- Puerto ------------------------------------------------------------------------------------


class StoragePort(Protocol):
    """Operaciones del almacén sobre un depósito (LC-NUC-30). Sin listado de objetos."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest: ...

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead: ...

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest: ...

    async def create_multipart(
        self, key: str, content_type: str, *, metadata: Mapping[str, str] | None = None
    ) -> str: ...

    async def presign_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        checksum_sha256: str,
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest: ...

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectHead: ...

    async def abort_multipart(self, key: str, upload_id: str) -> None: ...


# --- Utilidades --------------------------------------------------------------------------------


def sha256_b64(data: bytes) -> str:
    """Valor de ``x-amz-checksum-sha256`` de unos bytes: SHA-256 en base64 con relleno."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def sha256_hex_to_b64(checksum_hex: str) -> str:
    """SHA-256 en hexadecimal en minúsculas (como en el contrato) → base64 de la cabecera."""
    if _SHA256_HEX.fullmatch(checksum_hex) is None:
        raise ValueError("la suma SHA-256 debe ser hexadecimal en minúsculas de 64 caracteres")
    return base64.b64encode(bytes.fromhex(checksum_hex)).decode("ascii")


def _require_key(key: str) -> None:
    """Clave con el patrón de ``StorageKey`` y sin segmentos vacíos, ``.`` ni ``..``."""
    if not isinstance(key, str) or _STORAGE_KEY.fullmatch(key) is None:
        raise ValueError("clave de objeto no válida")
    if any(not segment or segment in _KEY_DOT_SEGMENTS for segment in key.split("/")):
        raise ValueError("clave de objeto no válida")


def _require_version_id(version_id: str | None) -> None:
    if version_id is not None and (
        not isinstance(version_id, str) or _VERSION_ID.fullmatch(version_id) is None
    ):
        raise ValueError("identificador de versión no válido")


def _require_ttl(ttl: timedelta, maximum: timedelta) -> int:
    if ttl <= timedelta(0) or ttl > maximum:
        raise ValueError(f"la vigencia debe estar entre 1 s y {int(maximum.total_seconds())} s")
    seconds = int(ttl.total_seconds())
    if seconds < 1 or timedelta(seconds=seconds) != ttl:
        raise ValueError("la vigencia debe ser un número entero de segundos")
    return seconds


def _metadata_from_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """``x-amz-meta-<nombre>: valor`` → ``{nombre: valor}``; cualquier otra cabecera se rechaza.

    El tipo de contenido y la suma los fija el puerto; en ``required_headers`` solo caben
    metadatos de usuario, en minúsculas y con valores ASCII visibles.
    """
    metadata: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in (CHECKSUM_HEADER, CONTENT_TYPE_HEADER):
            raise ValueError(f"{lowered} la fija el puerto, no required_headers")
        if not lowered.startswith(_METADATA_HEADER_PREFIX):
            raise ValueError("required_headers solo admite metadatos x-amz-meta-*")
        _require_metadata(lowered.removeprefix(_METADATA_HEADER_PREFIX), value)
        metadata[lowered.removeprefix(_METADATA_HEADER_PREFIX)] = value
    return metadata


def _require_metadata(name: str, value: str) -> None:
    if _METADATA_NAME.fullmatch(name) is None or _METADATA_VALUE.fullmatch(value) is None:
        raise ValueError("metadato de usuario no válido")


def _checked_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    checked = dict(metadata or {})
    for name, value in checked.items():
        _require_metadata(name, value)
    return checked


def _is_transient(error: Exception) -> bool:
    if isinstance(
        error,
        botocore_exceptions.ConnectionError | botocore_exceptions.HTTPClientError,
    ):
        return True
    if isinstance(error, botocore_exceptions.ClientError):
        response = error.response
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = response.get("Error", {}).get("Code")
        return status in _TRANSIENT_HTTP_STATUS or code in _TRANSIENT_ERROR_CODES
    return False


def _http_status(error: botocore_exceptions.ClientError) -> int | None:
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _object_head(key: str, response: Mapping[str, Any]) -> ObjectHead:
    checksum_type: ChecksumType | None
    try:
        checksum_type = ChecksumType(str(response.get("ChecksumType")))
    except ValueError:
        checksum_type = None
    raw_metadata = response.get("Metadata", {})
    metadata = {str(name).lower(): str(value) for name, value in raw_metadata.items()}
    return ObjectHead(
        key=key,
        size_bytes=int(response["ContentLength"]),
        checksum_sha256=response.get("ChecksumSHA256"),
        checksum_type=checksum_type,
        content_type=response.get("ContentType"),
        metadata=metadata,
        version_id=response.get("VersionId"),
    )


# --- Adaptador de S3 ---------------------------------------------------------------------------


class S3Storage:
    """``StoragePort`` sobre boto3. El cliente es seguro entre hilos y se comparte."""

    def __init__(
        self,
        settings: StorageSettings,
        clock: Clock,
        *,
        client: Any | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._executor = executor
        self._client = client if client is not None else self._build_client(settings)

    @staticmethod
    def _build_client(settings: StorageSettings) -> Any:
        credentials: dict[str, str] = {}
        if settings.credentials is not None:
            credentials = {
                "aws_access_key_id": settings.credentials.access_key_id,
                "aws_secret_access_key": settings.credentials.secret_access_key,
            }
        return boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            config=settings.botocore_config(),
            **credentials,
        )

    @property
    def bucket(self) -> str:
        return self._settings.bucket

    async def _call[T](self, operation: str, function: Callable[[], T]) -> T:
        """Ejecuta ``function`` en el pool de hilos con tope; lo transitorio da StorageUnavailable.

        Al vencer el tope se abandona la llamada sin esperarla: el hilo termina solo, en el
        tiempo de espera de boto3.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, function)
        try:
            async with asyncio.timeout(self._settings.call_timeout_seconds):
                return await future
        except TimeoutError as error:
            raise StorageUnavailable(operation) from error
        except Exception as error:
            if _is_transient(error):
                raise StorageUnavailable(operation) from error
            raise

    def _presign(self, method: str, params: Mapping[str, Any], seconds: int) -> str:
        url: str = self._client.generate_presigned_url(
            method, Params=dict(params), ExpiresIn=seconds
        )
        return url

    # --- lectura -----------------------------------------------------------------------------

    async def head_object(self, key: str) -> ObjectHead | None:
        """Metadatos, suma SHA-256, tamaño y metadatos de usuario; ``None`` si no hay objeto."""
        _require_key(key)

        def head() -> ObjectHead | None:
            try:
                response = self._client.head_object(
                    Bucket=self._settings.bucket, Key=key, ChecksumMode="ENABLED"
                )
            except botocore_exceptions.ClientError as error:
                if _http_status(error) in _ABSENT_HTTP_STATUS:
                    return None
                raise
            return _object_head(key, response)

        return await self._call("head_object", head)

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        """URL de solo lectura, vigente a lo sumo 5 minutos (BR-NUC-66).

        Con ``version_id`` la URL lee esa versión y ninguna otra, aunque después llegue otra.
        """
        _require_key(key)
        _require_version_id(version_id)
        seconds = _require_ttl(ttl, PRESIGN_GET_MAX_TTL)
        issued_at = self._clock.now()
        params: dict[str, Any] = {"Bucket": self._settings.bucket, "Key": key}
        if version_id is not None:
            params["VersionId"] = version_id
        url = self._presign("get_object", params, seconds)
        return PresignedRequest("GET", url, {}, issued_at + timedelta(seconds=seconds))

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        """Los bytes del objeto (o de ``version_id``); boto3 valida la suma de la respuesta."""
        _require_key(key)
        _require_version_id(version_id)
        params: dict[str, Any] = {
            "Bucket": self._settings.bucket,
            "Key": key,
            "ChecksumMode": "ENABLED",
        }
        if version_id is not None:
            params["VersionId"] = version_id

        def get() -> bytes:
            response = self._client.get_object(**params)
            body: bytes = response["Body"].read()
            return body

        return await self._call("get_object", get)

    # --- escritura propia --------------------------------------------------------------------

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        """Sube con la suma SHA-256 de los bytes y cifrado ``aws:kms`` (archivado)."""
        _require_key(key)
        checked = _checked_metadata(metadata)
        params: dict[str, Any] = {
            "Bucket": self._settings.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "ChecksumSHA256": sha256_b64(body),
            "ServerSideEncryption": "aws:kms",
            "Metadata": checked,
        }
        if kms_key_id is not None:
            params["SSEKMSKeyId"] = kms_key_id

        def put() -> ObjectHead:
            response = self._client.put_object(**params)
            return ObjectHead(
                key=key,
                size_bytes=len(body),
                checksum_sha256=response.get("ChecksumSHA256"),
                checksum_type=ChecksumType.FULL_OBJECT,
                content_type=content_type,
                metadata=checked,
                version_id=response.get("VersionId"),
            )

        return await self._call("put_object", put)

    # --- subida prefirmada -------------------------------------------------------------------

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest:
        """URL de subida con suma, tipo y metadatos firmados; vigente a lo sumo 15 minutos.

        ``checksum_sha256`` va en hexadecimal (como ``ClipReference.sha256``); ``required_headers``
        solo admite ``x-amz-meta-*``. Devuelve las cabeceras que el nodo envía **tal cual**
        (``ClipUploadGrant.required_headers``), en minúsculas.
        """
        _require_key(key)
        seconds = _require_ttl(ttl, PRESIGN_PUT_MAX_TTL)
        checksum = sha256_hex_to_b64(checksum_sha256)
        metadata = _metadata_from_headers(required_headers)
        issued_at = self._clock.now()
        url = self._presign(
            "put_object",
            {
                "Bucket": self._settings.bucket,
                "Key": key,
                "ContentType": content_type,
                "ChecksumSHA256": checksum,
                "Metadata": metadata,
            },
            seconds,
        )
        headers = {CONTENT_TYPE_HEADER: content_type, CHECKSUM_HEADER: checksum}
        headers.update({f"{_METADATA_HEADER_PREFIX}{name}": v for name, v in metadata.items()})
        return PresignedRequest("PUT", url, headers, issued_at + timedelta(seconds=seconds))

    # --- subida por partes -------------------------------------------------------------------

    async def create_multipart(
        self, key: str, content_type: str, *, metadata: Mapping[str, str] | None = None
    ) -> str:
        """Abre una subida por partes con suma SHA-256 por parte; devuelve su ``upload_id``."""
        _require_key(key)
        checked = _checked_metadata(metadata)

        def create() -> str:
            response = self._client.create_multipart_upload(
                Bucket=self._settings.bucket,
                Key=key,
                ContentType=content_type,
                ChecksumAlgorithm="SHA256",
                Metadata=checked,
            )
            upload_id: str = response["UploadId"]
            return upload_id

        return await self._call("create_multipart", create)

    async def presign_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        checksum_sha256: str,
        ttl: timedelta = PRESIGN_PUT_MAX_TTL,
    ) -> PresignedRequest:
        """URL de una parte (1 a 10 000) con su suma SHA-256 firmada."""
        _require_key(key)
        if not upload_id:
            raise ValueError("upload_id es obligatorio")
        if type(part_number) is not int or not 1 <= part_number <= _MAX_PART_NUMBER:
            raise ValueError("part_number debe estar entre 1 y 10000")
        seconds = _require_ttl(ttl, PRESIGN_PUT_MAX_TTL)
        checksum = sha256_hex_to_b64(checksum_sha256)
        issued_at = self._clock.now()
        url = self._presign(
            "upload_part",
            {
                "Bucket": self._settings.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
                "ChecksumSHA256": checksum,
            },
            seconds,
        )
        return PresignedRequest(
            "PUT", url, {CHECKSUM_HEADER: checksum}, issued_at + timedelta(seconds=seconds)
        )

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectHead:
        """Cierra la subida con las partes y sus sumas; devuelve los metadatos del objeto."""
        _require_key(key)
        if not parts:
            raise ValueError("una subida por partes necesita al menos una parte")
        listed = [
            {
                "PartNumber": part.part_number,
                "ETag": part.etag,
                "ChecksumSHA256": sha256_hex_to_b64(part.checksum_sha256),
            }
            for part in sorted(parts, key=lambda part: part.part_number)
        ]

        def complete() -> None:
            self._client.complete_multipart_upload(
                Bucket=self._settings.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": listed},
            )

        await self._call("complete_multipart", complete)
        head = await self.head_object(key)
        if head is None:
            raise StorageUnavailable("complete_multipart")
        return head

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        """Aborta la subida y libera sus partes."""
        _require_key(key)

        def abort() -> None:
            self._client.abort_multipart_upload(
                Bucket=self._settings.bucket, Key=key, UploadId=upload_id
            )

        await self._call("abort_multipart", abort)
