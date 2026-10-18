"""``ClosureAttachmentPort.read_url``: la URL de lectura de un adjunto ya aceptado (SUP-U05-10).

Mismo patrón que la lectura de un clip (``ledger.application.evidence_read``, BR-NUC-66):

- se consulta el objeto (``HEAD``) y la URL se fija a la versión vigente **solo si** su suma
  SHA-256 de objeto entero y su tamaño son los del registro; si falta, si no coincide o si el
  almacén no da ``version_id``, ``AttachmentUnreadable`` y no hay URL: nunca sirve otros bytes;
- la URL vale como mucho 15 minutos del ``Clock`` inyectado y se sirve como **descarga**
  (``Content-Disposition: attachment``, tipo opaco), nunca para mostrarse en el navegador;
- se firma **una clave concreta**: no hay listado de objetos ni URL permanente sobre
  ``closure/`` (BR-LAZ-45).

Ambas llamadas al almacén pasan por el cortacircuito.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage, StoreCircuit
from vigia_platform.shared.storage import (
    PRESIGN_ATTACHMENT_GET_MAX_TTL,
    ObjectHead,
    PresignedRequest,
)

__all__ = ["READ_URL_TTL", "AttachmentUnreadable", "ClosureAttachmentReader"]

READ_URL_TTL: Final = PRESIGN_ATTACHMENT_GET_MAX_TTL
"""Vigencia de la URL de lectura de un adjunto: 15 minutos (SUP-U05-10)."""


class AttachmentUnreadable(Exception):
    """El objeto falta o sus bytes no son los del registro: no se concede la lectura."""


@dataclass(frozen=True, slots=True)
class ClosureAttachmentReader:
    """Firma la lectura de la versión verificada de una clave ya autorizada."""

    storage: ClosureObjectStorage = field(repr=False)
    circuit: StoreCircuit

    async def read_url(self, storage_key: str, *, sha256: str, size_bytes: int) -> PresignedRequest:
        async def head() -> ObjectHead | None:
            return await self.storage.head_object(storage_key)

        found = await self.circuit.call(head)
        if (
            found is None
            or found.version_id is None
            or found.full_object_sha256_hex != sha256
            or found.size_bytes != size_bytes
        ):
            raise AttachmentUnreadable()
        version = found.version_id

        async def presign() -> PresignedRequest:
            return await self.storage.presign_attachment_get(
                storage_key, READ_URL_TTL, version_id=version
            )

        return await self.circuit.call(presign)
