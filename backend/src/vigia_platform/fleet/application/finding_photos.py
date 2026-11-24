"""La foto del hallazgo para ``loop`` (TASK-589; A-128 §5 y §6): el puerto de lectura por registros.

``loop`` no lee SQL de ``fleet``: el detalle, la ficha de vida y la ruta de la imagen piden las
fotos por este puerto. ``FindingPhotos`` lo realiza sobre ``PostgresEpisodePhotoQueries`` con el
``Clock`` inyectado (el instante del vencimiento; ningún paso lee la hora del sistema).

- ``record_photos(context, plant_id=…, zone_id=…, keys=…)``: por cada tramo de un registro del
  hallazgo (``FindingPhotoKey``: el ``record_id`` **del nodo**, el ``finding_id`` o el
  ``detection_id`` del documento, su episodio y su tramo), sus fotos ``productive`` vivas en la
  planta y la zona del hallazgo, por ``photo_index``. Una foto ``commissioning``, de otro registro,
  de otra zona, de otro episodio o de otro tramo no sale. Solo metadatos: nunca bytes, suma,
  tamaño ni URL, y **sin auditoría propia** (la lectura que la contiene ya deja la suya).
- ``finding_image(transaction, photo_id, plant_id=…, zone_id=…, record_ids=…)``: la foto con sus
  bytes, solo si es ``productive``, está viva y es de uno de los registros del hallazgo
  (``record_ids`` son sus ``platform_record_id``), en la transacción del llamador (la de su
  auditoría).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Protocol

from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    FindingPhoto,
    FindingPhotoKey,
    PostgresEpisodePhotoQueries,
    StoredImage,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "FindingPhoto",
    "FindingPhotoKey",
    "FindingPhotoPort",
    "FindingPhotos",
    "StoredImage",
]


class FindingPhotoPort(Protocol):
    """Lo que ``loop`` usa de la foto del hallazgo."""

    async def record_photos(
        self,
        context: ScopeContext,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        keys: Sequence[FindingPhotoKey],
    ) -> Mapping[FindingPhotoKey, tuple[FindingPhoto, ...]]: ...

    async def finding_image(
        self,
        transaction: Transaction,
        photo_id: uuid.UUID,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        record_ids: Sequence[uuid.UUID],
    ) -> StoredImage | None: ...


@repository
class FindingPhotos:
    """``FindingPhotoPort`` sobre PostgreSQL con el reloj inyectado."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        clock: Clock,
        queries: PostgresEpisodePhotoQueries | None = None,
    ) -> None:
        self._clock = clock
        self._queries = queries if queries is not None else PostgresEpisodePhotoQueries(database)

    def __repr__(self) -> str:
        return "FindingPhotos()"

    async def record_photos(
        self,
        context: ScopeContext,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        keys: Sequence[FindingPhotoKey],
    ) -> Mapping[FindingPhotoKey, tuple[FindingPhoto, ...]]:
        unique = tuple(dict.fromkeys(keys))
        if not unique:
            return {}
        return await self._queries.record_photos(
            context, plant_id=plant_id, zone_id=zone_id, keys=unique, now=self._clock.now()
        )

    async def finding_image(
        self,
        transaction: Transaction,
        photo_id: uuid.UUID,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        record_ids: Sequence[uuid.UUID],
    ) -> StoredImage | None:
        if not record_ids:
            return None
        return await self._queries.finding_image(
            transaction,
            photo_id,
            plant_id=plant_id,
            zone_id=zone_id,
            record_ids=tuple(record_ids),
            now=self._clock.now(),
        )
