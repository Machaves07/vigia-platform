"""Cifrado de sobre con AES-256-GCM y clave de datos de KMS (LC-NUC-27; PAT-NUC-SEG-04, RES-03).

``EnvelopeCipher`` cifra secretos que la plataforma guarda en la base (hoy, el secreto TOTP de
``identity.auth.second_factor``) para que no existan en claro ni en la base ni en sus copias:

- ``encrypt(plaintext, aad) -> (ciphertext, wrapped_key)``: pide a KMS una clave de datos
  AES-256 nueva (``GenerateDataKey`` con la clave del cliente ``vigia-secrets``) **en cada
  llamada** y cifra con AES-256-GCM y ``aad`` como dato asociado (el identificador de la
  credencial). En la fila viajan ``ciphertext`` y ``wrapped_key``; la clave de datos en claro solo
  vive en la memoria del proceso. Una clave por secreto cifrado es la forma más estricta del
  "una clave por periodo" de PAT-NUC-SEG-04: una inscripción nueva exige siempre generar clave,
  así que sin KMS falla cerrada (FS-NUC-05 b).
- ``decrypt(ciphertext, wrapped_key, aad) -> bytes``: descifra la clave envuelta con KMS
  (``Decrypt``) y la guarda en una caché en memoria de **5 minutos**; dentro de ese plazo no
  vuelve a llamar a KMS.

Cada clave de datos se genera y se descifra con el contexto de cifrado ``{"vigia_purpose":
<propósito>}`` y el descifrado fija la clave maestra (``KeyId``): una clave envuelta de otro
propósito o de otra clave maestra termina en ``DecryptionFailed``.

Formato de ``ciphertext``: versión (1 byte, ``0x01``) + nonce aleatorio de 12 bytes + texto
cifrado con la etiqueta GCM de 16 bytes. Un ``ciphertext``, una clave envuelta o un ``aad`` que no
corresponden (otra fila, bytes alterados) terminan en ``DecryptionFailed``: nunca se devuelve un
texto que no se autenticó.

**Sin KMS** (FS-NUC-05 b): ``decrypt`` sigue con la clave de datos que hay en memoria, también
pasados los 5 minutos (la relectura fallida suma 1 a ``secrets_refresh_failed`` con
``dependency=kms`` y se usa la clave anterior, como ``shared.secrets`` con sus secretos); lo
demás falla cerrado con ``SecretsUnavailable`` (``temporarily_unavailable``): cifrar, y descifrar
con una clave que nunca se tuvo en memoria. La caché tiene tope de entradas y descarta la más
antigua.

Las llamadas a KMS corren en el pool de hilos (``shared.secrets.KmsAdapter``). AES-GCM sobre a lo
sumo ``MAX_PLAINTEXT_BYTES`` tarda microsegundos y se hace en el bucle: enviarlo al pool costaría
más que el propio cifrado.

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy. No lee la hora del
sistema: la caducidad de la caché usa ``Clock.monotonic``. Ningún ``repr``, registro ni mensaje
de error lleva una clave, un texto en claro ni un texto cifrado.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, NamedTuple, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.secrets import Dependency, KmsPort, SecretsUnavailable

__all__ = [
    "DATA_KEY_BYTES",
    "DATA_KEY_CACHE_MAX_ENTRIES",
    "DATA_KEY_CACHE_TTL_SECONDS",
    "ENVELOPE_VERSION",
    "MAX_AAD_BYTES",
    "MAX_CIPHERTEXT_BYTES",
    "MAX_PLAINTEXT_BYTES",
    "MAX_WRAPPED_KEY_BYTES",
    "NONCE_BYTES",
    "DecryptionFailed",
    "EnvelopeCipher",
    "EnvelopeCiphertext",
    "EnvelopeCrypto",
]

ENVELOPE_VERSION: Final = 1
"""Primer byte de ``ciphertext``: permite cambiar el formato sin reinterpretar filas viejas."""
NONCE_BYTES: Final = 12
"""Nonce de AES-GCM (96 bits), aleatorio por cifrado."""
TAG_BYTES: Final = 16
DATA_KEY_BYTES: Final = 32
"""Clave de datos AES-256 (``KeySpec=AES_256``)."""
DATA_KEY_CACHE_TTL_SECONDS: Final = 300.0
"""Vigencia de una clave de datos descifrada en la caché: 5 minutos (LC-NUC-27)."""
DATA_KEY_CACHE_MAX_ENTRIES: Final = 4_096
"""Tope de claves de datos en memoria ``[objetivo propio]``: una por credencial activa."""

MAX_WRAPPED_KEY_BYTES: Final = 1_024
"""Tope de la clave envuelta (restricción ``totp_credential_data_key_length``)."""
MAX_CIPHERTEXT_BYTES: Final = 1_024
"""Tope del texto cifrado (restricción ``totp_credential_secret_length``)."""
MAX_PLAINTEXT_BYTES: Final = MAX_CIPHERTEXT_BYTES - 1 - NONCE_BYTES - TAG_BYTES
"""Lo más largo que cabe cifrado en ``MAX_CIPHERTEXT_BYTES``."""
MAX_AAD_BYTES: Final = 256
_MIN_CIPHERTEXT_BYTES: Final = 1 + NONCE_BYTES + TAG_BYTES

PURPOSE_CONTEXT_KEY: Final = "vigia_purpose"
"""Clave del contexto de cifrado de KMS que liga cada clave de datos a su propósito."""
DEFAULT_PURPOSE: Final = "envelope"
_PURPOSE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")

_log = get_logger("shared.crypto")


class DecryptionFailed(Exception):
    """El texto cifrado no se autentica con esa clave y ese dato asociado, o está mal formado.

    No es transitorio: la fila no corresponde o se alteró. El mensaje es fijo.
    """

    code: Final = "decryption_failed"

    def __init__(self) -> None:
        super().__init__("el texto cifrado no se pudo autenticar")


class EnvelopeCiphertext(NamedTuple):
    """Resultado de ``encrypt``: se desempaqueta como ``ciphertext, wrapped_key``."""

    ciphertext: bytes
    wrapped_key: bytes

    def __repr__(self) -> str:
        return (
            f"EnvelopeCiphertext(ciphertext=<{len(self.ciphertext)} bytes>,"
            f" wrapped_key=<{len(self.wrapped_key)} bytes>)"
        )


class EnvelopeCrypto(Protocol):
    """Puerto del cifrado de sobre que usan los módulos de identidad."""

    async def encrypt(self, plaintext: bytes, aad: bytes) -> EnvelopeCiphertext: ...

    async def decrypt(self, ciphertext: bytes, wrapped_key: bytes, aad: bytes) -> bytes: ...


@dataclass(slots=True)
class _CachedKey:
    plaintext: bytes = field(repr=False)
    fetched_at: float


def _require_bytes(value: object, what: str, minimum: int, maximum: int) -> bytes:
    if not isinstance(value, bytes) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{what} debe ser bytes de {minimum} a {maximum}")
    return value


class EnvelopeCipher:
    """``EnvelopeCrypto`` con claves de datos de KMS y caché de 5 minutos (LC-NUC-27)."""

    def __init__(
        self,
        kms: KmsPort,
        key_id: str,
        clock: Clock,
        *,
        metrics: PlatformMetrics | None = None,
        cache_ttl_seconds: float = DATA_KEY_CACHE_TTL_SECONDS,
        max_cached_keys: int = DATA_KEY_CACHE_MAX_ENTRIES,
        random_bytes: Callable[[int], bytes] = os.urandom,
        purpose: str = DEFAULT_PURPOSE,
    ) -> None:
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("key_id es obligatorio")
        if not isinstance(purpose, str) or _PURPOSE.fullmatch(purpose) is None:
            raise ValueError("purpose debe ser un identificador en minúsculas")
        if not 0 < cache_ttl_seconds <= DATA_KEY_CACHE_TTL_SECONDS:
            raise ValueError(f"la caché dura de 0 a {DATA_KEY_CACHE_TTL_SECONDS} s")
        if max_cached_keys < 1:
            raise ValueError("max_cached_keys debe ser al menos 1")
        self._kms = kms
        self._key_id = key_id
        self._clock = clock
        self._metrics = metrics
        self._ttl = cache_ttl_seconds
        self._max_cached = max_cached_keys
        self._random_bytes = random_bytes
        self._context = {PURPOSE_CONTEXT_KEY: purpose}
        self._cache: dict[bytes, _CachedKey] = {}

    def __repr__(self) -> str:
        return f"EnvelopeCipher(cached={len(self._cache)})"

    async def encrypt(self, plaintext: bytes, aad: bytes) -> EnvelopeCiphertext:
        """Cifra ``plaintext`` con una clave de datos nueva de KMS y ``aad`` como dato asociado.

        Raises:
            SecretsUnavailable: KMS no respondió (``temporarily_unavailable``); nada se cifró.
            ValueError: ``plaintext`` o ``aad`` vacíos, demasiado largos o no son ``bytes``.
        """
        _require_bytes(plaintext, "plaintext", 1, MAX_PLAINTEXT_BYTES)
        _require_bytes(aad, "aad", 1, MAX_AAD_BYTES)
        data_key = await self._kms.generate_data_key(self._key_id, context=self._context)
        if len(data_key.plaintext) != DATA_KEY_BYTES or not (
            0 < len(data_key.wrapped) <= MAX_WRAPPED_KEY_BYTES
        ):
            raise SecretsUnavailable(Dependency.KMS, "clave de datos con forma inesperada")
        nonce = self._random_bytes(NONCE_BYTES)
        if len(nonce) != NONCE_BYTES:
            raise ValueError("el generador no devolvió un nonce de 12 bytes")
        sealed = AESGCM(data_key.plaintext).encrypt(nonce, plaintext, aad)
        self._remember(data_key.wrapped, data_key.plaintext)
        return EnvelopeCiphertext(bytes((ENVELOPE_VERSION,)) + nonce + sealed, data_key.wrapped)

    async def decrypt(self, ciphertext: bytes, wrapped_key: bytes, aad: bytes) -> bytes:
        """El texto en claro de ``ciphertext`` si se autentica con ``wrapped_key`` y ``aad``.

        Raises:
            DecryptionFailed: formato desconocido, bytes alterados, otra clave u otro ``aad``.
            SecretsUnavailable: KMS no respondió y la clave de datos no está en memoria.
        """
        try:
            _require_bytes(ciphertext, "ciphertext", _MIN_CIPHERTEXT_BYTES, MAX_CIPHERTEXT_BYTES)
            _require_bytes(wrapped_key, "wrapped_key", 1, MAX_WRAPPED_KEY_BYTES)
            _require_bytes(aad, "aad", 1, MAX_AAD_BYTES)
        except ValueError:
            raise DecryptionFailed() from None
        if ciphertext[0] != ENVELOPE_VERSION:
            raise DecryptionFailed()
        key = await self._data_key(wrapped_key)
        nonce, sealed = ciphertext[1 : 1 + NONCE_BYTES], ciphertext[1 + NONCE_BYTES :]
        try:
            return AESGCM(key).decrypt(nonce, sealed, aad)
        except InvalidTag:
            raise DecryptionFailed() from None

    async def _data_key(self, wrapped_key: bytes) -> bytes:
        cached = self._cache.get(wrapped_key)
        if cached is not None and self._clock.monotonic() - cached.fetched_at < self._ttl:
            return cached.plaintext
        try:
            plaintext = await self._kms.decrypt(
                wrapped_key, key_id=self._key_id, context=self._context
            )
        except ValueError:
            # Otra clave maestra, otro propósito o una clave envuelta alterada: no es transitorio.
            raise DecryptionFailed() from None
        except SecretsUnavailable:
            if cached is None:
                raise
            # Degradación declarada (FS-NUC-05 b): se sigue con la clave que hay en memoria.
            metrics = self._metrics if self._metrics is not None else get_metrics()
            metrics.secrets_refresh_failed.add(1, {"dependency": Dependency.KMS.value})
            _log.warning("KMS no responde; se descifra con la clave de datos en memoria")
            return cached.plaintext
        if len(plaintext) != DATA_KEY_BYTES:
            raise DecryptionFailed()
        self._remember(wrapped_key, plaintext)
        return plaintext

    def _remember(self, wrapped_key: bytes, plaintext: bytes) -> None:
        self._cache.pop(wrapped_key, None)
        while len(self._cache) >= self._max_cached:
            # dict conserva el orden de inserción: la primera es la más antigua.
            del self._cache[next(iter(self._cache))]
        self._cache[wrapped_key] = _CachedKey(plaintext, self._clock.monotonic())
