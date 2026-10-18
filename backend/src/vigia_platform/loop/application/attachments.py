"""Caso de uso de adjuntos de cierre; los adaptadores conservan los efectos externos."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from vigia_platform.loop.adapters.attachments.grants import (
    AttachmentUploadGrant,
    ClosureObjectStore,
)
from vigia_platform.loop.adapters.attachments.read import ClosureAttachmentReader
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.storage import PresignedRequest

__all__ = ["AttachmentRepository", "ClosureAttachmentService", "IssuedAttachmentGrant"]


class AttachmentRepository(Protocol):
    """La proyección de acciones y adjuntos, siempre consultada bajo ScopeContext."""

    async def action_scope(
        self, context: ScopeContext, action_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None: ...

    async def attachment_key(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class IssuedAttachmentGrant:
    grant: AttachmentUploadGrant
    upload: PresignedRequest


@dataclass(slots=True)
class ClosureAttachmentService:
    """Coordina claves ligadas a acción; no descarga ni escribe objetos."""

    repository: AttachmentRepository
    grants: ClosureObjectStore
    reader: ClosureAttachmentReader
    clock: Clock

    async def grant_upload(
        self,
        context: ScopeContext,
        action_id: uuid.UUID,
        *,
        content_type: object,
        size_bytes: object,
        sha256: object,
        random_bytes: Callable[[int], bytes],
    ) -> IssuedAttachmentGrant:
        scope = await self.repository.action_scope(context, action_id)
        if scope is None:
            raise LookupError("acción no encontrada")
        organization_id, plant_id, zone_id = scope
        grant = AttachmentUploadGrant.issue(
            organization_id=organization_id,
            plant_id=plant_id,
            zone_id=zone_id,
            subject_id=action_id,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            issued_at=self.clock.now(),
            clock=self.clock,
            random_bytes=random_bytes,
        )
        upload = await self.grants.grant(grant, self.clock.now())
        return IssuedAttachmentGrant(grant, upload)

    async def read_url(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> str:
        key = await self.repository.attachment_key(context, attachment_id)
        if key is None:
            raise LookupError("adjunto no encontrado")
        now = self.clock.now()
        return (await self.reader.read_url(key, now, now + timedelta(minutes=15))).url
