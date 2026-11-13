"""Lectura y purga de ``fleet.episode_photo`` sobre PostgreSQL (TASK-573; A-112; QF5 y QF10).

Todas las sentencias nombran explícita la organización de la transacción (defensa en profundidad,
NFR-GOB-30: sin RLS, como superusuario, siguen sin tocar otra organización) y reciben el instante
del ``Clock`` inyectado (``:now``); ninguna lee la hora de la base.

**Foto retirada.** Una foto deja de existir para la lectura (y la purga la borra) si su zona tiene
una compuerta de **uso** ``approved`` que ya empezó (``effective_from <= now``) y que seguía vigente
cuando la foto llegó o después (``effective_until`` nulo o posterior a ``received_at``): la zona
pasó a ``productive`` con la foto ya guardada (BR-GOB-30, A-112 punto 5). Se mira la historia
(``catalog.gate_state_history``, la verdad de BR-GOB-20), no la proyección: una aprobación revocada
antes de la purga no devuelve a la vista las fotos que ya habían quedado retiradas. Una foto que
llega después de una revocación es de la nueva puesta en marcha y se ve.

**Vencida**: ``expires_at <= now``. A 1 ms de ``expires_at`` la foto sigue; en ``expires_at`` ni se
sirve ni sobrevive a la purga.

- ``zone``: la zona en la organización del contexto, si la RLS la deja ver;
- ``zone_retired``: la compuerta de uso de la zona está aprobada **ahora** (el listado responde
  ``not_found`` entero);
- ``page`` y ``cursor``: el listado por clave, el más reciente primero por ``(started_at,
  photo_id)``, con ``from``/``to`` sobre ``started_at`` (el inicio del tramo: cuándo hubo el
  episodio). **Nunca** selecciona la columna ``image``;
- ``location`` y ``image``: la foto de la petición de imagen; ``image`` es la única sentencia que
  lee los bytes, y solo de una foto viva;
- ``purge_retired`` y ``purge_expired``: ``DELETE`` de a lo sumo ``limit`` filas, que devuelve solo
  las que borró (una pasada repetida o solapada no cuenta dos veces).

SQL sin valores en trazas ni registros (NFR-GOB-57); ``StoredImage`` no muestra sus bytes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "EpisodePhotoSummary",
    "PhotoLocation",
    "PostgresEpisodePhotoQueries",
    "StoredImage",
]

_ZONE: Final = text(
    "SELECT z.organization_id, z.plant_id, z.zone_id FROM identity.zone AS z"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)
_ZONE_RETIRED: Final = text(
    "SELECT EXISTS (SELECT 1 FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = :organization_id AND h.zone_id = :zone_id"
    " AND h.gate = 'usage' AND h.status = 'approved'"
    " AND h.effective_from <= CAST(:now AS timestamptz)"
    " AND (h.effective_until IS NULL OR h.effective_until > CAST(:now AS timestamptz)))"
    " AS retired"
)
_CURSOR: Final = text(
    "SELECT p.started_at, p.photo_id FROM fleet.episode_photo AS p"
    " WHERE p.organization_id = :organization_id AND p.zone_id = :zone_id"
    " AND p.photo_id = :photo_id AND p.expires_at > CAST(:now AS timestamptz)"
    " AND NOT EXISTS (SELECT 1 FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = p.organization_id AND h.zone_id = p.zone_id"
    " AND h.gate = 'usage' AND h.status = 'approved'"
    " AND h.effective_from <= CAST(:now AS timestamptz)"
    " AND (h.effective_until IS NULL OR h.effective_until > p.received_at))"
)
_PAGE: Final = text(
    "SELECT p.photo_id, p.organization_id, p.plant_id, p.zone_id, p.node_id, p.episode_id,"
    " p.segment_index, p.classified_as, p.family, p.standard_id, p.standard_version,"
    " p.max_confidence, p.started_at, p.ended_at, p.received_at, p.expires_at, p.camera_id,"
    " p.frame_at, p.width, p.height, p.size_bytes, p.blur_params_version, p.image_absent_reason"
    " FROM fleet.episode_photo AS p"
    " WHERE p.organization_id = :organization_id AND p.zone_id = :zone_id"
    " AND p.expires_at > CAST(:now AS timestamptz)"
    " AND NOT EXISTS (SELECT 1 FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = p.organization_id AND h.zone_id = p.zone_id"
    " AND h.gate = 'usage' AND h.status = 'approved'"
    " AND h.effective_from <= CAST(:now AS timestamptz)"
    " AND (h.effective_until IS NULL OR h.effective_until > p.received_at))"
    " AND (CAST(:since AS timestamptz) IS NULL OR p.started_at >= CAST(:since AS timestamptz))"
    " AND (CAST(:until AS timestamptz) IS NULL OR p.started_at < CAST(:until AS timestamptz))"
    " AND (CAST(:after_started_at AS timestamptz) IS NULL"
    " OR (p.started_at, p.photo_id) < (CAST(:after_started_at AS timestamptz),"
    " CAST(:after_photo_id AS uuid)))"
    " ORDER BY p.started_at DESC, p.photo_id DESC LIMIT :limit"
)
_LOCATION: Final = text(
    "SELECT p.organization_id, p.plant_id, p.zone_id FROM fleet.episode_photo AS p"
    " WHERE p.organization_id = :organization_id AND p.photo_id = :photo_id"
)
_IMAGE: Final = text(
    "SELECT p.plant_id, p.zone_id, p.size_bytes, p.sha256, p.image FROM fleet.episode_photo AS p"
    " WHERE p.organization_id = :organization_id AND p.photo_id = :photo_id"
    " AND p.expires_at > CAST(:now AS timestamptz)"
    " AND NOT EXISTS (SELECT 1 FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = p.organization_id AND h.zone_id = p.zone_id"
    " AND h.gate = 'usage' AND h.status = 'approved'"
    " AND h.effective_from <= CAST(:now AS timestamptz)"
    " AND (h.effective_until IS NULL OR h.effective_until > p.received_at))"
)
_PURGE_RETIRED: Final = text(
    "DELETE FROM fleet.episode_photo AS p WHERE p.organization_id = :organization_id"
    " AND p.photo_id IN (SELECT q.photo_id FROM fleet.episode_photo AS q"
    " WHERE q.organization_id = :organization_id"
    " AND EXISTS (SELECT 1 FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = q.organization_id AND h.zone_id = q.zone_id"
    " AND h.gate = 'usage' AND h.status = 'approved'"
    " AND h.effective_from <= CAST(:now AS timestamptz)"
    " AND (h.effective_until IS NULL OR h.effective_until > q.received_at))"
    " ORDER BY q.photo_id LIMIT :limit)"
    " RETURNING p.photo_id"
)
_PURGE_EXPIRED: Final = text(
    "DELETE FROM fleet.episode_photo AS p WHERE p.organization_id = :organization_id"
    " AND p.photo_id IN (SELECT q.photo_id FROM fleet.episode_photo AS q"
    " WHERE q.organization_id = :organization_id"
    " AND q.expires_at <= CAST(:now AS timestamptz)"
    " ORDER BY q.photo_id LIMIT :limit)"
    " AND p.expires_at <= CAST(:now AS timestamptz)"
    " RETURNING p.photo_id"
)


def _plain(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _optional(value: object) -> uuid.UUID | None:
    return None if value is None else _plain(value)


@dataclass(frozen=True, slots=True)
class PhotoLocation:
    """Dónde está una foto: lo que se autoriza (``commissioning.run`` sobre su zona)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class EpisodePhotoSummary:
    """Una foto del listado: metadatos y el motivo de su ausencia; nunca los bytes ni su suma."""

    photo_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    episode_id: uuid.UUID
    segment_index: int
    classified_as: str
    family: str
    standard_id: uuid.UUID
    standard_version: int
    max_confidence: float
    started_at: datetime
    ended_at: datetime
    received_at: datetime
    expires_at: datetime
    camera_id: uuid.UUID | None
    frame_at: datetime | None
    width: int | None
    height: int | None
    size_bytes: int | None
    blur_params_version: str | None
    image_absent_reason: str | None


@dataclass(frozen=True, slots=True)
class StoredImage:
    """La foto viva de una petición de imagen: su zona, lo declarado y sus bytes (si los hay)."""

    plant_id: uuid.UUID
    zone_id: uuid.UUID
    size_bytes: int | None
    sha256: str | None
    data: bytes | None = field(repr=False)


def _summary(row: Any) -> EpisodePhotoSummary:
    values = row._mapping
    return EpisodePhotoSummary(
        photo_id=_plain(values["photo_id"]),
        organization_id=_plain(values["organization_id"]),
        plant_id=_plain(values["plant_id"]),
        zone_id=_plain(values["zone_id"]),
        node_id=_plain(values["node_id"]),
        episode_id=_plain(values["episode_id"]),
        segment_index=int(values["segment_index"]),
        classified_as=str(values["classified_as"]),
        family=str(values["family"]),
        standard_id=_plain(values["standard_id"]),
        standard_version=int(values["standard_version"]),
        max_confidence=float(values["max_confidence"]),
        started_at=values["started_at"],
        ended_at=values["ended_at"],
        received_at=values["received_at"],
        expires_at=values["expires_at"],
        camera_id=_optional(values["camera_id"]),
        frame_at=values["frame_at"],
        width=values["width"],
        height=values["height"],
        size_bytes=values["size_bytes"],
        blur_params_version=values["blur_params_version"],
        image_absent_reason=values["image_absent_reason"],
    )


@repository
class PostgresEpisodePhotoQueries:
    """Las sentencias de la lectura (listado e imagen) y de la purga de la foto del episodio."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def zone(self, context: ScopeContext, zone_id: uuid.UUID) -> PhotoLocation | None:
        """La zona en la organización del contexto (si la RLS la deja ver), o ``None``."""
        rows = await self._database.read(
            context, _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        if not rows:
            return None
        row = rows[0]
        return PhotoLocation(_plain(row.organization_id), _plain(row.plant_id), _plain(row.zone_id))

    async def location(self, context: ScopeContext, photo_id: uuid.UUID) -> PhotoLocation | None:
        """La planta y la zona de la foto (si la RLS la deja ver), o ``None``; sin bytes."""
        rows = await self._database.read(
            context, _LOCATION, {"organization_id": context.organization_id, "photo_id": photo_id}
        )
        if not rows:
            return None
        row = rows[0]
        return PhotoLocation(_plain(row.organization_id), _plain(row.plant_id), _plain(row.zone_id))

    async def zone_retired(
        self, transaction: Transaction, zone_id: uuid.UUID, now: datetime
    ) -> bool:
        """¿La compuerta de uso de la zona está aprobada en ``now``?"""
        row = (
            await transaction.execute(
                _ZONE_RETIRED,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "now": now,
                },
            )
        ).one()
        return bool(row.retired)

    async def page(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        *,
        now: datetime,
        since: datetime | None,
        until: datetime | None,
        after: uuid.UUID | None,
        limit: int,
    ) -> tuple[EpisodePhotoSummary, ...] | None:
        """Las fotos vivas de la zona, la más reciente primero; ``None`` si el cursor no es una
        foto viva de la zona."""
        organization_id = transaction.context.organization_id
        after_started_at: datetime | None = None
        after_photo_id: uuid.UUID | None = None
        if after is not None:
            cursor = (
                await transaction.execute(
                    _CURSOR,
                    {
                        "organization_id": organization_id,
                        "zone_id": zone_id,
                        "photo_id": after,
                        "now": now,
                    },
                )
            ).first()
            if cursor is None:
                return None
            after_started_at, after_photo_id = cursor.started_at, _plain(cursor.photo_id)
        result = await transaction.execute(
            _PAGE,
            {
                "organization_id": organization_id,
                "zone_id": zone_id,
                "now": now,
                "since": since,
                "until": until,
                "after_started_at": after_started_at,
                "after_photo_id": after_photo_id,
                "limit": limit,
            },
        )
        return tuple(_summary(row) for row in result)

    async def image(
        self, transaction: Transaction, photo_id: uuid.UUID, now: datetime
    ) -> StoredImage | None:
        """La foto viva (sin vencer ni retirada) con sus bytes, o ``None``."""
        row = (
            await transaction.execute(
                _IMAGE,
                {
                    "organization_id": transaction.context.organization_id,
                    "photo_id": photo_id,
                    "now": now,
                },
            )
        ).first()
        if row is None:
            return None
        data = row.image
        return StoredImage(
            plant_id=_plain(row.plant_id),
            zone_id=_plain(row.zone_id),
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            data=None if data is None else bytes(data),
        )

    async def purge_retired(
        self, transaction: Transaction, now: datetime, *, limit: int
    ) -> tuple[uuid.UUID, ...]:
        """Borra a lo sumo ``limit`` fotos de zonas que pasaron a ``productive``."""
        result = await transaction.execute(
            _PURGE_RETIRED,
            {"organization_id": transaction.context.organization_id, "now": now, "limit": limit},
        )
        return tuple(_plain(row.photo_id) for row in result)

    async def purge_expired(
        self, transaction: Transaction, now: datetime, *, limit: int
    ) -> tuple[uuid.UUID, ...]:
        """Borra a lo sumo ``limit`` fotos con ``expires_at <= now``."""
        result = await transaction.execute(
            _PURGE_EXPIRED,
            {"organization_id": transaction.context.organization_id, "now": now, "limit": limit},
        )
        return tuple(_plain(row.photo_id) for row in result)
