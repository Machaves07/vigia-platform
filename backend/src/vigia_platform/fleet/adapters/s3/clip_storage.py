"""Los clips del nodo en ``vigia-evidence``: URL de subida y metadatos (LC-GOB-13).

Sobre el ``StoragePort`` heredado (LC-NUC-30), ligado al depósito de evidencias. Nunca lista
objetos ni firma URL de lectura (BR-GOB §11): este adaptador no expone ``get_object``,
``presign_get`` ni listado.

- ``signing_deadline()``: el tope de 5 s de la concesión (NFR-GOB-43, firma de URL) sobre **todo**
  lo que la concesión pide al almacén (la consulta de la clave y la firma): vencido, o con un fallo
  de ``botocore`` al firmar, ``StorageUnavailable`` y nada escrito (FS-GOB-01 en comportamiento);
- ``head(key)``: metadatos con ``ChecksumMode=ENABLED`` (``ObjectFacts``), tope de 10 s; nunca se
  descarga el clip (NFR-GOB-09);
- ``heads(keys)``: lo mismo **en paralelo** con a lo sumo ``max_parallel_heads`` consultas a la
  vez (PAT-GOB-REN-05); si una falla, ``StorageUnavailable`` (fallo cerrado, nunca parcial);
- ``sign(grant, now)``: URL de un solo ``PUT`` con el tipo de contenido, la suma SHA-256 y el
  metadato ``vigia-anonymized: 1`` **firmados** (PAT-GOB-SEG-03), que vence **no después** que la
  concesión: su vigencia son los segundos enteros que le quedan a ``grant.expires_at``. Las
  cabeceras que devuelve el puerto tienen que ser exactamente ``grant.required_headers``.

**Un solo ``PUT``.** La URL lleva firmados la suma y el tipo: un ``PUT`` con otros bytes da
``BadDigest`` y uno sin la suma o con otro tipo no pasa la firma; después de vencer, ninguno. La
condición ``If-None-Match: *`` que rechazaría también un segundo ``PUT`` de los **mismos** bytes
dentro de la vigencia no se firma: el nodo envía exactamente ``required_headers`` (A-05) y el
contrato fijado no admite esa cabecera en el mapa (``RequiredHeaders`` cerrado), así que una URL
que la exigiera no la podría usar ningún nodo. Queda declarado en el PR de TASK-222.

**Límite conocido (heredado de U-02).** ``S3Storage.presign_put`` firma de forma síncrona dentro del
bucle de eventos: el tope corta un almacén asíncrono colgado, no un bloqueo de ``botocore`` al
refrescar las credenciales del rol (igual que ``catalog.adapters.s3.documents``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final, Protocol

from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]

from vigia_platform.fleet.domain.clip_upload_grant import CLIP_GRANT_TTL, ClipUploadGrant
from vigia_platform.fleet.domain.verification_clip import ObjectFacts
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, StorageUnavailable

__all__ = [
    "HEAD_TIMEOUT_SECONDS",
    "MAX_PARALLEL_HEADS",
    "PRESIGN_TIMEOUT_SECONDS",
    "ClipObjectStore",
    "ClipStorage",
]

PRESIGN_TIMEOUT_SECONDS: Final = 5.0
"""Firma de la URL prefirmada (NFR-GOB-43): tope de todo lo que la concesión pide al almacén."""
HEAD_TIMEOUT_SECONDS: Final = 10.0
"""Consulta de metadatos de un objeto (NFR-GOB-43)."""
MAX_PARALLEL_HEADS: Final = 16
"""Consultas de metadatos simultáneas de ``heads`` ``[objetivo propio]``."""


class ClipStorage(Protocol):
    """Lo que usan los clips de ``StoragePort``: ``head_object`` y ``presign_put``."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = ...,
    ) -> PresignedRequest: ...


def _facts(head: ObjectHead | None) -> ObjectFacts | None:
    if head is None:
        return None
    return ObjectFacts(
        size_bytes=head.size_bytes,
        sha256_hex=head.full_object_sha256_hex,
        content_type=head.content_type,
        metadata=dict(head.metadata),
    )


class ClipObjectStore:
    """URL de subida y verificación por metadatos de los clips del nodo."""

    def __init__(
        self,
        storage: ClipStorage,
        *,
        presign_timeout_seconds: float = PRESIGN_TIMEOUT_SECONDS,
        head_timeout_seconds: float = HEAD_TIMEOUT_SECONDS,
        max_parallel_heads: int = MAX_PARALLEL_HEADS,
    ) -> None:
        if presign_timeout_seconds <= 0 or head_timeout_seconds <= 0:
            raise ValueError("los tiempos de espera deben ser positivos")
        if type(max_parallel_heads) is not int or max_parallel_heads < 1:
            raise ValueError("max_parallel_heads debe ser al menos 1")
        self._storage = storage
        self._presign_timeout = presign_timeout_seconds
        self._head_timeout = head_timeout_seconds
        self._max_parallel_heads = max_parallel_heads

    @contextlib.asynccontextmanager
    async def signing_deadline(self) -> AsyncIterator[None]:
        """Tope de 5 s de la concesión; vencido, ``StorageUnavailable`` (nada escrito)."""
        try:
            async with asyncio.timeout(self._presign_timeout):
                yield
        except TimeoutError as error:
            raise StorageUnavailable("presign_put") from error

    async def _bounded[T](self, operation: str, call: Awaitable[T], seconds: float) -> T:
        try:
            async with asyncio.timeout(seconds):
                return await call
        except TimeoutError as error:
            raise StorageUnavailable(operation) from error
        except botocore_exceptions.BotoCoreError as error:
            raise StorageUnavailable(operation) from error

    async def head(self, key: str) -> ObjectFacts | None:
        """Metadatos del objeto (``None`` si no existe), con tope de 10 s."""
        head = await self._bounded(
            "head_object", self._storage.head_object(key), self._head_timeout
        )
        return _facts(head)

    async def heads(self, keys: Sequence[str]) -> dict[str, ObjectFacts | None]:
        """Metadatos de cada clave, en paralelo con tope; si una falla, ``StorageUnavailable``."""
        unique = list(dict.fromkeys(keys))
        gate = asyncio.Semaphore(self._max_parallel_heads)

        async def one(key: str) -> ObjectFacts | None:
            async with gate:
                return await self.head(key)

        results = await asyncio.gather(*(one(key) for key in unique))
        return dict(zip(unique, results, strict=True))

    async def sign(self, grant: ClipUploadGrant, now: Callable[[], datetime]) -> PresignedRequest:
        """URL de un solo ``PUT`` que vence no después que ``grant`` (tope de 5 s).

        Sin vigencia que firmar (menos de 1 s), ``StorageUnavailable``: transitorio, el nodo
        reintenta y la concesión ya vencida se reemite.
        """
        remaining = int((grant.expires_at - now()).total_seconds())
        if remaining < 1:
            raise StorageUnavailable("presign_put")
        ttl = min(timedelta(seconds=remaining), CLIP_GRANT_TTL)
        upload = await self._bounded(
            "presign_put",
            self._storage.presign_put(
                grant.storage_key,
                grant.content_type.value,
                grant.sha256,
                grant.metadata_headers,
                ttl,
            ),
            self._presign_timeout,
        )
        if upload.method != "PUT" or dict(upload.headers) != grant.required_headers:
            # Fallo cerrado: el nodo envía exactamente required_headers (A-05).
            raise RuntimeError("la URL firmada no exige exactamente las cabeceras concedidas")
        return upload
