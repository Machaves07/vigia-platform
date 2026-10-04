"""``fleet.grants``: concesión de subida de un clip del nodo (LC-GOB-13; BR-GOB-93; PR-GOB-20).

``issue(node, request)`` atiende ``POST clip-uploads`` con la ``ClipUploadRequest`` que ya pasó el
lector estricto de U-01 y el contexto del nodo (``NodeScope``):

1. la zona del cuerpo tiene que estar entre las asignadas al nodo **en el instante de la
   petición** (``ZoneNotAssigned`` → ``node_zone_mismatch``; una zona de otra organización
   nunca lo está);
2. la concesión pedida (``ClipUploadGrant.issue``: clave ligada a organización, planta, zona y
   nodo, 15 minutos, ``purpose`` con ``evidence`` por defecto);
3. si el ``clip_id`` no tiene concesión: dentro del tope de 5 s del almacén
   (``ClipObjectStore.signing_deadline``), la clave no tiene objeto y se firma la URL de un solo
   ``PUT``; **después**, fuera de la consulta al almacén (PAT-NUC-RES-08), una transacción con la
   concesión en ``issued``. Con el almacén caído o lento, ``StorageUnavailable`` y nada escrito
   (FS-GOB-01, NFR-GOB-43): la URL nunca sale si la fila no se escribió;
4. si ya la tiene, la **repetición** (PR-GOB-20, ``repeat_outcome``): misma petición, vigente y
   sin objeto → URL nueva con el tiempo que le queda; vencida y sin objeto → reemisión de la misma
   fila (``gob_0021``); otra petición, objeto ya subido o concesión cerrada → ``ClipGrantConflict``.

``ClipGrantConflict`` es ``schema_invalid`` (422) con ``field = clip_id`` hacia el nodo: la
operación ``post_clip_upload`` de ``ingest.yaml`` no declara ``409``, y A-37 limita cada operación a
los códigos que declara (la decisión del redactor pedía ``idempotency_conflict``; declarado en el
PR de TASK-222).

El ``issued_total`` de NFR-GOB-55 cuenta cada concesión escrita (alta o reemisión), por nodo.
La URL prefirmada es un secreto de corta vida: ``IssuedClipGrant`` no la muestra en ``repr`` y
nada de este módulo la registra (NFR-GOB-25).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from vigia_contracts.models.clip_upload_request import ClipUploadRequest

from vigia_platform.fleet.adapters.postgres.clip_grant_store import PostgresClipGrants
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.domain.clip_upload_grant import (
    ClipUploadGrant,
    RepeatOutcome,
    repeat_outcome,
)
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.storage import PresignedRequest

__all__ = [
    "ClipGrantConflict",
    "ClipGrantRepository",
    "ClipGrantService",
    "IssuedClipGrant",
    "ZoneNotAssigned",
]


class ClipGrantRepository(Protocol):
    """Lo que la concesión usa de ``PostgresClipGrants`` (un doble en memoria en PR-GOB-20)."""

    async def grant(self, context: ScopeContext, clip_id: uuid.UUID) -> ClipUploadGrant | None: ...

    async def insert(self, transaction: Transaction, grant: ClipUploadGrant) -> bool: ...

    async def reissue(
        self, transaction: Transaction, grant: ClipUploadGrant, previous_issued_at: datetime
    ) -> bool: ...


class ClipGrantConflict(Exception):
    """El ``clip_id`` ya tiene otra concesión, su objeto ya está subido o está cerrada."""

    def __init__(self) -> None:
        super().__init__("el clip ya tiene una concesión que no admite otra")


class ZoneNotAssigned(Exception):
    """La zona pedida no está asignada al nodo en el instante de la petición."""

    def __init__(self) -> None:
        super().__init__("la zona no está asignada al nodo")


@dataclass(frozen=True, slots=True)
class IssuedClipGrant:
    """La concesión escrita y su URL de subida (secreto de corta vida: no se registra)."""

    grant: ClipUploadGrant
    upload: PresignedRequest = field(repr=False)


class ClipGrantService:
    """Concesiones de subida de clips sobre PostgreSQL y ``vigia-evidence``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        store: ClipObjectStore,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        grants: ClipGrantRepository | None = None,
    ) -> None:
        self._database = database
        self._grants = grants if grants is not None else PostgresClipGrants(database)
        self._store = store
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()

    async def issue(self, node: NodeScope, request: ClipUploadRequest) -> IssuedClipGrant:
        """``POST clip-uploads``: concesión ``issued`` y URL de un solo ``PUT``."""
        zone_id = uuid.UUID(str(request.zone_id))
        if not node.covers_zone(zone_id):
            raise ZoneNotAssigned()
        requested = ClipUploadGrant.issue(
            clip_id=uuid.UUID(str(request.clip_id)),
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            zone_id=zone_id,
            node_id=node.node_id,
            purpose=request.purpose,
            media_kind=request.media_kind,
            content_type=request.content_type.value,
            sha256=request.sha256,
            size_bytes=request.size_bytes,
            issued_at=self._clock.now(),
        )
        context = node.context
        existing = await self._grants.grant(context, requested.clip_id)
        if existing is None:
            async with self._store.signing_deadline():
                if await self._store.head(requested.storage_key) is not None:
                    # Nunca se firma una URL sobre una clave con objeto (BR-NUC-65).
                    raise ClipGrantConflict()
                upload = await self._store.sign(requested, self._clock.now)
            async with self._database.transaction(context) as transaction:
                inserted = await self._grants.insert(transaction, requested)
            if inserted:
                self._count(requested)
                return IssuedClipGrant(requested, upload)
            # Otra petición del mismo clip escribió primero: se responde como repetición.
            existing = await self._grants.grant(context, requested.clip_id)
            if existing is None:
                raise ClipGrantConflict()
        return await self._repeat(node, existing, requested)

    async def _repeat(
        self, node: NodeScope, existing: ClipUploadGrant, requested: ClipUploadGrant
    ) -> IssuedClipGrant:
        if not existing.same_request(requested) or existing.status is not UploadGrantStatus.ISSUED:
            raise ClipGrantConflict()
        async with self._store.signing_deadline():
            uploaded = await self._store.head(existing.storage_key) is not None
            now = self._clock.now()
            outcome = repeat_outcome(existing, requested, now, uploaded=uploaded)
            if outcome is RepeatOutcome.CONFLICT:
                raise ClipGrantConflict()
            if outcome is RepeatOutcome.RENEW_URL:
                return IssuedClipGrant(existing, await self._store.sign(existing, self._clock.now))
            grant = existing.reissued(now)
            upload = await self._store.sign(grant, self._clock.now)
        async with self._database.transaction(node.context) as transaction:
            reissued = await self._grants.reissue(transaction, grant, existing.issued_at)
        if not reissued:
            # Otra reemisión simultánea ganó: su concesión vigente es la que vale.
            current = await self._grants.grant(node.context, grant.clip_id)
            if current is None or current.issued_at == existing.issued_at:
                raise ClipGrantConflict()
            return await self._repeat(node, current, requested)
        self._count(grant)
        return IssuedClipGrant(grant, upload)

    def _count(self, grant: ClipUploadGrant) -> None:
        self._metrics.clip_grants_issued_total.add(1, {"node_id": str(grant.node_id)})
