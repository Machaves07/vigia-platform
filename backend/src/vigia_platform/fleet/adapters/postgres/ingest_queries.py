"""Consultas de la ingesta sobre PostgreSQL (TASK-221, LC-GOB-12): una consulta por verificación.

Toda sentencia va con el ``ScopeContext`` del nodo (la seguridad a nivel de fila filtra por su
organización) y nombra **explícitos** la organización y, cuando aplica, la planta, la zona o el nodo
(defensa en profundidad, NFR-GOB-30). Solo traen las filas candidatas; la decisión es del dominio
puro (``fleet.domain.ingest_order``):

- ``assignments``: las asignaciones de la zona al nodo vigentes en el instante (paso 2; índice
  ``zone_node_assignment_node``, la misma lógica que ``FleetQueryPort.assignment_at`` de TASK-224,
  BR-GOB-88), de solo lectura y fuera de la transacción;
- ``zone_in_organization``: si la zona es de la organización del certificado (``zone_id`` de
  ``ingest_rejected`` y de la auditoría, BR-GOB-96);
- ``accepted``: el registro ya aceptado con esa clave (paso 5), con su contenido;
- ``retention_days``: ``sent_records_retention_days`` de ``fleet.node_configuration``;
- ``catalog_versions``: las versiones del catálogo de la zona vigentes en la ventana
  (``[issued_at, superseded_at)``, paso 7);
- ``usage_spans``: los intervalos de la compuerta de uso que se cruzan con la ventana (paso 8);
- ``event_accepted``: si la apertura de un cierre ya está aceptada (cierre huérfano);
- ``mark_orphan_close`` y ``mark_cited``: las dos proyecciones que se escriben con el registro.

Las de los pasos 7 y 8 y las proyecciones van en la transacción de la escritura (``projection`` del
escritor). SQL sin valores en trazas ni registros (NFR-GOB-57).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.fleet.adapters.postgres.clip_grant_store import PostgresClipGrants
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.ingest_order import (
    AssignmentSpan,
    CatalogVersionView,
    GateSpan,
    IngestKind,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["AcceptedRecord", "PostgresIngestStore"]

_ASSIGNMENTS: Final = text(
    "SELECT a.zone_id, a.assigned_at, a.unassigned_at FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = :organization_id AND a.node_id = :node_id"
    " AND a.zone_id = :zone_id AND a.assigned_at <= :at"
    " AND (a.unassigned_at IS NULL OR a.unassigned_at > :at)"
)
_ZONE: Final = text(
    "SELECT z.zone_id FROM identity.zone AS z"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)
_ACCEPTED: Final = text(
    "SELECT r.record_id, r.received_at, r.content FROM ledger.record_source_key AS k"
    " JOIN ledger.ledger_record AS r"
    " ON r.record_id = k.record_id AND r.received_at = k.received_at"
    " AND r.organization_id = k.organization_id"
    " WHERE k.organization_id = :organization_id AND k.record_type = :record_type"
    " AND k.source_key = :source_key"
)
_RETENTION: Final = text(
    "SELECT c.sent_records_retention_days FROM fleet.node_configuration AS c"
    " WHERE c.organization_id = :organization_id AND c.node_id = :node_id"
)
_CATALOG_VERSIONS: Final = text(
    "SELECT v.catalog_version, v.issued_at, v.superseded_at, v.payload"
    " FROM catalog.zone_catalog_version AS v"
    " WHERE v.organization_id = :organization_id AND v.zone_id = :zone_id"
    " AND v.issued_at <= :window_end"
    " AND (v.superseded_at IS NULL OR v.superseded_at > :window_start)"
    " ORDER BY v.catalog_version DESC"
)
_USAGE_SPANS: Final = text(
    "SELECT h.status, h.effective_from, h.effective_until FROM catalog.gate_state_history AS h"
    " WHERE h.organization_id = :organization_id AND h.zone_id = :zone_id AND h.gate = 'usage'"
    " AND h.effective && tstzrange(CAST(:window_start AS timestamptz),"
    " CAST(:window_end AS timestamptz), '[]')"
)
_EVENT_ACCEPTED: Final = text(
    "SELECT k.record_id FROM ledger.record_source_key AS k"
    " WHERE k.organization_id = :organization_id AND k.record_type = :record_type"
    " AND k.source_key = :source_key"
)
_ORPHAN_CLOSE: Final = text(
    "INSERT INTO fleet.observability_orphan_close (event_id, organization_id, plant_id, zone_id,"
    " node_id, opened_event_id, ledger_record_id, received_at)"
    " VALUES (:event_id, :organization_id, :plant_id, :zone_id, :node_id, :opened_event_id,"
    " :ledger_record_id, :received_at)"
    " ON CONFLICT (event_id) DO NOTHING"
)


@dataclass(frozen=True, slots=True)
class AcceptedRecord:
    """Un registro de la ingesta ya aceptado: su identificador y su contenido (con ``receipt``)."""

    record_id: uuid.UUID
    received_at: datetime
    content: dict[str, Any]


def _plain(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _json(value: object) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    return json.loads(value) if isinstance(value, str | bytes) else value


@repository
class PostgresIngestStore:
    """Las consultas y las proyecciones de la ingesta."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database
        self._grants = PostgresClipGrants(database)

    # --- Fuera de la transacción (pasos 2 y 5) -----------------------------------------------

    async def assignments(
        self, context: ScopeContext, node_id: uuid.UUID, zone_id: uuid.UUID, at: datetime
    ) -> tuple[AssignmentSpan, ...]:
        rows = await self._database.read(
            context,
            _ASSIGNMENTS,
            {
                "organization_id": context.organization_id,
                "node_id": node_id,
                "zone_id": zone_id,
                "at": at,
            },
        )
        return tuple(
            AssignmentSpan(_plain(row.zone_id), row.assigned_at, row.unassigned_at) for row in rows
        )

    async def zone_in_organization(self, context: ScopeContext, zone_id: uuid.UUID) -> bool:
        rows = await self._database.read(
            context, _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        return bool(rows)

    async def accepted(
        self, context: ScopeContext, kind: IngestKind, source_key: str
    ) -> AcceptedRecord | None:
        rows = await self._database.read(
            context,
            _ACCEPTED,
            {
                "organization_id": context.organization_id,
                "record_type": kind.record_type,
                "source_key": source_key,
            },
        )
        if not rows:
            return None
        row = rows[0]
        return AcceptedRecord(_plain(row.record_id), row.received_at, _json(row.content))

    # --- En la transacción de la escritura (pasos 7 y 8 y proyecciones) -----------------------

    async def retention_days(self, transaction: Transaction, node_id: uuid.UUID) -> int | None:
        row = (
            await transaction.execute(
                _RETENTION,
                {"organization_id": transaction.context.organization_id, "node_id": node_id},
            )
        ).first()
        return None if row is None else int(row.sent_records_retention_days)

    async def catalog_versions(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> tuple[CatalogVersionView, ...]:
        rows = (
            await transaction.execute(
                _CATALOG_VERSIONS,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "window_start": window.start,
                    "window_end": window.end,
                },
            )
        ).all()
        return tuple(
            CatalogVersionView.from_payload(
                int(row.catalog_version), row.issued_at, row.superseded_at, _json(row.payload)
            )
            for row in rows
        )

    async def usage_spans(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> tuple[GateSpan, ...]:
        rows = (
            await transaction.execute(
                _USAGE_SPANS,
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

    async def event_accepted(self, transaction: Transaction, event_id: str) -> bool:
        row = (
            await transaction.execute(
                _EVENT_ACCEPTED,
                {
                    "organization_id": transaction.context.organization_id,
                    "record_type": IngestKind.OBSERVABILITY_EVENT.record_type,
                    "source_key": event_id,
                },
            )
        ).first()
        return row is not None

    async def mark_orphan_close(
        self,
        transaction: Transaction,
        *,
        event_id: uuid.UUID,
        opened_event_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        ledger_record_id: uuid.UUID,
        received_at: datetime,
    ) -> None:
        await transaction.execute(
            _ORPHAN_CLOSE,
            {
                "event_id": event_id,
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "zone_id": zone_id,
                "node_id": node_id,
                "opened_event_id": opened_event_id,
                "ledger_record_id": ledger_record_id,
                "received_at": received_at,
            },
        )

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
        return await self._grants.mark_cited(
            transaction,
            plant_id=plant_id,
            zone_id=zone_id,
            node_id=node_id,
            clip_ids=clip_ids,
            now=now,
        )
