"""``mark_orphan_clips``: subidas sin hallazgo convertidas en huérfanos contados (LC-GOB-18).

BR-GOB-94 y sus notas del 2026-09-23, nota de cadencias de BLM §2.6 e infraestructura §4.3. Cada
hora, por organización (el planificador de U-02 da a cada una su transacción y su contexto de
iteración periódica; el registro en la raíz es de TASK-227, aquí solo
``register_mark_orphan_clips``):

1. las concesiones ``evidence`` emitidas hace **más de 24 h** que siguen ``issued`` (ningún
   registro aceptado las citó: esa marca la deja la ingesta, TASK-221), a lo sumo
   ``batch_size`` por ejecución (las demás, en la siguiente hora);
2. ``head_object`` de todas **en paralelo** con tope (``ClipObjectStore.heads``: 10 s por consulta);
   si una falla, ``StorageUnavailable`` **antes** de escribir nada: la organización queda sin
   cambios para el ciclo siguiente;
3. objeto presente → ``issued → used → orphan`` (``orphan_outcome``); sin objeto y vencida →
   ``expired``. ``purpose = verification`` nunca entra (un clip de verificación no tiene hallazgo
   que lo cite). Nada se borra ni se sobrescribe en el depósito.

**Una sola vez.** Las transiciones son ``UPDATE`` condicionales (``status = 'issued'``) que solo
devuelven las filas que cambiaron, y el contador ``clip_grants_orphaned_total`` (por nodo, sin
zona) suma exactamente esas filas: dos ejecuciones solapadas sobre la misma organización se
serializan en el bloqueo de cada fila y la segunda no cambia ni cuenta ninguna. El contador se
suma al terminar el manejador, dentro de la transacción de la organización: si el planificador la
deshiciera después (valla del arrendamiento perdida), la cifra exacta sigue siendo la de la base
(``node_clip_counts``).

``node_clip_counts`` es la consulta de solo lectura que TASK-225 usa para ``orphan_clips_growing``
(> 50 huérfanos o > 5 % de los clips del día): por nodo, los huérfanos y los clips ``evidence``
emitidos en la ventana.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Protocol

from vigia_platform.fleet.adapters.postgres.clip_grant_store import (
    NodeClipCounts,
    PostgresClipGrants,
)
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.domain.clip_upload_grant import (
    ORPHAN_AFTER,
    ClipUploadGrant,
    OrphanOutcome,
    orphan_outcome,
    to_millisecond,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "MARK_ORPHAN_CLIPS",
    "MARK_ORPHAN_CLIPS_SCHEDULE",
    "SWEEP_BATCH_SIZE",
    "OrphanClipSweeper",
    "OrphanSweepReport",
    "SweepRepository",
    "register_mark_orphan_clips",
]

MARK_ORPHAN_CLIPS: Final = "mark_orphan_clips"
MARK_ORPHAN_CLIPS_SCHEDULE: Final = Schedule.every(3600)
"""Cada hora (nota de cadencias de BLM §2.6; LC-GOB-18)."""
SWEEP_BATCH_SIZE: Final = 1000
"""Concesiones por organización y ejecución ``[objetivo propio]``: con 16 consultas a la vez,
cabe en el barrido de 20 s de NFR-GOB-08."""


@dataclass(frozen=True, slots=True)
class OrphanSweepReport:
    """Lo que cambió una ejecución en una organización: ``{clip_id: node_id}``."""

    orphaned: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    expired: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)


class SweepRepository(Protocol):
    """Lo que el barrido usa de ``PostgresClipGrants`` (un doble en memoria en PR-GOB-20)."""

    async def orphan_candidates(
        self, transaction: Transaction, *, issued_before: datetime, limit: int
    ) -> tuple[ClipUploadGrant, ...]: ...

    async def mark_orphans(
        self, transaction: Transaction, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]: ...

    async def mark_expired(
        self, transaction: Transaction, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]: ...

    async def node_clip_counts(
        self, transaction: Transaction, *, since: datetime, until: datetime
    ) -> tuple[NodeClipCounts, ...]: ...


class OrphanClipSweeper:
    """El manejador de ``mark_orphan_clips`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        store: ClipObjectStore,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        batch_size: int = SWEEP_BATCH_SIZE,
        grants: SweepRepository | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._grants = grants if grants is not None else PostgresClipGrants(database)
        self._store = store
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()
        self._batch_size = batch_size

    async def sweep(self, transaction: Transaction) -> OrphanSweepReport:
        """Una pasada sobre la organización de ``transaction``."""
        now = to_millisecond(self._clock.now())
        candidates = await self._grants.orphan_candidates(
            transaction, issued_before=now - ORPHAN_AFTER, limit=self._batch_size
        )
        if not candidates:
            return OrphanSweepReport()
        # Todas las consultas antes de escribir: con el almacén caído, nada cambia.
        facts = await self._store.heads([grant.storage_key for grant in candidates])
        to_orphan: list[uuid.UUID] = []
        to_expire: list[uuid.UUID] = []
        for grant in candidates:
            outcome = orphan_outcome(grant, now, uploaded=facts[grant.storage_key] is not None)
            if outcome is OrphanOutcome.ORPHAN:
                to_orphan.append(grant.clip_id)
            elif outcome is OrphanOutcome.EXPIRE:
                to_expire.append(grant.clip_id)
        orphaned = await self._grants.mark_orphans(transaction, to_orphan, now)
        expired = await self._grants.mark_expired(transaction, to_expire, now)
        for node_id, count in sorted(Counter(orphaned.values()).items()):
            self._metrics.clip_grants_orphaned_total.add(count, {"node_id": str(node_id)})
        return OrphanSweepReport(orphaned=orphaned, expired=expired)

    async def node_clip_counts(
        self, transaction: Transaction, *, until: datetime, window: timedelta = ORPHAN_AFTER
    ) -> tuple[NodeClipCounts, ...]:
        """Por nodo, huérfanos y clips ``evidence`` de ``[until - window, until)`` (TASK-225)."""
        if window <= timedelta(0):
            raise ValueError("la ventana debe ser positiva")
        return await self._grants.node_clip_counts(transaction, since=until - window, until=until)


def register_mark_orphan_clips(
    registry: PeriodicTaskRegistry, sweeper: OrphanClipSweeper
) -> PeriodicTask:
    """Registra ``mark_orphan_clips`` cada hora, por organización (lo llama TASK-227)."""

    async def handler(transaction: Transaction) -> None:
        await sweeper.sweep(transaction)

    return registry.register(
        MARK_ORPHAN_CLIPS, MARK_ORPHAN_CLIPS_SCHEDULE, handler, unit=ActorUnit.U03
    )
