"""``purge_episode_photos``: la foto del episodio se borra al vencer o al pasar a productivo.

TASK-573 (A-112, punto 4; QF5 ampliada). Diaria, a las 04:00 UTC, por organización (el
planificador de U-02 da a cada una su transacción y su contexto de iteración periódica; el
registro en la raíz es de ``fleet.registration``). En la transacción de la organización, con el
instante del ``Clock`` inyectado:

1. **retiradas** (``cause = productive``): **todas** las fotos de las zonas cuya compuerta de uso se
   aprobó con la foto ya guardada (la zona pasó a ``productive``; ver ``episode_photo_queries``);
2. **vencidas** (``cause = expired``): las de ``expires_at <= now``. Una foto que cumple las dos
   cuenta como retirada (se borra en el paso 1).

**Por lotes**: ``DELETE`` de a lo sumo ``batch_size`` filas que devuelve solo las que borró, hasta
que un lote sale corto o se llega a ``max_batches`` por causa y ejecución (lo que quede, en la
siguiente; la lectura ya no sirve una foto vencida ni retirada, así que la espera no la expone).

**Idempotente**: una ejecución repetida no encuentra nada; dos solapadas sobre la misma
organización se serializan en el candado de cada fila y la segunda no borra ni cuenta ninguna. La
métrica ``episode_photos_purged_total`` suma exactamente las filas borradas, por causa; nunca por
nodo ni por zona (NFR-GOB-13) y nunca con bytes.

**Frente a una lectura simultánea** (MVCC de PostgreSQL): la lectura de la imagen es una sola
sentencia con su instantánea, que ve la fila entera o no la ve; el ``DELETE`` no espera a las
lecturas ni las bloquea. La petición recibe los bytes completos (y su ``episode_photo_read``
``success``) o ``not_found``, nunca una respuesta parcial.

**Lo que queda en disco.** PostgreSQL no tiene un ``secure_delete``: el ``DELETE`` deja la tupla
muerta (en el montón y, para imágenes de más de unos 2 KB, en la TOAST) con sus bytes hasta que
``VACUUM`` la poda y el espacio se reutiliza, y el WAL conserva el ``INSERT`` original hasta que se
recicla su segmento. ``gob_0043`` dispara la limpieza automática de la tabla y de su TOAST a partir
de 50 tuplas muertas, sin esperar al 20 % de la tabla; la copia diaria de la base guarda las fotos
de su día hasta su propia retención (A-112, «Consecuencias»). La purga no escribe en el expediente
ni en la auditoría: la foto nunca estuvo en la cadena (QF10).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    PostgresEpisodePhotoQueries,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "MAX_BATCHES",
    "PURGE_BATCH_SIZE",
    "PURGE_EPISODE_PHOTOS",
    "PURGE_EPISODE_PHOTOS_SCHEDULE",
    "EpisodePhotoPurger",
    "PurgeCause",
    "PurgeReport",
    "PurgeRepository",
    "register_purge_episode_photos",
]

PURGE_EPISODE_PHOTOS: Final = "purge_episode_photos"
PURGE_EPISODE_PHOTOS_SCHEDULE: Final = Schedule.daily(hour=4)
"""Diaria a las 04:00 UTC (TASK-573), entre ``alert_expiring_certificates`` (03:00) y
``expire_walk_test_sessions`` (05:00)."""
PURGE_BATCH_SIZE: Final = 500
"""Fotos por lote ``[objetivo propio]``: unas 2,5 días de una zona con 200 episodios al día."""
MAX_BATCHES: Final = 40
"""Lotes por causa, organización y ejecución ``[objetivo propio]``: 20 000 fotos."""


class PurgeCause(enum.StrEnum):
    """Atributo ``cause`` de ``episode_photos_purged_total``."""

    EXPIRED = "expired"
    PRODUCTIVE = "productive"


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Lo que borró una ejecución en una organización, por causa."""

    retired: tuple[uuid.UUID, ...] = field(default=())
    expired: tuple[uuid.UUID, ...] = field(default=())


class PurgeRepository(Protocol):
    """Lo que la purga usa de ``PostgresEpisodePhotoQueries``."""

    async def purge_retired(
        self, transaction: Transaction, now: datetime, *, limit: int
    ) -> tuple[uuid.UUID, ...]: ...

    async def purge_expired(
        self, transaction: Transaction, now: datetime, *, limit: int
    ) -> tuple[uuid.UUID, ...]: ...


async def _in_batches(
    delete: Callable[[int], Awaitable[tuple[uuid.UUID, ...]]], batch_size: int, max_batches: int
) -> tuple[uuid.UUID, ...]:
    deleted: list[uuid.UUID] = []
    for _ in range(max_batches):
        batch = await delete(batch_size)
        deleted.extend(batch)
        if len(batch) < batch_size:
            break
    return tuple(deleted)


@repository
class EpisodePhotoPurger:
    """El manejador de ``purge_episode_photos`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        batch_size: int = PURGE_BATCH_SIZE,
        max_batches: int = MAX_BATCHES,
        queries: PurgeRepository | None = None,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        if type(max_batches) is not int or max_batches < 1:
            raise ValueError("max_batches debe ser al menos 1")
        self._queries = queries if queries is not None else PostgresEpisodePhotoQueries(database)
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()
        self._batch_size = batch_size
        self._max_batches = max_batches

    def __repr__(self) -> str:
        return "EpisodePhotoPurger()"

    async def purge(self, transaction: Transaction) -> PurgeReport:
        """Una pasada sobre la organización de ``transaction``."""
        # Sin redondear: ``expires_at`` sale del mismo reloj con toda su precisión.
        now = self._clock.now()
        queries = self._queries
        retired = await _in_batches(
            lambda limit: queries.purge_retired(transaction, now, limit=limit),
            self._batch_size,
            self._max_batches,
        )
        expired = await _in_batches(
            lambda limit: queries.purge_expired(transaction, now, limit=limit),
            self._batch_size,
            self._max_batches,
        )
        counter = self._metrics.episode_photos_purged_total
        for cause, deleted in ((PurgeCause.PRODUCTIVE, retired), (PurgeCause.EXPIRED, expired)):
            if deleted:
                counter.add(len(deleted), {"cause": cause.value})
        return PurgeReport(retired=retired, expired=expired)


def register_purge_episode_photos(
    registry: PeriodicTaskRegistry, purger: EpisodePhotoPurger
) -> PeriodicTask:
    """Registra ``purge_episode_photos`` diaria, por organización (lo llama la raíz)."""

    async def handler(transaction: Transaction) -> None:
        await purger.purge(transaction)

    return registry.register(
        PURGE_EPISODE_PHOTOS, PURGE_EPISODE_PHOTOS_SCHEDULE, handler, unit=ActorUnit.U03
    )
