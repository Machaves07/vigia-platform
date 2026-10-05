"""``fleet.clip_upload_grant`` y ``fleet.verification_clip`` sobre PostgreSQL (LC-GOB-13, 18).

Tablas de ``gob_0018`` con la reemisión y ``first_served_at`` de ``gob_0022``. Toda sentencia va
con el ``ScopeContext`` de la operación (el del nodo, el de la persona o el de la iteración
periódica de una organización): la seguridad a nivel de fila filtra por organización y por
concesión de proveedor, y además cada sentencia nombra **explícitos** la organización del
contexto y, cuando aplica, el nodo o la zona (defensa en profundidad, NFR-GOB-30).

Escrituras, todas **condicionales** y con ``RETURNING`` (solo cuentan las filas que cambiaron):

- ``insert``: ``ON CONFLICT (clip_id) DO NOTHING``; ``False`` si el ``clip_id`` ya tenía fila;
- ``reissue``: misma fila con ``issued_at`` nuevo solo si sigue ``issued`` con el ``issued_at``
  leído (dos reemisiones simultáneas: gana una); la guarda ``reissue_guard`` de la base exige
  además que estuviera vencida;
- ``lock_for_confirmation``: ``SELECT ... FOR UPDATE`` de la concesión del nodo: el **único**
  candado de la confirmación; serializa las confirmaciones del mismo clip, que leen el
  ``VerificationClip`` ya creado en vez de crear otro;
- ``mark_used``: ``issued → used`` de la confirmación;
- ``mark_cited``: ``issued → used`` de las concesiones ``evidence`` que cita un registro aceptado
  de la ingesta (TASK-221), en su transacción;
- ``mark_orphans``: ``issued → used → orphan`` en dos sentencias de la misma transacción (la
  guarda de ``gob_0018`` no admite ``issued → orphan``) y solo de las que seguían ``issued``;
- ``mark_expired``: ``issued → expired`` solo de las que seguían ``issued`` y ya vencidas;
- ``mark_first_served``: el cierre ``first_served_at`` solo donde aún es nulo.

Dos ejecuciones solapadas de ``mark_orphan_clips`` se serializan en el bloqueo de cada fila: la
segunda vuelve a evaluar ``status = 'issued'``, ya no lo cumple y no cambia (ni cuenta) nada.

``mark_cited``, ``mark_orphans`` (``issued → used``) y ``mark_expired`` toman sus filas en una
subconsulta ``ORDER BY clip_id FOR UPDATE`` antes del ``UPDATE`` condicional: un ``UPDATE`` con
``= ANY(...)`` bloquea en el orden físico del recorrido, y una aceptación de la ingesta y un barrido
sobre las mismas concesiones podrían tomarlas en orden inverso e interbloquearse.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import ClipUploadPurpose

from vigia_platform.fleet.domain.clip_upload_grant import (
    ClipContentType,
    ClipUploadGrant,
    sha256_from_headers,
)
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.fleet.domain.verification_clip import VerificationClip
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["NodeClipCounts", "PostgresClipGrants", "ZoneRow"]

_GRANT: Final = text(
    "SELECT g.clip_id, g.organization_id, g.plant_id, g.zone_id, g.node_id, g.purpose,"
    " g.storage_key, g.content_type, g.max_size_bytes, g.required_headers, g.issued_at,"
    " g.expires_at, g.status, g.used_at, g.orphaned_at FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.clip_id = :clip_id"
)
_LOCK_GRANT: Final = text(
    "SELECT g.clip_id, g.organization_id, g.plant_id, g.zone_id, g.node_id, g.purpose,"
    " g.storage_key, g.content_type, g.max_size_bytes, g.required_headers, g.issued_at,"
    " g.expires_at, g.status, g.used_at, g.orphaned_at FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.node_id = :node_id"
    " AND g.clip_id = :clip_id FOR UPDATE"
)
_INSERT: Final = text(
    "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id, node_id,"
    " purpose, storage_key, content_type, max_size_bytes, required_headers, issued_at,"
    " expires_at, status)"
    " VALUES (:clip_id, :organization_id, :plant_id, :zone_id, :node_id, :purpose,"
    " :storage_key, :content_type, :max_size_bytes, CAST(:required_headers AS jsonb),"
    " :issued_at, :expires_at, 'issued')"
    " ON CONFLICT (clip_id) DO NOTHING RETURNING clip_id"
)
_REISSUE: Final = text(
    "UPDATE fleet.clip_upload_grant SET issued_at = :issued_at, expires_at = :expires_at"
    " WHERE organization_id = :organization_id AND node_id = :node_id AND clip_id = :clip_id"
    " AND status = 'issued' AND issued_at = :previous_issued_at RETURNING clip_id"
)
_MARK_USED: Final = text(
    "UPDATE fleet.clip_upload_grant SET status = 'used', used_at = :now"
    " WHERE organization_id = :organization_id AND node_id = :node_id AND clip_id = :clip_id"
    " AND status = 'issued' RETURNING clip_id"
)
_MARK_CITED: Final = text(
    "UPDATE fleet.clip_upload_grant SET status = 'used', used_at = :now"
    " WHERE organization_id = :organization_id AND clip_id IN ("
    "SELECT g.clip_id FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.plant_id = :plant_id"
    " AND g.zone_id = :zone_id AND g.node_id = :node_id"
    " AND g.clip_id = ANY(CAST(:clip_ids AS uuid[])) AND g.status = 'issued'"
    " AND g.purpose = 'evidence' AND g.issued_at <= :now"
    " ORDER BY g.clip_id FOR UPDATE)"
    " AND status = 'issued' RETURNING clip_id"
)
_INSERT_CLIP: Final = text(
    "INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id, zone_id, node_id,"
    " received_at, sha256)"
    " VALUES (:clip_id, :organization_id, :plant_id, :zone_id, :node_id, :received_at, :sha256)"
)
_CLIP: Final = text(
    "SELECT c.clip_id, c.organization_id, c.plant_id, c.zone_id, c.node_id, c.received_at,"
    " c.sha256, c.blur_check_result, c.first_served_at FROM fleet.verification_clip AS c"
    " WHERE c.organization_id = :organization_id AND c.clip_id = :clip_id"
)
_ZONE: Final = text(
    "SELECT z.organization_id, z.plant_id, z.zone_id FROM identity.zone AS z"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)
_CURSOR: Final = text(
    "SELECT c.received_at, c.clip_id FROM fleet.verification_clip AS c"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND c.clip_id = :clip_id"
)
_ZONE_CLIPS: Final = text(
    "SELECT c.clip_id, c.organization_id, c.plant_id, c.zone_id, c.node_id, c.received_at,"
    " c.sha256, c.blur_check_result, c.first_served_at FROM fleet.verification_clip AS c"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND (CAST(:after_received_at AS timestamptz) IS NULL"
    " OR (c.received_at, c.clip_id) < (CAST(:after_received_at AS timestamptz),"
    " CAST(:after_clip_id AS uuid)))"
    " ORDER BY c.received_at DESC, c.clip_id DESC LIMIT :limit"
)
_MARK_FIRST_SERVED: Final = text(
    "UPDATE fleet.verification_clip SET first_served_at = :now"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
    " AND clip_id = ANY(CAST(:clip_ids AS uuid[])) AND first_served_at IS NULL"
    " AND received_at <= :now RETURNING clip_id"
)
_ORPHAN_CANDIDATES: Final = text(
    "SELECT g.clip_id, g.organization_id, g.plant_id, g.zone_id, g.node_id, g.purpose,"
    " g.storage_key, g.content_type, g.max_size_bytes, g.required_headers, g.issued_at,"
    " g.expires_at, g.status, g.used_at, g.orphaned_at FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.status = 'issued'"
    " AND g.purpose = 'evidence' AND g.issued_at <= :issued_before"
    " ORDER BY g.issued_at, g.clip_id LIMIT :limit"
)
_TO_USED: Final = text(
    "UPDATE fleet.clip_upload_grant SET status = 'used', used_at = :now"
    " WHERE organization_id = :organization_id AND clip_id IN ("
    "SELECT g.clip_id FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id"
    " AND g.clip_id = ANY(CAST(:clip_ids AS uuid[])) AND g.status = 'issued'"
    " AND g.purpose = 'evidence' ORDER BY g.clip_id FOR UPDATE)"
    " AND status = 'issued' AND purpose = 'evidence' RETURNING clip_id"
)
_TO_ORPHAN: Final = text(
    "UPDATE fleet.clip_upload_grant SET status = 'orphan', orphaned_at = :now"
    " WHERE organization_id = :organization_id AND clip_id = ANY(CAST(:clip_ids AS uuid[]))"
    " AND status = 'used' AND used_at = :now AND purpose = 'evidence'"
    " RETURNING clip_id, node_id"
)
_TO_EXPIRED: Final = text(
    "UPDATE fleet.clip_upload_grant SET status = 'expired'"
    " WHERE organization_id = :organization_id AND clip_id IN ("
    "SELECT g.clip_id FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id"
    " AND g.clip_id = ANY(CAST(:clip_ids AS uuid[])) AND g.status = 'issued'"
    " AND g.purpose = 'evidence' AND g.expires_at <= :now ORDER BY g.clip_id FOR UPDATE)"
    " AND status = 'issued' AND purpose = 'evidence' AND expires_at <= :now"
    " RETURNING clip_id, node_id"
)
_NODE_COUNTS: Final = text(
    "SELECT g.node_id,"
    " count(*) FILTER (WHERE g.status = 'orphan' AND g.orphaned_at >= :since"
    " AND g.orphaned_at < :until) AS orphan_clips,"
    " count(*) FILTER (WHERE g.issued_at >= :since AND g.issued_at < :until) AS day_clips"
    " FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.purpose = 'evidence'"
    " AND ((g.status = 'orphan' AND g.orphaned_at >= :since AND g.orphaned_at < :until)"
    " OR (g.issued_at >= :since AND g.issued_at < :until))"
    " GROUP BY g.node_id ORDER BY g.node_id"
)


@dataclass(frozen=True, slots=True)
class ZoneRow:
    """Una zona con su planta, tal como la ve el contexto."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class NodeClipCounts:
    """Huérfanos y clips del intervalo de un nodo (``orphan_clips_growing``, TASK-225)."""

    node_id: uuid.UUID
    orphan_clips: int
    """Concesiones ``evidence`` que pasaron a ``orphan`` en el intervalo."""
    day_clips: int
    """Concesiones ``evidence`` emitidas en el intervalo."""


def _plain(value: object) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(str(value))


def _document(value: object) -> Any:
    if isinstance(value, bytes | str):
        return json.loads(value)
    return value


def _grant(row: Row[Any]) -> ClipUploadGrant:
    headers: Mapping[str, str] = _document(row.required_headers)
    return ClipUploadGrant(
        clip_id=_plain(row.clip_id),
        organization_id=_plain(row.organization_id),
        plant_id=_plain(row.plant_id),
        zone_id=_plain(row.zone_id),
        node_id=_plain(row.node_id),
        purpose=ClipUploadPurpose(row.purpose),
        content_type=ClipContentType(row.content_type),
        sha256=sha256_from_headers(headers),
        max_size_bytes=int(row.max_size_bytes),
        storage_key=str(row.storage_key),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        status=UploadGrantStatus(row.status),
        used_at=row.used_at,
        orphaned_at=row.orphaned_at,
    )


def _clip(row: Row[Any]) -> VerificationClip:
    blur = _document(row.blur_check_result)
    return VerificationClip(
        clip_id=_plain(row.clip_id),
        organization_id=_plain(row.organization_id),
        plant_id=_plain(row.plant_id),
        zone_id=_plain(row.zone_id),
        node_id=_plain(row.node_id),
        received_at=row.received_at,
        sha256=str(row.sha256),
        blur_check_result=None if blur is None else dict(blur),
        first_served_at=row.first_served_at,
    )


def _ids(values: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    return list(values)


@repository
class PostgresClipGrants:
    """Concesiones de clip y clips de verificación de una organización."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    # --- Concesión ---------------------------------------------------------------------------

    async def grant(self, context: ScopeContext, clip_id: uuid.UUID) -> ClipUploadGrant | None:
        """La concesión de ``clip_id`` en la organización del contexto, o ``None``."""
        rows = await self._database.read(
            context, _GRANT, {"organization_id": context.organization_id, "clip_id": clip_id}
        )
        return _grant(rows[0]) if rows else None

    async def insert(self, transaction: Transaction, grant: ClipUploadGrant) -> bool:
        """Alta en ``issued``; ``False`` si el ``clip_id`` ya tenía concesión (nada escrito)."""
        if grant.organization_id != transaction.context.organization_id:
            raise ValueError("la concesión es de otra organización que la transacción")
        result = await transaction.execute(
            _INSERT,
            {
                "clip_id": grant.clip_id,
                "organization_id": grant.organization_id,
                "plant_id": grant.plant_id,
                "zone_id": grant.zone_id,
                "node_id": grant.node_id,
                "purpose": grant.purpose.value,
                "storage_key": grant.storage_key,
                "content_type": grant.content_type.value,
                "max_size_bytes": grant.max_size_bytes,
                "required_headers": json.dumps(grant.required_headers, sort_keys=True),
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
            },
        )
        return result.first() is not None

    async def reissue(
        self, transaction: Transaction, grant: ClipUploadGrant, previous_issued_at: datetime
    ) -> bool:
        """``issued_at`` y ``expires_at`` nuevos si la fila sigue como se leyó."""
        result = await transaction.execute(
            _REISSUE,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": grant.node_id,
                "clip_id": grant.clip_id,
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "previous_issued_at": previous_issued_at,
            },
        )
        return result.first() is not None

    # --- Confirmación ------------------------------------------------------------------------

    async def lock_for_confirmation(
        self, transaction: Transaction, node_id: uuid.UUID, clip_id: uuid.UUID
    ) -> ClipUploadGrant | None:
        """La concesión de ``clip_id`` **de ese nodo**, bloqueada hasta el fin de la transacción."""
        result = await transaction.execute(
            _LOCK_GRANT,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "clip_id": clip_id,
            },
        )
        row = result.first()
        return None if row is None else _grant(row)

    async def verification_clip(
        self, transaction: Transaction, clip_id: uuid.UUID
    ) -> VerificationClip | None:
        result = await transaction.execute(
            _CLIP, {"organization_id": transaction.context.organization_id, "clip_id": clip_id}
        )
        row = result.first()
        return None if row is None else _clip(row)

    async def read_verification_clip(
        self, context: ScopeContext, clip_id: uuid.UUID
    ) -> VerificationClip | None:
        """El ``VerificationClip`` de ``clip_id`` (lectura sin transacción de escritura)."""
        rows = await self._database.read(
            context, _CLIP, {"organization_id": context.organization_id, "clip_id": clip_id}
        )
        return _clip(rows[0]) if rows else None

    async def insert_verification_clip(
        self, transaction: Transaction, clip: VerificationClip
    ) -> None:
        if clip.organization_id != transaction.context.organization_id:
            raise ValueError("el clip es de otra organización que la transacción")
        await transaction.execute(
            _INSERT_CLIP,
            {
                "clip_id": clip.clip_id,
                "organization_id": clip.organization_id,
                "plant_id": clip.plant_id,
                "zone_id": clip.zone_id,
                "node_id": clip.node_id,
                "received_at": clip.received_at,
                "sha256": clip.sha256,
            },
        )

    async def mark_used(
        self, transaction: Transaction, node_id: uuid.UUID, clip_id: uuid.UUID, now: datetime
    ) -> bool:
        """``issued → used`` de la concesión del nodo; ``False`` si ya no estaba ``issued``."""
        result = await transaction.execute(
            _MARK_USED,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "clip_id": clip_id,
                "now": now,
            },
        )
        return result.first() is not None

    async def mark_cited(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        clip_ids: Sequence[uuid.UUID],
        now: datetime,
    ) -> tuple[uuid.UUID, ...]:
        """``issued → used`` de las concesiones ``evidence`` del nodo y la zona que cita un registro
        aceptado (TASK-221; la otra mitad de BR-GOB-94): ``mark_orphan_clips`` solo mira las que
        siguen ``issued``. Bloquea las filas en orden de ``clip_id``; una ya cerrada no cambia."""
        if not clip_ids:
            return ()
        result = await transaction.execute(
            _MARK_CITED,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "zone_id": zone_id,
                "node_id": node_id,
                "clip_ids": _ids(clip_ids),
                "now": now,
            },
        )
        return tuple(_plain(row.clip_id) for row in result)

    # --- Consola -----------------------------------------------------------------------------

    async def zone(self, context: ScopeContext, zone_id: uuid.UUID) -> ZoneRow | None:
        """La zona en la organización del contexto (si la RLS la deja ver), o ``None``."""
        rows = await self._database.read(
            context, _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        if not rows:
            return None
        row = rows[0]
        return ZoneRow(_plain(row.organization_id), _plain(row.plant_id), _plain(row.zone_id))

    async def zone_clips(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> tuple[VerificationClip, ...] | None:
        """Los clips de la zona, el más reciente primero; ``None`` si el cursor no es de ella."""
        organization_id = transaction.context.organization_id
        after_received_at: datetime | None = None
        after_clip_id: uuid.UUID | None = None
        if after is not None:
            cursor = await transaction.execute(
                _CURSOR,
                {"organization_id": organization_id, "zone_id": zone_id, "clip_id": after},
            )
            row = cursor.first()
            if row is None:
                return None
            after_received_at, after_clip_id = row.received_at, _plain(row.clip_id)
        result = await transaction.execute(
            _ZONE_CLIPS,
            {
                "organization_id": organization_id,
                "zone_id": zone_id,
                "after_received_at": after_received_at,
                "after_clip_id": after_clip_id,
                "limit": limit,
            },
        )
        return tuple(_clip(row) for row in result)

    async def mark_first_served(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        clip_ids: Sequence[uuid.UUID],
        now: datetime,
    ) -> frozenset[uuid.UUID]:
        """El cierre ``first_served_at`` de los que aún no lo tenían; devuelve los marcados."""
        if not clip_ids:
            return frozenset()
        result = await transaction.execute(
            _MARK_FIRST_SERVED,
            {
                "organization_id": transaction.context.organization_id,
                "zone_id": zone_id,
                "clip_ids": _ids(clip_ids),
                "now": now,
            },
        )
        return frozenset(_plain(row.clip_id) for row in result)

    # --- Huérfanos ---------------------------------------------------------------------------

    async def orphan_candidates(
        self, transaction: Transaction, *, issued_before: datetime, limit: int
    ) -> tuple[ClipUploadGrant, ...]:
        """Concesiones ``evidence`` aún ``issued`` emitidas no después de ``issued_before``."""
        result = await transaction.execute(
            _ORPHAN_CANDIDATES,
            {
                "organization_id": transaction.context.organization_id,
                "issued_before": issued_before,
                "limit": limit,
            },
        )
        return tuple(_grant(row) for row in result)

    async def mark_orphans(
        self, transaction: Transaction, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]:
        """``issued → used → orphan`` de las que seguían ``issued``; ``{clip_id: node_id}``."""
        if not clip_ids:
            return {}
        organization_id = transaction.context.organization_id
        used = await transaction.execute(
            _TO_USED,
            {"organization_id": organization_id, "clip_ids": _ids(clip_ids), "now": now},
        )
        changed = [_plain(row.clip_id) for row in used]
        if not changed:
            return {}
        orphaned = await transaction.execute(
            _TO_ORPHAN,
            {"organization_id": organization_id, "clip_ids": _ids(changed), "now": now},
        )
        return {_plain(row.clip_id): _plain(row.node_id) for row in orphaned}

    async def mark_expired(
        self, transaction: Transaction, clip_ids: Sequence[uuid.UUID], now: datetime
    ) -> dict[uuid.UUID, uuid.UUID]:
        """``issued → expired`` de las que seguían ``issued`` y vencidas; ``{clip_id: node_id}``."""
        if not clip_ids:
            return {}
        result = await transaction.execute(
            _TO_EXPIRED,
            {
                "organization_id": transaction.context.organization_id,
                "clip_ids": _ids(clip_ids),
                "now": now,
            },
        )
        return {_plain(row.clip_id): _plain(row.node_id) for row in result}

    async def node_clip_counts(
        self, transaction: Transaction, *, since: datetime, until: datetime
    ) -> tuple[NodeClipCounts, ...]:
        """Por nodo: huérfanos y clips ``evidence`` del intervalo ``[since, until)``."""
        result = await transaction.execute(
            _NODE_COUNTS,
            {
                "organization_id": transaction.context.organization_id,
                "since": since,
                "until": until,
            },
        )
        return tuple(
            NodeClipCounts(_plain(row.node_id), int(row.orphan_clips), int(row.day_clips))
            for row in result
        )
