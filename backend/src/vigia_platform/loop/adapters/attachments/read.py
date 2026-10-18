"""Lectura prefirmada de adjuntos de cierre."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vigia_platform.shared.storage import (
    PRESIGN_ATTACHMENT_GET_MAX_TTL,
    PresignedRequest,
    StoragePort,
)

__all__ = ["ClosureAttachmentReader"]


@dataclass(frozen=True, slots=True)
class ClosureAttachmentReader:
    """Firma solo la clave ya autorizada; no enumera ni resuelve prefijos."""

    storage: StoragePort = field(repr=False)

    async def read_url(
        self, storage_key: str, now: datetime, expires_at: datetime
    ) -> PresignedRequest:
        """Devuelve una URL de descarga que no vive más de quince minutos ni de la autorización."""
        seconds = int((expires_at - now).total_seconds())
        if seconds < 1:
            raise ValueError("la lectura ya venció")
        return await self.storage.presign_attachment_get(
            storage_key,
            min(timedelta(seconds=seconds), PRESIGN_ATTACHMENT_GET_MAX_TTL),
        )
