"""Concesiones de subida de adjuntos de cierre, sin escribir objetos."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vigia_platform.loop.domain.enums import AttachmentContentType
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.storage import PresignedRequest, StoragePort

__all__ = ["ATTACHMENT_GRANT_TTL", "AttachmentUploadGrant", "ClosureObjectStore"]

ATTACHMENT_GRANT_TTL = timedelta(minutes=15)
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_EXTENSIONS = {
    AttachmentContentType.IMAGE_JPEG: "jpg",
    AttachmentContentType.IMAGE_PNG: "png",
    AttachmentContentType.APPLICATION_PDF: "pdf",
}


@dataclass(frozen=True, slots=True)
class AttachmentUploadGrant:
    """Datos inmutables de una URL de un único PUT, nunca registrados con su URL."""

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
        issued_at: datetime,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> AttachmentUploadGrant:
        if not isinstance(content_type, str):
            raise ValueError("content_type no permitido")
        try:
            parsed_type = AttachmentContentType(content_type)
        except ValueError:
            raise ValueError("content_type no permitido") from None
        if type(size_bytes) is not int or not 1 <= size_bytes <= MAX_ATTACHMENT_BYTES:
            raise ValueError("size_bytes fuera de rango")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise ValueError("sha256 no válido")
        attachment_id = uuid7(clock, random_bytes)
        key = (
            f"org/{organization_id}/plant/{plant_id}/zone/{zone_id}/closure/"
            f"{subject_id}/{attachment_id}.{_EXTENSIONS[parsed_type]}"
        )
        return cls(
            attachment_id,
            key,
            parsed_type,
            size_bytes,
            sha256,
            issued_at + ATTACHMENT_GRANT_TTL,
        )


@dataclass(frozen=True, slots=True)
class ClosureObjectStore:
    """Firma el PUT tras comprobar que una clave UUID nueva no existe."""

    storage: StoragePort = field(repr=False)

    async def grant(self, grant: AttachmentUploadGrant, now: datetime) -> PresignedRequest:
        if await self.storage.head_object(grant.storage_key) is not None:
            raise ValueError("la clave de adjunto ya existe")
        seconds = int((grant.expires_at - now).total_seconds())
        if seconds < 1:
            raise ValueError("la concesión ya venció")
        return await self.storage.presign_put(
            grant.storage_key,
            grant.content_type.value,
            grant.sha256,
            {},
            min(timedelta(seconds=seconds), ATTACHMENT_GRANT_TTL),
        )
