"""Adaptadores del gestor de secretos y de KMS (LC-NUC-28; PAT-NUC-SEG-04, RES-02, RES-03).

Dos puertos, cada uno con **un** adaptador sobre boto3:

- ``SecretsPort``: ``get(arn) -> bytes`` lee un secreto por ARN o nombre con una caché en
  memoria de 5 minutos, y ``create(name, value) -> arn`` crea uno nuevo (la rotación de claves de
  firma, BR-NUC-85: cada versión de clave es un secreto propio, nunca se sobrescribe ni se borra
  uno anterior). Los valores son bytes (``SecretBinary``); nunca aparecen en un ``repr``, un
  registro ni un mensaje de error.
- ``KmsPort``: ``generate_data_key(key_id, context=…)`` y ``decrypt(wrapped, key_id=…,
  context=…)`` para el cifrado de sobre (LC-NUC-27) con la clave simétrica ``vigia-secrets``; el
  contexto de cifrado liga cada clave de datos a su propósito y el descifrado fija la clave
  maestra esperada. ``sign(key_id, message)`` y
  ``get_public_key(key_id)`` sobre la clave asimétrica ``vigia-node-ca`` (ECDSA P-256 con
  SHA-256, ``infrastructure-design.md`` §7.1). KMS no ofrece Ed25519 (cierre de R7): las claves
  Ed25519 de ``shared.signing`` viven en el gestor de secretos, no aquí.

Tiempo de espera (NFR-NUC-36, PAT-NUC-RES-03): **5 s** por llamada, contados en el bucle de
eventos sobre la llamada entera; boto3 lleva además conexión y lectura de 5 s y un solo intento.
boto3 es bloqueante: cada llamada corre en el pool de hilos. Una dependencia que no responde, que
rechaza la conexión, que responde 5xx, que limita o que niega el acceso termina en
``SecretsUnavailable`` (transitorio: hacia la API, ``temporarily_unavailable``).

Degradación declarada (FS-NUC-05):

- **al arrancar**, ``load_required`` falla cerrado con ``SecretsStartupError`` si no puede leer
  todo lo requerido, y el proceso no queda ``ready`` (PAT-NUC-RES-02);
- **en operación**, ``get`` sirve el último valor leído cuando la relectura falla (el refresco
  cada 5 minutos no deja al proceso sin claves) y suma 1 a ``secrets_refresh_failed``, que
  alerta. Un secreto que nunca se pudo leer no tiene respaldo: ``SecretsUnavailable``.

Ningún módulo de aquí lee la hora del sistema: la caducidad de la caché usa ``Clock.monotonic``.
"""

from __future__ import annotations

import asyncio
import enum
import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import boto3  # type: ignore[import-untyped]
from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "SECRETS_CACHE_TTL_SECONDS",
    "AwsCredentials",
    "AwsSettings",
    "DataKey",
    "Dependency",
    "KmsAdapter",
    "KmsPort",
    "SecretNotFound",
    "SecretsManagerAdapter",
    "SecretsPort",
    "SecretsStartupError",
    "SecretsUnavailable",
    "load_required",
]

CALL_TIMEOUT_SECONDS: Final = 5.0
"""Tope de cada llamada al gestor de secretos o a KMS (PAT-NUC-RES-03) ``[objetivo propio]``."""
SECRETS_CACHE_TTL_SECONDS: Final = 300.0
"""Vigencia de un secreto leído en la caché en memoria: 5 minutos (LC-NUC-28)."""
RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` de ``temporarily_unavailable`` ``[objetivo propio]``."""

DATA_KEY_SPEC: Final = "AES_256"
NODE_CA_SIGNING_ALGORITHM: Final = "ECDSA_SHA_256"
"""Perfil de firma de ``vigia-node-ca`` (NFR-CTR-13; ``infrastructure-design.md`` §7.1)."""

_SECRET_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_+=.@-]{0,2047}")
"""ARN o nombre de secreto: los caracteres que admite el servicio, sin espacios ni controles."""
_SECRET_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_+=.@-]{0,511}")
_KMS_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_-]{0,2047}")
_MAX_SECRET_BYTES: Final = 65_536
"""Tamaño máximo de un secreto en el servicio."""
_MAX_SIGN_MESSAGE_BYTES: Final = 4_096
"""Mensaje máximo de ``kms:Sign`` con ``MessageType=RAW``."""
_CONTEXT_TOKEN: Final = re.compile(r"[a-z][a-z0-9_:.-]{0,63}")
_MAX_CONTEXT_PAIRS: Final = 8
_KMS_REJECTED_CIPHERTEXT: Final = frozenset({"InvalidCiphertextException", "IncorrectKeyException"})
"""Rechazos de ``kms:Decrypt`` por clave envuelta, clave maestra o contexto que no corresponden."""

_log = get_logger("shared.secrets")


class Dependency(enum.StrEnum):
    """Dependencia externa que falló (atributo de ``secrets_refresh_failed``)."""

    SECRETS_MANAGER = "secrets_manager"
    KMS = "kms"


# --- Errores y valores -------------------------------------------------------------------------


class SecretsUnavailable(Exception):
    """El gestor de secretos o KMS no respondió a tiempo o no es accesible: transitorio.

    Hacia la API se traduce a ``temporarily_unavailable`` (PAT-NUC-RES-03). El mensaje nombra la
    dependencia y la operación; nunca un ARN, un valor ni el detalle del servicio.
    """

    code: Final = "temporarily_unavailable"
    retryable: Final = True

    def __init__(self, dependency: Dependency, operation: str) -> None:
        super().__init__(f"{dependency.value} no disponible ({operation})")
        self.dependency = dependency
        self.operation = operation
        self.retry_after_seconds = RETRY_AFTER_SECONDS


class SecretNotFound(Exception):
    """El secreto no existe (o está marcado para borrado): no es transitorio."""

    code: Final = "secret_not_found"

    def __init__(self) -> None:
        super().__init__("el secreto no existe")


class SecretsStartupError(Exception):
    """No se pudo cargar un secreto requerido al arrancar: el proceso no queda ``ready``.

    Solo lleva cuántos fallaron y la causa de cada uno (``unavailable`` o ``not_found``), en el
    orden pedido; nunca el ARN ni el valor.
    """

    def __init__(self, causes: Iterable[str]) -> None:
        self.causes = tuple(causes)
        super().__init__(
            f"no se pudieron cargar {len(self.causes)} secretos requeridos al arrancar"
        )


@dataclass(frozen=True, slots=True)
class DataKey:
    """Clave de datos de KMS: en claro (solo memoria) y envuelta con la clave maestra."""

    plaintext: bytes = field(repr=False)
    wrapped: bytes
    key_id: str


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """Credenciales explícitas, solo para LocalStack; en AWS, las del rol de la tarea."""

    access_key_id: str
    secret_access_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AwsSettings:
    """Punto de conexión y tiempos de un cliente de Secrets Manager o KMS."""

    region: str
    endpoint_url: str | None = None
    """``None`` en AWS (punto privado de la VPC por DNS); LocalStack en pruebas."""
    connect_timeout_seconds: float = CALL_TIMEOUT_SECONDS
    read_timeout_seconds: float = CALL_TIMEOUT_SECONDS
    call_timeout_seconds: float = CALL_TIMEOUT_SECONDS
    max_pool_connections: int = 10
    credentials: AwsCredentials | None = None

    def __post_init__(self) -> None:
        if not self.region:
            raise ValueError("la región es obligatoria")
        timeouts = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.call_timeout_seconds,
        )
        if any(value <= 0 for value in timeouts):
            raise ValueError("los tiempos de espera deben ser positivos")
        if self.call_timeout_seconds > CALL_TIMEOUT_SECONDS:
            raise ValueError(f"el tope de una llamada no supera {CALL_TIMEOUT_SECONDS} s")
        if self.max_pool_connections < 1:
            raise ValueError("max_pool_connections debe ser al menos 1")

    def botocore_config(self) -> Config:
        """Tiempos de espera fijos y un solo intento (PAT-NUC-RES-03)."""
        return Config(
            region_name=self.region,
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=self.read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            max_pool_connections=self.max_pool_connections,
        )

    def make_client(self, service_name: str) -> Any:
        """Cliente de boto3 con ``botocore_config()`` (tiempos de espera y un solo intento)."""
        # Argumentos con nombre, sin ``**``: VIG003 ve la llamada completa con su ``config``.
        # Sin credenciales explícitas (``None``), boto3 usa las del rol de la tarea.
        explicit = self.credentials
        return boto3.client(
            service_name,
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            config=self.botocore_config(),
            aws_access_key_id=None if explicit is None else explicit.access_key_id,
            aws_secret_access_key=None if explicit is None else explicit.secret_access_key,
        )


# --- Puertos -----------------------------------------------------------------------------------


class SecretsPort(Protocol):
    """Lectura y alta de secretos (LC-NUC-28). Sin borrado: solo la identidad administrativa."""

    async def get(self, arn: str) -> bytes: ...

    async def create(self, name: str, value: bytes) -> str: ...


class KmsPort(Protocol):
    """Operaciones de KMS que usa la plataforma (LC-NUC-27, LC-NUC-28, U-03).

    Toda clave de datos va ligada a su propósito con un contexto de cifrado (``context``), y el
    descifrado fija la clave maestra esperada (``key_id``): una clave envuelta de otro propósito,
    o envuelta con otra clave maestra, no se descifra.
    """

    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey: ...

    async def decrypt(
        self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]
    ) -> bytes: ...

    async def sign(self, key_id: str, message: bytes) -> bytes: ...

    async def get_public_key(self, key_id: str) -> bytes: ...


# --- Utilidades --------------------------------------------------------------------------------


def _require(pattern: re.Pattern[str], value: object, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{what} no válido")
    return value


def _require_bytes(value: object, what: str, maximum: int) -> bytes:
    if not isinstance(value, bytes) or not 0 < len(value) <= maximum:
        raise ValueError(f"{what} debe ser de 1 a {maximum} bytes")
    return value


def _error_code(error: botocore_exceptions.ClientError) -> str:
    code = error.response.get("Error", {}).get("Code")
    return code if isinstance(code, str) else ""


async def _call[T](
    settings: AwsSettings,
    executor: Executor | None,
    dependency: Dependency,
    operation: str,
    function: Callable[[], T],
) -> T:
    """Ejecuta ``function`` en el pool de hilos con tope; un fallo de AWS da ``SecretsUnavailable``.

    Al vencer el tope se abandona la llamada sin esperarla: el hilo termina solo, en el tiempo de
    espera de boto3. ``SecretNotFound`` y los ``ValueError`` de validación pasan tal cual.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(executor, function)
    try:
        async with asyncio.timeout(settings.call_timeout_seconds):
            return await future
    except (SecretNotFound, ValueError):
        raise
    except (TimeoutError, botocore_exceptions.BotoCoreError, botocore_exceptions.ClientError):
        # Conexión rechazada o cortada, tiempo de espera, 5xx, limitación y acceso negado: el
        # proceso no distingue entre ellos (la causa queda en la traza, no en el mensaje).
        raise SecretsUnavailable(dependency, operation) from None


# --- Secrets Manager ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Cached:
    value: bytes = field(repr=False)
    fetched_at: float


class SecretsManagerAdapter:
    """``SecretsPort`` sobre boto3 con caché de 5 minutos por ARN o nombre."""

    def __init__(
        self,
        settings: AwsSettings,
        clock: Clock,
        *,
        kms_key_id: str | None = None,
        client: Any | None = None,
        executor: Executor | None = None,
        metrics: PlatformMetrics | None = None,
        cache_ttl_seconds: float = SECRETS_CACHE_TTL_SECONDS,
    ) -> None:
        if kms_key_id is not None:
            _require(_KMS_KEY_ID, kms_key_id, "kms_key_id")
        if cache_ttl_seconds <= 0 or cache_ttl_seconds > SECRETS_CACHE_TTL_SECONDS:
            raise ValueError(f"la caché dura de 0 a {SECRETS_CACHE_TTL_SECONDS} s")
        self._settings = settings
        self._clock = clock
        self._kms_key_id = kms_key_id
        self._client = client if client is not None else settings.make_client("secretsmanager")
        self._executor = executor
        self._metrics = metrics
        self._ttl = cache_ttl_seconds
        self._cache: dict[str, _Cached] = {}

    def __repr__(self) -> str:
        return f"SecretsManagerAdapter(cached={len(self._cache)})"

    async def get(self, arn: str) -> bytes:
        """El valor binario del secreto; de la caché si se leyó hace menos de 5 minutos.

        Si la relectura falla y hay un valor anterior, lo devuelve y suma 1 a
        ``secrets_refresh_failed`` (degradación declarada). Sin valor anterior, el error sube.
        """
        _require(_SECRET_ID, arn, "el identificador del secreto")
        cached = self._cache.get(arn)
        now = self._clock.monotonic()
        if cached is not None and now - cached.fetched_at < self._ttl:
            return cached.value

        def fetch() -> bytes:
            try:
                response = self._client.get_secret_value(SecretId=arn)
            except botocore_exceptions.ClientError as error:
                if _error_code(error) == "ResourceNotFoundException":
                    raise SecretNotFound from None
                raise
            value = response.get("SecretBinary")
            if not isinstance(value, bytes) or not value:
                # Un secreto de texto no es un secreto de la plataforma: no se interpreta.
                raise ValueError("el secreto no tiene valor binario")
            return value

        try:
            value = await _call(
                self._settings, self._executor, Dependency.SECRETS_MANAGER, "get_secret", fetch
            )
        except SecretsUnavailable:
            if cached is None:
                raise
            self._refresh_failed()
            return cached.value
        self._cache[arn] = _Cached(value, self._clock.monotonic())
        return value

    async def create(self, name: str, value: bytes) -> str:
        """Crea el secreto ``name`` con ``value`` (cifrado con ``kms_key_id``) y devuelve su ARN.

        Un nombre ya existente es un error (``ValueError``): una versión de clave nunca
        sobrescribe a otra.
        """
        _require(_SECRET_NAME, name, "el nombre del secreto")
        _require_bytes(value, "el valor del secreto", _MAX_SECRET_BYTES)
        params: dict[str, Any] = {"Name": name, "SecretBinary": value}
        if self._kms_key_id is not None:
            params["KmsKeyId"] = self._kms_key_id

        def put() -> str:
            try:
                response = self._client.create_secret(**params)
            except botocore_exceptions.ClientError as error:
                if _error_code(error) == "ResourceExistsException":
                    raise ValueError("el secreto ya existe") from None
                raise
            arn = response.get("ARN")
            if not isinstance(arn, str) or _SECRET_ID.fullmatch(arn) is None:
                raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "create_secret")
            return arn

        arn = await _call(
            self._settings, self._executor, Dependency.SECRETS_MANAGER, "create_secret", put
        )
        now = self._clock.monotonic()
        self._cache[arn] = _Cached(value, now)
        self._cache[name] = _Cached(value, now)
        return arn

    async def put_one_time(self, name: str, value: str) -> str:
        """Deja ``value`` (texto) en el secreto de un solo uso ``name`` y devuelve su ARN.

        Para ``vigia/<entorno>/bootstrap/invitation`` (``infrastructure-design.md`` §5.4): lo
        crea o, si ya existe, le añade una versión nueva (``PutSecretValue``); el dueño lo lee y
        lo borra tras usarlo. Es texto para leerlo con ``aws secretsmanager get-secret-value``;
        la plataforma nunca lo lee, y el valor **no** se guarda en la caché.
        """
        _require(_SECRET_NAME, name, "el nombre del secreto")
        if not isinstance(value, str) or not 0 < len(value.encode("utf-8")) <= _MAX_SECRET_BYTES:
            raise ValueError(
                f"el valor del secreto debe ser texto de 1 a {_MAX_SECRET_BYTES} bytes"
            )
        params: dict[str, Any] = {"Name": name, "SecretString": value}
        if self._kms_key_id is not None:
            params["KmsKeyId"] = self._kms_key_id

        def put() -> str:
            try:
                response = self._client.create_secret(**params)
            except botocore_exceptions.ClientError as error:
                if _error_code(error) != "ResourceExistsException":
                    raise
                response = self._client.put_secret_value(SecretId=name, SecretString=value)
            arn = response.get("ARN")
            if not isinstance(arn, str) or _SECRET_ID.fullmatch(arn) is None:
                raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "put_secret")
            return arn

        return await _call(
            self._settings, self._executor, Dependency.SECRETS_MANAGER, "put_secret", put
        )

    def _refresh_failed(self) -> None:
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.secrets_refresh_failed.add(1, {"dependency": Dependency.SECRETS_MANAGER.value})
        _log.warning("relectura de un secreto fallida; se usa el valor en memoria")


async def load_required(secrets: SecretsPort, arns: Iterable[str]) -> dict[str, bytes]:
    """Lee todos los secretos requeridos para arrancar; si falta uno, ``SecretsStartupError``.

    Fallo cerrado (PAT-NUC-RES-02): un proceso que no pudo leer lo requerido no queda ``ready``.
    Las lecturas son concurrentes, así que el arranque tarda a lo sumo un tope de llamada.
    """
    wanted = list(dict.fromkeys(arns))
    results = await asyncio.gather(*(secrets.get(arn) for arn in wanted), return_exceptions=True)
    loaded: dict[str, bytes] = {}
    causes: list[str] = []
    for arn, result in zip(wanted, results, strict=True):
        if isinstance(result, bytes):
            loaded[arn] = result
        elif isinstance(result, SecretNotFound):
            causes.append("not_found")
        elif isinstance(result, SecretsUnavailable | ValueError):
            causes.append("unavailable")
        else:
            raise result
    if causes:
        _log.error("arranque sin secretos requeridos: el proceso no queda listo")
        raise SecretsStartupError(causes)
    return loaded


# --- KMS ---------------------------------------------------------------------------------------


class KmsAdapter:
    """``KmsPort`` sobre boto3."""

    def __init__(
        self,
        settings: AwsSettings,
        *,
        client: Any | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._client = client if client is not None else settings.make_client("kms")
        self._executor = executor

    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey:
        """Clave de datos AES-256 nueva, en claro y envuelta con ``key_id`` (``vigia-secrets``),
        ligada a ``context`` (contexto de cifrado de KMS)."""
        _require(_KMS_KEY_ID, key_id, "el identificador de la clave KMS")
        encryption_context = _encryption_context(context)

        def generate() -> DataKey:
            response = self._client.generate_data_key(
                KeyId=key_id, KeySpec=DATA_KEY_SPEC, EncryptionContext=encryption_context
            )
            return DataKey(
                plaintext=_response_bytes(response, "Plaintext"),
                wrapped=_response_bytes(response, "CiphertextBlob"),
                key_id=str(response.get("KeyId", key_id)),
            )

        return await self._run("generate_data_key", generate)

    async def decrypt(self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]) -> bytes:
        """La clave de datos en claro, solo si ``wrapped`` se envolvió con ``key_id`` y ``context``.

        Una clave envuelta con otra clave maestra, con otro contexto o alterada no es un fallo
        transitorio: ``ValueError`` (nunca ``SecretsUnavailable``).
        """
        _require_bytes(wrapped, "la clave envuelta", 6_144)
        _require(_KMS_KEY_ID, key_id, "el identificador de la clave KMS")
        encryption_context = _encryption_context(context)

        def decrypt() -> bytes:
            try:
                response = self._client.decrypt(
                    CiphertextBlob=wrapped, KeyId=key_id, EncryptionContext=encryption_context
                )
            except botocore_exceptions.ClientError as error:
                if _error_code(error) in _KMS_REJECTED_CIPHERTEXT:
                    raise ValueError(
                        "la clave envuelta no corresponde a la clave ni al contexto"
                    ) from None
                raise
            return _response_bytes(response, "Plaintext")

        return await self._run("decrypt", decrypt)

    async def sign(self, key_id: str, message: bytes) -> bytes:
        """Firma DER ECDSA P-256 con SHA-256 de ``message`` con la clave asimétrica ``key_id``."""
        _require(_KMS_KEY_ID, key_id, "el identificador de la clave KMS")
        _require_bytes(message, "el mensaje", _MAX_SIGN_MESSAGE_BYTES)

        def sign() -> bytes:
            response = self._client.sign(
                KeyId=key_id,
                Message=message,
                MessageType="RAW",
                SigningAlgorithm=NODE_CA_SIGNING_ALGORITHM,
            )
            return _response_bytes(response, "Signature")

        return await self._run("sign", sign)

    async def get_public_key(self, key_id: str) -> bytes:
        """Clave pública DER (``SubjectPublicKeyInfo``) de la clave asimétrica ``key_id``."""
        _require(_KMS_KEY_ID, key_id, "el identificador de la clave KMS")

        def public_key() -> bytes:
            response = self._client.get_public_key(KeyId=key_id)
            return _response_bytes(response, "PublicKey")

        return await self._run("get_public_key", public_key)

    async def _run[T](self, operation: str, function: Callable[[], T]) -> T:
        return await _call(self._settings, self._executor, Dependency.KMS, operation, function)


def _encryption_context(context: object) -> dict[str, str]:
    """Contexto de cifrado de KMS: de 1 a 8 pares de identificadores cortos, sin texto libre."""
    if not isinstance(context, Mapping) or not 0 < len(context) <= _MAX_CONTEXT_PAIRS:
        raise ValueError(f"el contexto de cifrado debe tener de 1 a {_MAX_CONTEXT_PAIRS} pares")
    for key, value in context.items():
        _require(_CONTEXT_TOKEN, key, "la clave del contexto de cifrado")
        _require(_CONTEXT_TOKEN, value, "el valor del contexto de cifrado")
    return dict(context)


def _response_bytes(response: Mapping[str, Any], name: str) -> bytes:
    value = response.get(name)
    if not isinstance(value, bytes) or not value:
        raise SecretsUnavailable(Dependency.KMS, "respuesta sin " + name)
    return value
