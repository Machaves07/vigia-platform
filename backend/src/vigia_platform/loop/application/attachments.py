"""``ClosureAttachmentPort`` (LC-LAZ-10; business-logic-model §9): ``grant_upload``, ``verify``
y ``read_url`` de los adjuntos de evidencia de cierre.

- ``grant_upload`` (SUP-U05-09, ``actions.manage`` sobre la zona de la acción): límite de 60 por
  hora y persona (cubo de fichas aproximado por instancia, heredado de U-02), acción visible y
  autorizada **por su zona**, tipo y tamaño, y la URL de un solo ``PUT`` (``grants``).
- ``verify``: el orden fijo de PAT-LAZ-SEG-05 (``verify``); no escribe nada.
- ``read_url`` (SUP-U05-10, ``closure_attachments.read`` sobre la zona del adjunto): la URL de la
  versión verificada (``read``) y **una entrada de auditoría por emisión**
  (``closure_attachment_read_granted``), que declara en ``filters`` que registra la concesión de
  acceso y no la descarga efectiva, con su vigencia (PAT-LAZ-SEG-04, PR-LAZ-60). Si la entrada no
  se escribe, la URL no sale de aquí.

Un recurso de otra organización, inexistente o fuera del alcance de la persona responde igual
(``ResourceNotFound`` → ``not_found``). La hora sale siempre del ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    ResourceRef,
)
from vigia_platform.loop.adapters.attachments.grants import (
    AttachmentUploadGrant,
    ClosureUploadSigner,
)
from vigia_platform.loop.adapters.attachments.read import (
    AttachmentUnreadable,
    ClosureAttachmentReader,
)
from vigia_platform.loop.adapters.attachments.repository import ActionPlace, StoredAttachment
from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage, StoreCircuit
from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentSubject,
    ClosureAttachmentVerifier,
    VerifiedAttachment,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.ratelimit import (
    CLOSURE_ATTACHMENT_BUDGET,
    Budget,
    Limited,
    RateLimiter,
    closure_attachment_key,
)
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import PresignedRequest

__all__ = [
    "ACCESS_GRANT_DECLARATION",
    "ACTION_RESOURCE_KIND",
    "ATTACHMENT_RESOURCE_KIND",
    "AttachmentRateLimited",
    "AttachmentReadGrant",
    "AttachmentRepository",
    "ClosureAttachmentService",
    "IssuedAttachmentGrant",
    "build_attachment_service",
]

ACTION_RESOURCE_KIND: Final = "corrective_action"
ATTACHMENT_RESOURCE_KIND: Final = "closure_attachment"
ACCESS_GRANT_DECLARATION: Final = "access_grant_not_download"
"""Valor de ``filters.records`` de la auditoría de lectura: la concesión, no la descarga."""


class AttachmentRateLimited(Exception):
    """Más de 60 concesiones en la hora para esta persona (``rate_limited``)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("límite de concesiones de adjuntos")
        self.retry_after_seconds = retry_after_seconds


class AttachmentRepository(Protocol):
    async def action_place(
        self, context: ScopeContext, action_id: uuid.UUID
    ) -> ActionPlace | None: ...

    async def stored_attachment(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> StoredAttachment | None: ...


class ZoneAuthorizer(Protocol):
    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext: ...


class AuditPort(Protocol):
    async def append(
        self,
        context: ScopeContext,
        operation: AuditOperation | str,
        *,
        outcome: AuditOutcome | str = ...,
        plant_id: uuid.UUID | None = ...,
        zone_id: uuid.UUID | None = ...,
        resource: ResourceRef | None = ...,
        filters: dict[str, str] | None = ...,
        result_count: int | None = ...,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class IssuedAttachmentGrant:
    grant: AttachmentUploadGrant
    upload: PresignedRequest = field(repr=False)


@dataclass(frozen=True, slots=True)
class AttachmentReadGrant:
    """La lectura concedida; no se persiste. La URL es un secreto de corta vida."""

    attachment_id: uuid.UUID
    url: str = field(repr=False)
    expires_at: datetime


@repository
@dataclass(slots=True)
class ClosureAttachmentService:
    """Coordina el puerto; los efectos externos están en los adaptadores."""

    repository: AttachmentRepository
    signer: ClosureUploadSigner
    verifier: ClosureAttachmentVerifier
    reader: ClosureAttachmentReader
    authorizer: ZoneAuthorizer
    audit: AuditPort
    limiter: RateLimiter
    clock: Clock
    budget: Budget = CLOSURE_ATTACHMENT_BUDGET
    random_bytes: Callable[[int], bytes] = os.urandom

    async def grant_upload(
        self,
        context: ScopeContext,
        action_id: uuid.UUID,
        *,
        content_type: object,
        size_bytes: object,
        sha256: object,
    ) -> IssuedAttachmentGrant:
        """La concesión de un solo ``PUT`` sobre ``closure/`` de la zona de la acción."""
        limit = self.limiter.check(closure_attachment_key(context.actor.id), self.budget)
        if isinstance(limit, Limited):
            raise AttachmentRateLimited(limit.retry_after_seconds)
        place = await self.repository.action_place(context, action_id)
        if place is None:
            raise ResourceNotFound()
        await self.authorizer.authorize(
            context,
            PermissionKey.ACTIONS_MANAGE,
            Resource(
                place.organization_id,
                ACTION_RESOURCE_KIND,
                action_id,
                plant_id=place.plant_id,
                zone_id=place.zone_id,
            ),
        )
        grant = AttachmentUploadGrant.issue(
            organization_id=place.organization_id,
            plant_id=place.plant_id,
            zone_id=place.zone_id,
            subject_id=place.subject_id,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            clock=self.clock,
            random_bytes=self.random_bytes,
        )
        return IssuedAttachmentGrant(grant, await self.signer.sign(grant))

    async def verify(
        self, subject: AttachmentSubject, claims: object, *, already_accepted: object = 0
    ) -> tuple[VerifiedAttachment, ...]:
        """El orden fijo de PAT-LAZ-SEG-05 antes de escribir el registro que los referencia."""
        return await self.verifier.verify(subject, claims, already_accepted=already_accepted)

    async def read_url(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> AttachmentReadGrant:
        """URL de descarga ≤ 15 min de la versión verificada, con una auditoría por emisión."""
        resource = ResourceRef(ATTACHMENT_RESOURCE_KIND, attachment_id)
        stored = await self.repository.stored_attachment(context, attachment_id)
        if stored is None:
            await self.audit.append(
                context,
                AuditOperation.CLOSURE_ATTACHMENT_READ_GRANTED,
                outcome=AuditOutcome.DENIED,
                resource=resource,
                result_count=0,
            )
            raise ResourceNotFound()
        authorized = await self.authorizer.authorize(
            context,
            PermissionKey.CLOSURE_ATTACHMENTS_READ,
            Resource(
                stored.organization_id,
                ATTACHMENT_RESOURCE_KIND,
                attachment_id,
                plant_id=stored.plant_id,
                zone_id=stored.zone_id,
            ),
        )
        try:
            presigned = await self.reader.read_url(
                stored.storage_key, sha256=stored.sha256, size_bytes=stored.size_bytes
            )
        except AttachmentUnreadable:
            await self.audit.append(
                authorized,
                AuditOperation.CLOSURE_ATTACHMENT_READ_GRANTED,
                outcome=AuditOutcome.ERROR,
                plant_id=stored.plant_id,
                zone_id=stored.zone_id,
                resource=resource,
                result_count=0,
            )
            raise
        await self.audit.append(
            authorized,
            AuditOperation.CLOSURE_ATTACHMENT_READ_GRANTED,
            plant_id=stored.plant_id,
            zone_id=stored.zone_id,
            resource=resource,
            filters={
                "records": ACCESS_GRANT_DECLARATION,
                "expires_at": format_timestamp(presigned.expires_at),
            },
            result_count=1,
        )
        return AttachmentReadGrant(attachment_id, presigned.url, presigned.expires_at)


def build_attachment_service(
    *,
    repository: AttachmentRepository,
    storage: ClosureObjectStorage,
    authorizer: ZoneAuthorizer,
    audit: AuditPort,
    clock: Clock,
    budget: Budget = CLOSURE_ATTACHMENT_BUDGET,
) -> ClosureAttachmentService:
    """El puerto con **un** cortacircuito compartido por concesión, verificación y lectura."""
    circuit = StoreCircuit(clock)
    return ClosureAttachmentService(
        repository=repository,
        signer=ClosureUploadSigner(storage, circuit, clock),
        verifier=ClosureAttachmentVerifier(storage, circuit),
        reader=ClosureAttachmentReader(storage, circuit),
        authorizer=authorizer,
        audit=audit,
        limiter=RateLimiter(clock),
        clock=clock,
        budget=budget,
    )
