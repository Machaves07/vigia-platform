"""Confirmación del clip de verificación y listado de clips de comisionamiento (nº 32; LC-GOB-13).

**Confirmación** (``ClipConfirmationService.confirm``, ruta del contrato ``POST
clip-uploads/{clip_id}/confirmation``, sin cuerpo):

1. la concesión de ``clip_id`` tiene que ser **de ese nodo** (``ClipNotOfNode`` →
   ``node_zone_mismatch``; inexistente o de otro nodo responden igual);
2. solo ``purpose = verification``: una concesión ``evidence`` se confirma al presentar su
   registro (``EvidenceClipNotConfirmable`` → ``schema_invalid`` 422 con ``field = purpose``, A-37);
3. si el clip ya está confirmado, el **mismo** recibo, sin consultar el almacén ni crear otro;
4. verificación **por metadatos** (``head_object`` con ``ChecksumMode``, tope de 10 s; nunca se
   descarga, NFR-GOB-09): ``ClipCheckFailed`` con ``clip_missing``, ``clip_too_large``,
   ``clip_hash_mismatch`` o ``clip_not_anonymized``; el almacén caído es ``StorageUnavailable``
   (transitorio, nunca acepta);
5. una transacción: ``SELECT ... FOR UPDATE`` de la concesión (el único candado de la
   operación), lectura del clip por si otra confirmación lo creó mientras tanto y, si no,
   ``VerificationClip`` nuevo y concesión ``issued → used``. Cinco confirmaciones simultáneas del
   mismo clip se serializan en ese candado: crean **un** clip y devuelven el mismo recibo.

Retención igual a la evidencia: ⛓, nada se borra.

**Listado** (``CommissioningClips.page``, ruta de consola ``GET /zones/{zone_id}/commissioning-
clips``; interfaces v1.5, precisión (d)): ``catalog.read`` sobre la zona; inexistente o fuera de
alcance, ``ResourceNotFound`` (``not_found``). Páginas de hasta 200, el más reciente primero;
``after`` es el ``clip_id`` del último de la página anterior. La lectura del proveedor bajo
concesión se audita (``catalog_read``, BR-NUC-38). **``first_served_at``**: en la misma transacción,
el cierre único de los clips de la página que aún no lo tenían (marca final del tramo 3b de
NFR-GOB-70). Lectura del redactor: la forma v1.5 no lleva URL de lectura y BR-NUC-66 exige
``evidence.read`` y auditoría para cualquiera, así que «la consola obtiene el clip» es que esta
ruta lo sirve por primera vez.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Protocol

from vigia_contracts.models.enumerations import ClipUploadPurpose

from vigia_platform.fleet.adapters.postgres.clip_grant_store import PostgresClipGrants
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.domain.clip_upload_grant import ClipUploadGrant, to_millisecond
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.fleet.domain.verification_clip import (
    ClipCheckFailure,
    VerificationClip,
    check_object,
)
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "MAX_PAGE_SIZE",
    "ClipCheckFailed",
    "ClipConfirmationService",
    "ClipNotOfNode",
    "CommissioningClipPage",
    "CommissioningClips",
    "CommissioningClipsRequestInvalid",
    "ConfirmationRepository",
    "EvidenceClipNotConfirmable",
]

MAX_PAGE_SIZE: Final = 200
"""Clips por página del listado de comisionamiento ``[objetivo propio]``."""


class ClipNotOfNode(Exception):
    """No hay concesión de ese ``clip_id`` del nodo (inexistente o de otro nodo)."""

    def __init__(self) -> None:
        super().__init__("la concesión no es del nodo")


class EvidenceClipNotConfirmable(Exception):
    """La concesión es de ``purpose = evidence``: se confirma al presentar su registro."""

    def __init__(self) -> None:
        super().__init__("una concesión de evidencia no se confirma")


class ClipCheckFailed(Exception):
    """El objeto no es el concedido: rechazo permanente con su causa."""

    def __init__(self, failure: ClipCheckFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class CommissioningClipsRequestInvalid(ValueError):
    """Cursor o tamaño de página fuera de los límites: ``invalid_request``."""


class ClipAuthorizer(Protocol):
    """``AuthorizationPort.authorize`` (``identity.authz.authorize.Authorizer``)."""

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext: ...


class ConfirmationRepository(Protocol):
    """Lo que la confirmación usa de ``PostgresClipGrants`` (un doble en memoria en PR-GOB-20)."""

    async def grant(self, context: ScopeContext, clip_id: uuid.UUID) -> ClipUploadGrant | None: ...

    async def read_verification_clip(
        self, context: ScopeContext, clip_id: uuid.UUID
    ) -> VerificationClip | None: ...

    async def lock_for_confirmation(
        self, transaction: Transaction, node_id: uuid.UUID, clip_id: uuid.UUID
    ) -> ClipUploadGrant | None: ...

    async def verification_clip(
        self, transaction: Transaction, clip_id: uuid.UUID
    ) -> VerificationClip | None: ...

    async def insert_verification_clip(
        self, transaction: Transaction, clip: VerificationClip
    ) -> None: ...

    async def mark_used(
        self, transaction: Transaction, node_id: uuid.UUID, clip_id: uuid.UUID, now: datetime
    ) -> bool: ...


class ClipConfirmationService:
    """Confirmación de los clips de verificación (nº 32)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        store: ClipObjectStore,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        grants: ConfirmationRepository | None = None,
    ) -> None:
        self._database = database
        self._grants = grants if grants is not None else PostgresClipGrants(database)
        self._store = store
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()

    async def confirm(self, node: NodeScope, clip_id: uuid.UUID) -> VerificationClip:
        """El ``VerificationClip`` del clip (nuevo o el ya confirmado)."""
        context = node.context
        grant = await self._grants.grant(context, clip_id)
        if grant is None or grant.node_id != node.node_id:
            raise ClipNotOfNode()
        if grant.purpose is not ClipUploadPurpose.VERIFICATION:
            raise EvidenceClipNotConfirmable()
        confirmed = await self._grants.read_verification_clip(context, clip_id)
        if confirmed is not None:
            return confirmed
        failure = check_object(grant, await self._store.head(grant.storage_key))
        if failure is not None:
            raise ClipCheckFailed(failure)
        received_at = to_millisecond(self._clock.now())
        async with self._database.transaction(context) as transaction:
            locked = await self._grants.lock_for_confirmation(transaction, node.node_id, clip_id)
            if locked is None:
                raise ClipNotOfNode()
            existing = await self._grants.verification_clip(transaction, clip_id)
            if existing is not None:
                return existing
            if locked.status is not UploadGrantStatus.ISSUED:
                raise RuntimeError("concesión de verificación cerrada sin clip confirmado")
            clip = VerificationClip.confirmed(locked, received_at)
            await self._grants.insert_verification_clip(transaction, clip)
            if not await self._grants.mark_used(transaction, node.node_id, clip_id, received_at):
                raise RuntimeError("la concesión dejó de estar emitida durante la confirmación")
        self._metrics.clip_grants_used_total.add(1, {"node_id": str(node.node_id)})
        return clip


@dataclass(frozen=True, slots=True)
class CommissioningClipPage:
    """Una página del listado; ``next_after`` es el cursor de la siguiente (o ``None``)."""

    clips: tuple[VerificationClip, ...]
    next_after: uuid.UUID | None


class CommissioningClips:
    """``GET /zones/{zone_id}/commissioning-clips`` (selector de U-05 para el pase)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: ClipAuthorizer,
        audit: AuditWriter,
        clock: Clock,
    ) -> None:
        self._database = database
        self._grants = PostgresClipGrants(database)
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock

    async def page(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        *,
        after: uuid.UUID | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> CommissioningClipPage:
        """Los clips de verificación de la zona; marca ``first_served_at`` de los servidos."""
        if not isinstance(context, ScopeContext) or type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise CommissioningClipsRequestInvalid("limit fuera de 1 a 200")
        zone = await self._grants.zone(context, zone_id)
        if zone is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.CATALOG_READ,
            Resource.zone(context.organization_id, zone.plant_id, zone.zone_id),
        )
        now = to_millisecond(self._clock.now())
        async with self._database.transaction(authorized) as transaction:
            # Uno de más: dice si hay página siguiente sin otra consulta.
            found = await self._grants.zone_clips(
                transaction, zone.zone_id, after=after, limit=limit + 1
            )
            if found is None:
                raise CommissioningClipsRequestInvalid("el cursor no es un clip de la zona")
            items = found[:limit]
            served = await self._grants.mark_first_served(
                transaction,
                zone.zone_id,
                [clip.clip_id for clip in items if clip.first_served_at is None],
                now,
            )
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada en la misma transacción.
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=len(items),
                    transaction=transaction,
                )
        clips = tuple(
            replace(clip, first_served_at=now) if clip.clip_id in served else clip for clip in items
        )
        following = items[-1].clip_id if len(found) > limit else None
        return CommissioningClipPage(clips=clips, next_after=following)
