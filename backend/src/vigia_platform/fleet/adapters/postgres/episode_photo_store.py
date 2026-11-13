"""``fleet.episode_photo`` sobre PostgreSQL (TASK-572; A-112; BR-GOB-84).

Todas las sentencias van en la transacción de la presentación, con el ``ScopeContext`` del nodo
(la seguridad a nivel de fila filtra por su organización) y nombran explícita la organización
(defensa en profundidad, NFR-GOB-30):

- ``claim``: ``INSERT … ON CONFLICT (organization_id, photo_id) DO NOTHING RETURNING``. Es la
  idempotencia **atómica** del paso 5: una segunda presentación simultánea del mismo ``photo_id``
  espera en la clave única a que la primera confirme o se revierta y, si confirmó, no inserta;
- ``stored``: la fila ya aceptada con ese ``photo_id`` (su ``canonical_sha256`` y su recibo),
  para resolver el duplicado o el conflicto en la misma transacción;
- ``retention_days`` y ``catalog_versions``: las del paso 7 de la ingesta
  (``PostgresIngestStore``);
- ``mounting_spans``: los intervalos de la compuerta de **montaje** que se cruzan con la ventana
  (paso 8, A-112).

SQL sin valores en trazas ni registros (NFR-GOB-57); los bytes de la imagen solo viajan como
parámetro del ``INSERT``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.fleet.adapters.postgres.ingest_queries import PostgresIngestStore
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.episode_photo import EpisodePhotoRow
from vigia_platform.fleet.domain.ingest_order import CatalogVersionView, GateSpan
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["COLUMNS", "PostgresEpisodePhotoStore", "StoredPhoto"]

COLUMNS: Final = (
    "organization_id",
    "photo_id",
    "plant_id",
    "zone_id",
    "node_id",
    "episode_id",
    "segment_index",
    "classified_as",
    "family",
    "standard_id",
    "standard_version",
    "max_confidence",
    "started_at",
    "ended_at",
    "received_at",
    "expires_at",
    "camera_id",
    "frame_at",
    "width",
    "height",
    "size_bytes",
    "sha256",
    "blur_params_version",
    "image",
    "image_absent_reason",
    "canonical_sha256",
    "platform_record_id",
)
"""Las columnas de ``fleet.episode_photo`` (``gob_0042``), en el orden del ``INSERT``."""

_CLAIM: Final = text(
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, camera_id, frame_at, width,"
    " height, size_bytes, sha256, blur_params_version, image, image_absent_reason,"
    " canonical_sha256, platform_record_id)"
    " VALUES (:organization_id, :photo_id, :plant_id, :zone_id, :node_id, :episode_id,"
    " :segment_index, :classified_as, :family, :standard_id, :standard_version, :max_confidence,"
    " :started_at, :ended_at, :received_at, :expires_at, :camera_id, :frame_at, :width, :height,"
    " :size_bytes, :sha256, :blur_params_version, :image, :image_absent_reason,"
    " :canonical_sha256, :platform_record_id)"
    " ON CONFLICT (organization_id, photo_id) DO NOTHING RETURNING photo_id"
)
_STORED: Final = text(
    "SELECT p.canonical_sha256, p.platform_record_id, p.received_at FROM fleet.episode_photo AS p"
    " WHERE p.organization_id = :organization_id AND p.photo_id = :photo_id"
)
_MOUNTING_SPANS: Final = text(
    "SELECT h.status, h.effective_from, h.effective_until FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = :organization_id AND h.zone_id = :zone_id"
    " AND h.gate = 'mounting'"
    " AND h.effective && tstzrange(CAST(:window_start AS timestamptz),"
    " CAST(:window_end AS timestamptz), '[]')"
)


@dataclass(frozen=True, slots=True)
class StoredPhoto:
    """La foto ya aceptada: su hash canónico y lo que forma su recibo."""

    canonical_sha256: str
    platform_record_id: uuid.UUID
    received_at: datetime


def _plain(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


@repository
class PostgresEpisodePhotoStore:
    """Las sentencias de la ingesta de la foto del episodio."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._ingest = PostgresIngestStore(database)

    async def claim(self, transaction: Transaction, row: EpisodePhotoRow) -> bool:
        """``True`` si la fila entró; ``False`` si ya había una con ese ``photo_id``."""
        if row.organization_id != transaction.context.organization_id:
            raise ValueError("la foto es de otra organización que la transacción")
        values = {column: getattr(row, column) for column in COLUMNS}
        inserted = (await transaction.execute(_CLAIM, values)).first()
        return inserted is not None

    async def stored(self, transaction: Transaction, photo_id: uuid.UUID) -> StoredPhoto | None:
        row = (
            await transaction.execute(
                _STORED,
                {"organization_id": transaction.context.organization_id, "photo_id": photo_id},
            )
        ).first()
        if row is None:
            return None
        return StoredPhoto(
            str(row.canonical_sha256), _plain(row.platform_record_id), row.received_at
        )

    async def retention_days(self, transaction: Transaction, node_id: uuid.UUID) -> int | None:
        return await self._ingest.retention_days(transaction, node_id)

    async def catalog_versions(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> tuple[CatalogVersionView, ...]:
        return await self._ingest.catalog_versions(transaction, zone_id, window)

    async def mounting_spans(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> tuple[GateSpan, ...]:
        rows = (
            await transaction.execute(
                _MOUNTING_SPANS,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "window_start": window.start,
                    "window_end": window.end,
                },
            )
        ).all()
        return tuple(
            GateSpan(row.status == "approved", row.effective_from, row.effective_until)
            for row in rows
        )
