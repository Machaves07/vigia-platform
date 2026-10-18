"""Concesión de subida de un adjunto de cierre (SUP-U05-09; PAT-LAZ-SEG-07; PR-LAZ-63).

La plataforma **solo firma**: una URL de un único ``PUT`` (nunca por partes), vigente 15
minutos desde el ``Clock`` inyectado, con el tipo de contenido y ``x-amz-checksum-sha256``
**entre las cabeceras firmadas**, sobre la clave de ``rules.attachment_storage_key``. Un ``PUT``
sin la suma, con otra suma o con otros bytes lo rechaza el almacén.

Antes de firmar se consulta la clave (``HEAD``): con un identificador UUID v7 nuevo nunca hay
objeto, y la consulta hace que un almacén caído se **declare** en la concesión
(``storage_unavailable``) en vez de entregar una URL que no se podría usar (FS-LAZ-03). Ambas
llamadas pasan por el cortacircuito.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.loop.adapters.attachments.rules import (
    MAX_ATTACHMENT_BYTES,
    AttachmentRejected,
    AttachmentSchemaInvalid,
    attachment_storage_key,
)
from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage, StoreCircuit
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.storage import PresignedRequest

__all__ = ["ATTACHMENT_GRANT_TTL", "AttachmentUploadGrant", "ClosureUploadSigner"]

ATTACHMENT_GRANT_TTL: Final = timedelta(minutes=15)
"""Vigencia de la URL de subida (LC-LAZ-10)."""

_SHA256_CHARS: Final = frozenset("0123456789abcdef")
_MAX_CONTENT_TYPE_CHARS: Final = 255


@dataclass(frozen=True, slots=True)
class AttachmentUploadGrant:
    """Lo que se concede; la URL va aparte (``PresignedRequest``) y nunca se registra."""

    attachment_id: uuid.UUID
    storage_key: str
    content_type: AttachmentContentType
    size_bytes: int
    sha256: str
    expires_at: datetime

    @classmethod
    def issue(
        cls,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        subject_id: uuid.UUID,
        content_type: object,
        size_bytes: object,
        sha256: object,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> AttachmentUploadGrant:
        """Valida en el orden de SUP-U05-09 (esquema, tipo, tamaño) y fija la clave y la vigencia.

        Raises:
            AttachmentSchemaInvalid: forma ajena al esquema (tipos JSON, suma no hexadecimal).
            AttachmentRejected: tipo fuera de la lista o más de 10 MB (``detail_code``).
        """
        if (
            not isinstance(content_type, str)
            or not 1 <= len(content_type) <= _MAX_CONTENT_TYPE_CHARS
            or type(size_bytes) is not int
            or size_bytes < 1
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or not set(sha256) <= _SHA256_CHARS
        ):
            raise AttachmentSchemaInvalid("concesión de adjunto con forma inválida")
        try:
            parsed = AttachmentContentType(content_type)
        except ValueError:
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED) from None
        if size_bytes > MAX_ATTACHMENT_BYTES:
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TOO_LARGE)
        attachment_id = uuid7(clock, random_bytes)
        key = attachment_storage_key(
            organization_id=organization_id,
            plant_id=plant_id,
            zone_id=zone_id,
            subject_id=subject_id,
            attachment_id=attachment_id,
            content_type=parsed,
        )
        return cls(
            attachment_id, key, parsed, size_bytes, sha256, clock.now() + ATTACHMENT_GRANT_TTL
        )


@dataclass(frozen=True, slots=True)
class ClosureUploadSigner:
    """Firma el ``PUT`` de una concesión; no escribe objetos por sí mismo (PAT-LAZ-SEG-07)."""

    storage: ClosureObjectStorage = field(repr=False)
    circuit: StoreCircuit
    clock: Clock

    async def sign(self, grant: AttachmentUploadGrant) -> PresignedRequest:
        """La URL de un solo ``PUT`` que vence en ``grant.expires_at`` (nunca después)."""
        remaining = int((grant.expires_at - self.clock.now()).total_seconds())
        if remaining < 1:
            raise AttachmentSchemaInvalid("la concesión ya venció")
        ttl = min(timedelta(seconds=remaining), ATTACHMENT_GRANT_TTL)

        async def head() -> object:
            return await self.storage.head_object(grant.storage_key)

        if await self.circuit.call(head) is not None:
            raise AttachmentSchemaInvalid("la clave del adjunto ya existe")

        async def presign() -> PresignedRequest:
            return await self.storage.presign_put(
                grant.storage_key, grant.content_type.value, grant.sha256, {}, ttl
            )

        return await self.circuit.call(presign)
