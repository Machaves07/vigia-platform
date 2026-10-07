"""Lo que el cierre del acta lee de ``fleet`` (TASK-216, LC-GOB-08 sobre LC-GOB-13 y LC-GOB-15).

Lecturas acotadas a **una zona** o a **un nodo**, siempre con la ``Transaction`` del cierre
(contexto de la persona: RLS por organización y concesión) y nombrando además la organización y
la zona o el nodo en cada sentencia (defensa en profundidad, NFR-GOB-30):

- ``window_clips``: los ``VerificationClip`` de la zona recibidos en la ventana de la sesión, con
  ``issued_at`` y ``storage_key`` de su concesión (tramos 2 y 3b; repeticiones de la latencia);
- ``blur_candidates``: los clips de la zona más recientes cuyo ``blur_check_result`` está sin
  escribir o aprobado (los rechazados ya no cuentan), para la guarda del difuminado;
- ``zone_clip_ids``: cuáles de unos ``evidence_ref`` son clips de verificación de la zona;
- ``record_blur_check``: el cierre ``blur_check_result`` (de nulo a valor, una vez) de **un** clip
  de la zona, bajo el candado de su fila;
- ``cameras_measured``: ``measured_fps`` y ``declared_min_fps`` del último latido aceptado
  (``fleet.camera_inventory``) de las cámaras del nodo.

Ninguna sentencia lee ni lista objetos del almacén: eso lo hace ``ClipObjectStore.heads`` con
``head_object`` (PAT-GOB-REN-05).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "MAX_BLUR_CANDIDATES",
    "MAX_WINDOW_CLIPS",
    "CameraInventoryRow",
    "CommissioningClip",
    "PostgresCommissioningQueries",
]

MAX_WINDOW_CLIPS: Final = 10_000
"""Clips de una ventana que se leen para los tramos `[objetivo propio]`; el recuento es exacto."""
MAX_BLUR_CANDIDATES: Final = 16
"""Clips que la guarda del difuminado consulta con ``head_object`` (``MAX_PARALLEL_HEADS``)."""

_WINDOW_CLIPS: Final = text(
    "SELECT c.clip_id, c.sha256, c.received_at, c.first_served_at, c.blur_check_result,"
    " g.issued_at, g.storage_key FROM fleet.verification_clip AS c"
    " JOIN fleet.clip_upload_grant AS g"
    " ON g.organization_id = c.organization_id AND g.clip_id = c.clip_id"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND c.received_at >= :since AND c.received_at <= :until"
    " ORDER BY c.received_at, c.clip_id LIMIT :limit"
)
_WINDOW_COUNT: Final = text(
    "SELECT count(*) AS clips FROM fleet.verification_clip AS c"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND c.received_at >= :since AND c.received_at <= :until"
)
_BLUR_CANDIDATES: Final = text(
    "SELECT c.clip_id, c.sha256, c.received_at, c.first_served_at, c.blur_check_result,"
    " g.issued_at, g.storage_key FROM fleet.verification_clip AS c"
    " JOIN fleet.clip_upload_grant AS g"
    " ON g.organization_id = c.organization_id AND g.clip_id = c.clip_id"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND (c.blur_check_result IS NULL OR c.blur_check_result ->> 'result' = 'approved')"
    " ORDER BY c.received_at DESC, c.clip_id DESC LIMIT :limit"
)
_ZONE_CLIP_IDS: Final = text(
    "SELECT c.clip_id FROM fleet.verification_clip AS c"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND c.clip_id = ANY(CAST(:clip_ids AS uuid[]))"
)
_RECORD_BLUR: Final = text(
    "UPDATE fleet.verification_clip SET blur_check_result = CAST(:result AS jsonb)"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND clip_id = :clip_id"
    " AND blur_check_result IS NULL"
)
_CAMERAS: Final = text(
    "SELECT ci.camera_id, ci.measured_fps, ci.declared_min_fps FROM fleet.camera_inventory AS ci"
    " WHERE ci.organization_id = :organization_id AND ci.node_id = :node_id"
    " AND ci.camera_id = ANY(CAST(:camera_ids AS uuid[]))"
)


@dataclass(frozen=True, slots=True)
class CommissioningClip:
    """Un ``VerificationClip`` de la zona con las marcas de su concesión."""

    clip_id: uuid.UUID
    sha256: str
    storage_key: str
    issued_at: datetime
    received_at: datetime
    first_served_at: datetime | None
    blur_check_result: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class CameraInventoryRow:
    """La cámara tal como la dejó el último latido aceptado."""

    camera_id: uuid.UUID
    measured_fps: float
    declared_min_fps: float


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _document(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _clip(row: Any) -> CommissioningClip:
    blur = _document(row.blur_check_result)
    return CommissioningClip(
        clip_id=_uuid(row.clip_id),
        sha256=str(row.sha256),
        storage_key=str(row.storage_key),
        issued_at=row.issued_at,
        received_at=row.received_at,
        first_served_at=row.first_served_at,
        blur_check_result=None if blur is None else dict(blur),
    )


def _zone_key(transaction: Transaction, zone_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": transaction.context.organization_id, "zone_id": zone_id}


@repository
class PostgresCommissioningQueries:
    """Clips de verificación e inventario de cámaras que lee y cierra el acta."""

    async def window_clips(
        self, transaction: Transaction, zone_id: uuid.UUID, since: datetime, until: datetime
    ) -> tuple[int, tuple[CommissioningClip, ...]]:
        """El número de clips de la zona recibidos en ``[since, until]`` y los primeros
        ``MAX_WINDOW_CLIPS`` en orden de recepción."""
        window = {**_zone_key(transaction, zone_id), "since": since, "until": until}
        count = (await transaction.execute(_WINDOW_COUNT, window)).one()
        rows = await transaction.execute(_WINDOW_CLIPS, {**window, "limit": MAX_WINDOW_CLIPS})
        return int(count.clips), tuple(_clip(row) for row in rows.all())

    async def blur_candidates(
        self, transaction: Transaction, zone_id: uuid.UUID
    ) -> tuple[CommissioningClip, ...]:
        """Los clips de la zona aún sin comprobar o aprobados, del más reciente al más antiguo."""
        rows = await transaction.execute(
            _BLUR_CANDIDATES, {**_zone_key(transaction, zone_id), "limit": MAX_BLUR_CANDIDATES}
        )
        return tuple(_clip(row) for row in rows.all())

    async def zone_clip_ids(
        self, transaction: Transaction, zone_id: uuid.UUID, clip_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """Los de ``clip_ids`` que son ``VerificationClip`` de la zona."""
        if not clip_ids:
            return frozenset()
        rows = await transaction.execute(
            _ZONE_CLIP_IDS, {**_zone_key(transaction, zone_id), "clip_ids": list(clip_ids)}
        )
        return frozenset(_uuid(row.clip_id) for row in rows.all())

    async def record_blur_check(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        clip_id: uuid.UUID,
        result: Mapping[str, Any],
    ) -> bool:
        """Cierra ``blur_check_result`` del clip si seguía nulo; ``False`` si ya tenía uno."""
        written: Any = await transaction.execute(
            _RECORD_BLUR,
            {
                **_zone_key(transaction, zone_id),
                "clip_id": clip_id,
                "result": json.dumps(dict(result), ensure_ascii=False),
            },
        )
        count: int = written.rowcount
        return count == 1

    async def cameras_measured(
        self, transaction: Transaction, node_id: uuid.UUID, camera_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, CameraInventoryRow]:
        """La fila de inventario de cada cámara del nodo que el último latido dejó."""
        if not camera_ids:
            return {}
        rows = await transaction.execute(
            _CAMERAS,
            {
                "organization_id": transaction.context.organization_id,
                "node_id": node_id,
                "camera_ids": list(camera_ids),
            },
        )
        return {
            _uuid(row.camera_id): CameraInventoryRow(
                camera_id=_uuid(row.camera_id),
                measured_fps=float(row.measured_fps),
                declared_min_fps=float(row.declared_min_fps),
            )
            for row in rows.all()
        }
