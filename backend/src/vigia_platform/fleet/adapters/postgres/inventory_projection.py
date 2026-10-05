"""Proyecciones del latido sobre PostgreSQL: ``NodeInventory``, ``CameraInventory`` y
``ZoneNodeState`` (TASK-223; gob_0018; DE §3.5 a §3.7), y lo que la respuesta lee de ``fleet``.

**Candado del nodo** (``lock``): ``SELECT … FOR UPDATE`` de la fila de ``fleet.node_inventory`` del
nodo, que serializa los latidos del mismo nodo en cualquier instancia (NFR-GOB-15, 47). El primer
latido de un nodo no tiene fila que bloquear: ``insert_first`` la inserta con ``ON CONFLICT DO
NOTHING``, y un segundo primer latido concurrente espera en ese ``INSERT`` a que el primero
confirme (la fila ya existe: no inserta y bloquea la fila con ``lock``) o revierta (inserta él).
Sin fila no hubo ningún latido aceptado de ese nodo (las filas del inventario nunca se borran), así
que el primer latido nunca es un duplicado.

Las proyecciones son 🔒: se insertan o se actualizan, nunca se borran (``vigia_app`` no tiene
``DELETE``); una cámara o una zona que deja de aparecer conserva su última fila. Cada sentencia
nombra la organización de la transacción (defensa en profundidad sobre la RLS) y las cámaras y
zonas se escriben en una sola sentencia, en orden de clave.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.fleet.domain.communication_state import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    CommunicationState,
)
from vigia_platform.fleet.domain.heartbeat import (
    CameraRow,
    InventoryRow,
    PreviousInventory,
    ZoneRow,
)
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresInventoryProjection"]

_LOCK: Final = text(
    "SELECT model_version, last_heartbeat_at, communication_state FROM fleet.node_inventory"
    " WHERE organization_id = :organization_id AND node_id = :node_id FOR UPDATE"
)
_INSERT_FIRST: Final = text(
    "INSERT INTO fleet.node_inventory (node_id, organization_id, plant_id, software_version,"
    " contract_version, model_version, contract_notice, last_heartbeat_at, communication_state,"
    " local_queue, clock, signal_reader, uptime_seconds, updated_at)"
    " VALUES (:node_id, :organization_id, :plant_id, :software_version, :contract_version,"
    " :model_version, CAST(:contract_notice AS jsonb), :last_heartbeat_at, :communication_state,"
    " CAST(:local_queue AS jsonb), CAST(:clock AS jsonb), CAST(:signal_reader AS jsonb),"
    " :uptime_seconds, :updated_at)"
    " ON CONFLICT (node_id) DO NOTHING RETURNING node_id"
)
_UPDATE: Final = text(
    "UPDATE fleet.node_inventory SET software_version = :software_version,"
    " contract_version = :contract_version, model_version = :model_version,"
    " contract_notice = CAST(:contract_notice AS jsonb), last_heartbeat_at = :last_heartbeat_at,"
    " communication_state = :communication_state, local_queue = CAST(:local_queue AS jsonb),"
    " clock = CAST(:clock AS jsonb), signal_reader = CAST(:signal_reader AS jsonb),"
    " uptime_seconds = :uptime_seconds, updated_at = :updated_at"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
)
_CAMERAS: Final = text(
    "INSERT INTO fleet.camera_inventory (organization_id, plant_id, node_id, camera_id,"
    " connected, measured_fps, declared_min_fps, observability_state, updated_at)"
    " SELECT :organization_id, :plant_id, :node_id, c.camera_id, c.connected, c.measured_fps,"
    " c.declared_min_fps, c.observability_state, :updated_at"
    " FROM unnest(CAST(:camera_ids AS uuid[]), CAST(:connected AS boolean[]),"
    " CAST(:measured_fps AS double precision[]), CAST(:declared_min_fps AS double precision[]),"
    " CAST(:observability_states AS text[]))"
    " AS c(camera_id, connected, measured_fps, declared_min_fps, observability_state)"
    " ON CONFLICT (node_id, camera_id) DO UPDATE SET connected = EXCLUDED.connected,"
    " measured_fps = EXCLUDED.measured_fps, declared_min_fps = EXCLUDED.declared_min_fps,"
    " observability_state = EXCLUDED.observability_state, updated_at = EXCLUDED.updated_at"
    " WHERE fleet.camera_inventory.organization_id = EXCLUDED.organization_id"
)
_ZONES: Final = text(
    "INSERT INTO fleet.zone_node_state (organization_id, plant_id, node_id, zone_id, mode,"
    " observability_state, catalog_version_in_node, gate_state_valid_until, open_episodes,"
    " coverage_ok, updated_at)"
    " SELECT :organization_id, :plant_id, :node_id, z.zone_id, z.mode, z.observability_state,"
    " z.catalog_version_in_node, z.gate_state_valid_until, z.open_episodes, z.coverage_ok,"
    " :updated_at"
    " FROM unnest(CAST(:zone_ids AS uuid[]), CAST(:modes AS text[]),"
    " CAST(:observability_states AS text[]), CAST(:catalog_versions AS integer[]),"
    " CAST(:gate_state_valid_until AS timestamptz[]), CAST(:open_episodes AS integer[]),"
    " CAST(:coverage_ok AS boolean[]))"
    " AS z(zone_id, mode, observability_state, catalog_version_in_node, gate_state_valid_until,"
    " open_episodes, coverage_ok)"
    " ON CONFLICT (node_id, zone_id) DO UPDATE SET mode = EXCLUDED.mode,"
    " observability_state = EXCLUDED.observability_state,"
    " catalog_version_in_node = EXCLUDED.catalog_version_in_node,"
    " gate_state_valid_until = EXCLUDED.gate_state_valid_until,"
    " open_episodes = EXCLUDED.open_episodes, coverage_ok = EXCLUDED.coverage_ok,"
    " updated_at = EXCLUDED.updated_at"
    " WHERE fleet.zone_node_state.organization_id = EXCLUDED.organization_id"
)
_INTERVAL: Final = text(
    "SELECT heartbeat_interval_seconds FROM fleet.node_configuration"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
)
_TARGET_VERSION: Final = text(
    "SELECT target_version FROM fleet.target_version_publication"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND CAST(:node_id AS uuid) = ANY(node_ids)"
    " ORDER BY published_at DESC, publication_id DESC LIMIT 1"
)


def _dumps(value: Any) -> str:
    return json.dumps(value, allow_nan=False, sort_keys=True, ensure_ascii=False)


def _inventory_parameters(row: InventoryRow) -> dict[str, Any]:
    return {
        "node_id": row.node_id,
        "organization_id": row.organization_id,
        "plant_id": row.plant_id,
        "software_version": row.software_version,
        "contract_version": row.contract_version,
        "model_version": row.model_version,
        "contract_notice": _dumps(dict(row.contract_notice)),
        "last_heartbeat_at": row.last_heartbeat_at,
        "communication_state": row.communication_state.value,
        "local_queue": _dumps(dict(row.local_queue)),
        "clock": _dumps(dict(row.clock)),
        "signal_reader": _dumps(dict(row.signal_reader)),
        "uptime_seconds": row.uptime_seconds,
        "updated_at": row.updated_at,
    }


@repository
class PostgresInventoryProjection:
    """La fila del nodo en el inventario, sus cámaras, sus zonas y lo que lee la respuesta."""

    async def lock(self, transaction: Transaction, node_id: uuid.UUID) -> PreviousInventory | None:
        """La fila del nodo, bloqueada hasta el fin de la transacción (``None``: no hay fila)."""
        result = await transaction.execute(
            _LOCK, {"organization_id": transaction.context.organization_id, "node_id": node_id}
        )
        row = result.first()
        if row is None:
            return None
        return PreviousInventory(
            model_version=str(row.model_version),
            last_heartbeat_at=row.last_heartbeat_at,
            communication_state=CommunicationState(row.communication_state),
        )

    async def insert_first(self, transaction: Transaction, row: InventoryRow) -> bool:
        """Inserta la primera fila del nodo; ``False`` si otra transacción ya la confirmó."""
        if row.organization_id != transaction.context.organization_id:
            raise ValueError("la fila es de otra organización que la transacción")
        result = await transaction.execute(_INSERT_FIRST, _inventory_parameters(row))
        return result.first() is not None

    async def update(self, transaction: Transaction, row: InventoryRow) -> None:
        """Actualiza la fila ya bloqueada con el latido aceptado."""
        if row.organization_id != transaction.context.organization_id:
            raise ValueError("la fila es de otra organización que la transacción")
        await transaction.execute(_UPDATE, _inventory_parameters(row))

    async def save_cameras(
        self,
        transaction: Transaction,
        row: InventoryRow,
        cameras: Sequence[CameraRow],
    ) -> None:
        """Inserta o actualiza ``CameraInventory`` de cada cámara del latido (una sentencia)."""
        if not cameras:
            return
        await transaction.execute(
            _CAMERAS,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": row.plant_id,
                "node_id": row.node_id,
                "updated_at": row.updated_at,
                "camera_ids": [str(camera.camera_id) for camera in cameras],
                "connected": [camera.connected for camera in cameras],
                "measured_fps": [camera.measured_fps for camera in cameras],
                "declared_min_fps": [camera.declared_min_fps for camera in cameras],
                "observability_states": [camera.observability_state.value for camera in cameras],
            },
        )

    async def save_zones(
        self,
        transaction: Transaction,
        row: InventoryRow,
        zones: Sequence[ZoneRow],
    ) -> None:
        """Inserta o actualiza ``ZoneNodeState`` de cada zona asignada informada (una sentencia)."""
        if not zones:
            return
        await transaction.execute(
            _ZONES,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": row.plant_id,
                "node_id": row.node_id,
                "updated_at": row.updated_at,
                "zone_ids": [str(zone.zone_id) for zone in zones],
                "modes": [zone.mode.value for zone in zones],
                "observability_states": [zone.observability_state.value for zone in zones],
                "catalog_versions": [zone.catalog_version_in_node for zone in zones],
                "gate_state_valid_until": [zone.gate_state_valid_until for zone in zones],
                "open_episodes": [zone.open_episodes for zone in zones],
                "coverage_ok": [zone.coverage_ok for zone in zones],
            },
        )

    async def heartbeat_interval(self, transaction: Transaction, node_id: uuid.UUID) -> int:
        """``NodeConfiguration.heartbeat_interval_seconds`` del nodo, o el de D-11 (60 s)."""
        result = await transaction.execute(
            _INTERVAL, {"organization_id": transaction.context.organization_id, "node_id": node_id}
        )
        row = result.first()
        return DEFAULT_HEARTBEAT_INTERVAL_SECONDS if row is None else int(row[0])

    async def target_version(
        self, transaction: Transaction, plant_id: uuid.UUID, node_id: uuid.UUID
    ) -> str | None:
        """La versión de la última ``TargetVersionPublication`` que alcanza al nodo (sin ventana,
        D-5), o ``None``."""
        result = await transaction.execute(
            _TARGET_VERSION,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": str(node_id),
            },
        )
        row = result.first()
        return None if row is None else str(row[0])
