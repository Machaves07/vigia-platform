"""Los documentos firmados en ``vigia-evidence``: URL de subida y metadatos (LC-GOB-05).

Sobre el ``StoragePort`` heredado (LC-NUC-30), ligado al depósito de evidencias. Dos operaciones:

- ``prepare_upload(grant, now)``: comprueba con ``head_object`` que la clave nueva aún no tiene
  objeto y firma la URL de **un solo** ``PUT`` con ``presign_put``: vence a lo sumo con la
  concesión (15 minutos desde ``issued_at``) y lleva **firmados** el tipo de contenido y la suma
  SHA-256 concedidos (``x-amz-checksum-sha256`` en base64). Un ``PUT`` con otro tipo o sin la
  suma no pasa la firma; unos bytes con otra suma dan ``BadDigest``. La consulta previa hace que
  la concesión **falle cerrada** con el almacén caído (FS-GOB-01): firmar una URL no toca la red
  y, sin ella, se emitiría una concesión que nadie podría usar;
- ``heads(keys)``: los metadatos de cada objeto con ``ChecksumMode=ENABLED``, **en paralelo** y en
  el pool de hilos (PAT-GOB-REN-05). Nunca se descarga un documento: este adaptador no expone
  ``get_object``.

Tiempos de espera de NFR-GOB-43 ``[objetivos propios]``: firma de URL 5 s y consulta de metadatos
10 s, además de los de ``StorageSettings`` (conexión 5 s, lectura 10 s, sin reintentos). Vencer
cualquiera, o un fallo de ``botocore`` al firmar (credenciales del rol no disponibles), termina en
``StorageUnavailable``: transitorio, con ``retry_after_seconds``.

**Límite conocido (heredado de U-02).** ``S3Storage.presign_put`` llama a
``generate_presigned_url`` de forma síncrona dentro del bucle de eventos, sin pasar por el pool de
hilos: el tope de 5 s de la firma corta un almacén asíncrono colgado, pero no un bloqueo de
``botocore`` al refrescar las credenciales del rol. Corregirlo es de ``shared.storage``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, Protocol

from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]

from vigia_platform.catalog.domain.documents import DOCUMENT_GRANT_TTL, DocumentUploadGrant
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, StorageUnavailable

__all__ = [
    "HEAD_TIMEOUT_SECONDS",
    "PRESIGN_TIMEOUT_SECONDS",
    "DocumentKeyTaken",
    "DocumentObjectStore",
    "DocumentStorage",
]

PRESIGN_TIMEOUT_SECONDS: Final = 5.0
"""Firma de la URL prefirmada (NFR-GOB-43)."""
HEAD_TIMEOUT_SECONDS: Final = 10.0
"""Consulta de metadatos de un objeto (NFR-GOB-43)."""


class DocumentKeyTaken(Exception):
    """La clave nueva ya tiene objeto: no se emite la URL (nunca se sobrescribe, BR-NUC-65)."""

    def __init__(self) -> None:
        super().__init__("la clave del documento ya tiene un objeto")


class DocumentStorage(Protocol):
    """Lo que usan los documentos de ``StoragePort``: ``head_object`` y ``presign_put``."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = ...,
    ) -> PresignedRequest: ...


class DocumentObjectStore:
    """URL de subida y verificación por metadatos de los documentos firmados."""

    def __init__(
        self,
        storage: DocumentStorage,
        *,
        presign_timeout_seconds: float = PRESIGN_TIMEOUT_SECONDS,
        head_timeout_seconds: float = HEAD_TIMEOUT_SECONDS,
    ) -> None:
        if presign_timeout_seconds <= 0 or head_timeout_seconds <= 0:
            raise ValueError("los tiempos de espera deben ser positivos")
        self._storage = storage
        self._presign_timeout = presign_timeout_seconds
        self._head_timeout = head_timeout_seconds

    async def _bounded[T](self, operation: str, call: Awaitable[T], seconds: float) -> T:
        try:
            async with asyncio.timeout(seconds):
                return await call
        except TimeoutError as error:
            raise StorageUnavailable(operation) from error
        except botocore_exceptions.BotoCoreError as error:
            raise StorageUnavailable(operation) from error

    async def head(self, key: str) -> ObjectHead | None:
        """Metadatos de un objeto (``None`` si no existe), con tope de 10 s."""
        return await self._bounded(
            "head_object", self._storage.head_object(key), self._head_timeout
        )

    async def heads(self, keys: Sequence[str]) -> dict[str, ObjectHead | None]:
        """Metadatos de cada clave, todas a la vez; si una falla, ``StorageUnavailable``."""
        unique = list(dict.fromkeys(keys))
        results = await asyncio.gather(*(self.head(key) for key in unique))
        return dict(zip(unique, results, strict=True))

    async def prepare_upload(
        self, grant: DocumentUploadGrant, now: Callable[[], datetime]
    ) -> PresignedRequest:
        """URL de un solo ``PUT`` con tipo y suma firmados, que vence **no después** que la
        concesión: su vigencia son los segundos enteros que le quedan a ``grant.expires_at``
        cuando se firma (la autorización y la consulta previa ya consumieron algunos)."""
        if await self.head(grant.storage_key) is not None:
            raise DocumentKeyTaken()
        remaining = int((grant.expires_at - now()).total_seconds())
        if remaining < 1:
            # La concesión se agotó esperando al almacén: no queda vigencia que firmar.
            raise StorageUnavailable("presign_put")
        ttl = min(timedelta(seconds=remaining), DOCUMENT_GRANT_TTL)
        return await self._bounded(
            "presign_put",
            self._storage.presign_put(
                grant.storage_key, grant.content_type.value, grant.sha256, {}, ttl
            ),
            self._presign_timeout,
        )
