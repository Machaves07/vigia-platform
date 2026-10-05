"""Lecturas de ``FleetQueryPort`` por zona sobre PostgreSQL (TASK-224; LC-GOB-23b; PAT-GOB-REN-04).

Una **sola sentencia** por operación, siempre desde ``identity.zone`` (la zona tiene que existir en
la organización del contexto y estar en su alcance: si no, la sentencia no devuelve filas y el
llamador responde ``not_found``):

- ``nodes_by_zone``: los nodos con asignación vigente en la zona, con su estado de comunicación
  (último ``node_communication_state_changed`` del expediente) y las cámaras de su último latido;
- ``assignment_at``: la asignación de ``identity.zone_node_assignment`` cuyo ``[assigned_at,
  unassigned_at)`` contiene el instante, o el hueco en que la zona no tenía nodo;
- ``assignment_history``: las asignaciones que se solapan con ``[from, to]`` (``assigned_at <= to``
  y ``unassigned_at`` nulo o posterior a ``from``), en orden de ``assigned_at``.

Las asignaciones son de U-02 y se leen sobre sus columnas ``assigned_at`` y ``unassigned_at`` con la
exclusión GiST de A-12 y A-32 (a lo sumo un nodo por zona y periodo): U-03 no escribe ni modifica
esa tabla. ``replaces_node_id`` sale de la ficha de flota del nodo asignado.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.fleet.adapters.postgres.inventory_queries import scope_parameters
from vigia_platform.fleet.ports import ZoneAssignmentPeriod, ZoneNode, ZoneNodeCamera
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["PostgresAssignmentQueries"]

# Las tres sentencias repiten la condición de zona visible (organización del contexto y
# ``allowed_scopes``): ``text()`` solo admite literales (VIG001).
_NODES_BY_ZONE: Final = text(
    "SELECT z.zone_id, n.node_id, n.code, n.created_at, v.last_heartbeat_at,"
    " COALESCE(comm.state, v.communication_state, 'unknown') AS communication_state,"
    " COALESCE(comm.since, n.created_at) AS since,"
    " (SELECT COALESCE(json_agg(json_build_object('camera_id', ci.camera_id, 'code', cc.code,"
    " 'connected', ci.connected, 'measured_fps', ci.measured_fps,"
    " 'observability_state', ci.observability_state) ORDER BY ci.camera_id), '[]'::json)::text"
    " FROM fleet.camera_inventory AS ci"
    " LEFT JOIN LATERAL (SELECT e ->> 'code' AS code FROM identity.zone_node_assignment AS ca"
    " JOIN catalog.zone_catalog_version AS zc ON zc.organization_id = ca.organization_id"
    " AND zc.zone_id = ca.zone_id AND zc.superseded_at IS NULL"
    " CROSS JOIN LATERAL jsonb_array_elements(zc.payload -> 'cameras') AS e"
    " WHERE ca.organization_id = :organization_id AND ca.node_id = n.node_id"
    " AND ca.unassigned_at IS NULL AND e ->> 'camera_id' = ci.camera_id::text"
    " ORDER BY ca.zone_id LIMIT 1) AS cc ON true"
    " WHERE ci.organization_id = :organization_id AND ci.node_id = n.node_id"
    " AND ci.updated_at = v.updated_at) AS cameras"
    " FROM identity.zone AS z"
    " LEFT JOIN identity.zone_node_assignment AS a ON a.organization_id = z.organization_id"
    " AND a.zone_id = z.zone_id AND a.unassigned_at IS NULL"
    " LEFT JOIN identity.node_identity AS n"
    " ON n.organization_id = a.organization_id AND n.node_id = a.node_id"
    " LEFT JOIN fleet.node_inventory AS v"
    " ON v.organization_id = n.organization_id AND v.node_id = n.node_id"
    " LEFT JOIN LATERAL (SELECT r.content_json ->> 'state' AS state,"
    " CASE WHEN r.content_json ->> 'state' = 'mute'"
    " AND r.content_json ->> 'last_heartbeat_at' IS NOT NULL"
    " THEN CAST(r.content_json ->> 'last_heartbeat_at' AS timestamptz)"
    " ELSE CAST(r.content_json ->> 'since' AS timestamptz) END AS since"
    " FROM ledger.ledger_record AS r"
    " WHERE r.organization_id = n.organization_id AND r.scope_zone_id IS NULL"
    " AND r.record_type = 'node_communication_state_changed' AND r.scope_node_id = n.node_id"
    " ORDER BY CAST(r.content_json ->> 'since' AS timestamptz) DESC, r.record_id DESC LIMIT 1)"
    " AS comm ON true"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY n.code"
)

_ASSIGNMENT_AT: Final = text(
    "SELECT z.zone_id, cur.node_id, cur.assigned_at, cur.unassigned_at, cur.replaces_node_id,"
    " COALESCE(prev.unassigned_at, LEAST(z.created_at, CAST(:at AS timestamptz))) AS gap_start,"
    " nxt.assigned_at AS gap_end"
    " FROM identity.zone AS z"
    " LEFT JOIN LATERAL (SELECT a.node_id, a.assigned_at, a.unassigned_at, f.replaces_node_id"
    " FROM identity.zone_node_assignment AS a LEFT JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = a.organization_id AND f.node_id = a.node_id"
    " WHERE a.organization_id = z.organization_id AND a.zone_id = z.zone_id"
    " AND a.assigned_at <= CAST(:at AS timestamptz)"
    " AND (a.unassigned_at IS NULL OR a.unassigned_at > CAST(:at AS timestamptz))"
    " ORDER BY a.assigned_at DESC LIMIT 1) AS cur ON true"
    " LEFT JOIN LATERAL (SELECT max(a.unassigned_at) AS unassigned_at"
    " FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = z.organization_id AND a.zone_id = z.zone_id"
    " AND a.unassigned_at <= CAST(:at AS timestamptz)) AS prev ON true"
    " LEFT JOIN LATERAL (SELECT min(a.assigned_at) AS assigned_at"
    " FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = z.organization_id AND a.zone_id = z.zone_id"
    " AND a.assigned_at > CAST(:at AS timestamptz)) AS nxt ON true"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
)

_ASSIGNMENT_HISTORY: Final = text(
    "SELECT z.zone_id, a.node_id, a.assigned_at, a.unassigned_at, f.replaces_node_id"
    " FROM identity.zone AS z"
    " LEFT JOIN identity.zone_node_assignment AS a"
    " ON a.organization_id = z.organization_id AND a.zone_id = z.zone_id"
    " AND a.assigned_at <= CAST(:end AS timestamptz)"
    " AND (a.unassigned_at IS NULL OR a.unassigned_at > CAST(:start AS timestamptz))"
    " LEFT JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = a.organization_id AND f.node_id = a.node_id"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR z.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR z.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY a.assigned_at, a.assignment_id"
)


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _parameters(context: ScopeContext, zone_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "organization_id": context.organization_id,
        "zone_id": zone_id,
        **scope_parameters(context),
        **extra,
    }


@repository
class PostgresAssignmentQueries:
    """Las operaciones por zona de ``FleetQueryPort``: cada una, una lectura ``READ ONLY`` de una
    sentencia; ``None``: zona inexistente, de otra organización o fuera de alcance."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def nodes_by_zone(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> tuple[ZoneNode, ...] | None:
        rows = await self._database.read(context, _NODES_BY_ZONE, _parameters(context, zone_id))
        if not rows:
            return None
        return tuple(
            ZoneNode(
                node_id=_uuid(row.node_id),
                code=str(row.code),
                communication_state=CommunicationState(row.communication_state),
                since=row.since,
                last_heartbeat_at=row.last_heartbeat_at,
                cameras=tuple(
                    ZoneNodeCamera(
                        camera_id=_uuid(item["camera_id"]),
                        code=item["code"],
                        connected=bool(item["connected"]),
                        measured_fps=float(item["measured_fps"]),
                        observability_state=str(item["observability_state"]),
                    )
                    for item in json.loads(row.cameras)
                ),
            )
            for row in rows
            if row.node_id is not None
        )

    async def assignment_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneAssignmentPeriod | None:
        rows = await self._database.read(
            context, _ASSIGNMENT_AT, _parameters(context, zone_id, at=at)
        )
        if not rows:
            return None
        (row,) = rows
        if row.node_id is None:
            return ZoneAssignmentPeriod(
                node_id=None,
                assigned_at=row.gap_start,
                unassigned_at=row.gap_end,
                replaces_node_id=None,
            )
        return ZoneAssignmentPeriod(
            node_id=_uuid(row.node_id),
            assigned_at=row.assigned_at,
            unassigned_at=row.unassigned_at,
            replaces_node_id=_optional_uuid(row.replaces_node_id),
        )

    async def assignment_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[ZoneAssignmentPeriod, ...] | None:
        rows = await self._database.read(
            context, _ASSIGNMENT_HISTORY, _parameters(context, zone_id, start=start, end=end)
        )
        if not rows:
            return None
        return tuple(
            ZoneAssignmentPeriod(
                node_id=_uuid(row.node_id),
                assigned_at=row.assigned_at,
                unassigned_at=row.unassigned_at,
                replaces_node_id=_optional_uuid(row.replaces_node_id),
            )
            for row in rows
            if row.node_id is not None
        )
