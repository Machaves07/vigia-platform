"""``fleet.heartbeat_history`` sobre PostgreSQL (TASK-223; gob_0018; DE §3.8; BR-GOB-71 y 72).

La historia está particionada por mes de ``received_at``: la unicidad de ``heartbeat_id`` no puede
ser un índice único entre particiones (nota de gob_0018). La detección del duplicado es:

1. el **candado de la fila del nodo** en ``fleet.node_inventory`` (``inventory_projection``), que
   serializa los latidos del mismo nodo en cualquier instancia;
2. con el candado tomado, ``seen``: la búsqueda por ``(organization_id, plant_id, node_id,
   heartbeat_id)`` (índice ``heartbeat_history_dedup``) dentro de la retención de 90 días, que
   poda las particiones por ``received_at``. Devuelve el ``received_at`` del original: es el
   ``server_time`` de su respuesta, y el duplicado lo repite (BR-CTR-26, BR-GOB-71; VIG-182).

Sin estado en memoria: dos instancias detrás del balanceador deciden igual (NFR-GOB-15, 47). Solo
``INSERT`` y ``SELECT``: la tabla es de solo anexar (⛓).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.fleet.domain.heartbeat import HistoryRow
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresHeartbeatHistoryStore"]

_SEEN: Final = text(
    "SELECT received_at FROM fleet.heartbeat_history"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
    " AND heartbeat_id = :heartbeat_id AND received_at >= :since LIMIT 1"
)
_APPEND: Final = text(
    "INSERT INTO fleet.heartbeat_history (heartbeat_id, organization_id, plant_id, node_id,"
    " received_at, sent_at, payload_summary)"
    " VALUES (:heartbeat_id, :organization_id, :plant_id, :node_id, :received_at, :sent_at,"
    " CAST(:payload_summary AS jsonb))"
)


@repository
class PostgresHeartbeatHistoryStore:
    """Búsqueda del duplicado y anexo de la historia de latidos de un nodo."""

    async def seen(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        heartbeat_id: uuid.UUID,
        since: datetime,
    ) -> datetime | None:
        """El ``received_at`` del latido ``heartbeat_id`` de este nodo aceptado desde ``since``, o
        ``None`` si no se aceptó. Con el nodo bloqueado."""
        result = await transaction.execute(
            _SEEN,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": node_id,
                "heartbeat_id": heartbeat_id,
                "since": since,
            },
        )
        row = result.first()
        if row is None:
            return None
        received_at: datetime = row[0]
        return received_at

    async def append(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        row: HistoryRow,
    ) -> None:
        """Anexa la fila del latido aceptado."""
        await transaction.execute(
            _APPEND,
            {
                "heartbeat_id": row.heartbeat_id,
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": node_id,
                "received_at": row.received_at,
                "sent_at": row.sent_at,
                "payload_summary": json.dumps(
                    dict(row.payload_summary), allow_nan=False, sort_keys=True
                ),
            },
        )
