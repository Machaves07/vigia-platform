"""Lecturas del inventario de flota sobre PostgreSQL (TASK-224; LC-GOB-15; PAT-GOB-ESC-01).

``inventory``: **una sola sentencia** por página de ``GET /fleet/nodes`` (y por nodo en el detalle y
en ``FleetQueryPort.node``). Lee, sin escribir nada:

- ``identity.node_identity`` (código, planta, estado) y ``identity.zone_node_assignment`` (zonas
  vigentes) de U-02;
- la ficha ``fleet.node_fleet_record`` (baja, reemplazo, URL local) y las proyecciones del latido
  (``node_inventory``, ``camera_inventory``, ``zone_node_state``, TASK-223); de cámaras y zonas,
  **solo las filas del último latido aceptado** (``updated_at`` igual al del inventario): una
  cámara retirada conserva su fila (nada se borra) pero ya no se muestra ni dispara avisos;
- el estado de comunicación y su ``since`` del último ``node_communication_state_changed`` del
  expediente: lo escriben la declaración, la vuelta a ``reachable`` y la tarea de mudos, nunca un
  nodo revocado (BR-GOB-76), así que un latido que llegó durante la revocación no lo cambia;
- para los avisos: los umbrales vigentes de la planta (``plant_fleet_thresholds`` o los valores por
  defecto), el intervalo efectivo (``node_configuration``), la credencial vigente de vencimiento más
  lejano, los clips ``evidence`` huérfanos y emitidos en las últimas 24 h y el
  ``resulting_mode`` de las zonas asignadas (``catalog.zone_gate_state``).

Los **avisos se calculan en la misma consulta** (las ocho condiciones de
``fleet.domain.fleet_warnings``, con sus constantes como parámetros), sin materializarse: el filtro
por aviso y por estado de comunicación pagina sin huecos. ``fleet_warnings.evaluate`` es el
evaluador de referencia con el que PR-GOB-27 los compara fila a fila.

**Alcance.** Toda sentencia nombra la organización del contexto (además de la RLS) y filtra por
``allowed_scopes``: con alcance de organización, todos sus nodos; de planta, los nodos de esa
planta; de zona, los nodos que atienden esa zona ahora, con ``zones`` y ``zone_states`` reducidas
a las zonas visibles, ``cameras`` reducidas a las que declara el catálogo vigente de una zona
visible (con el ``code`` de esa zona) y los avisos que dependen de ellas
(``camera_below_min_fps`` y ``simulated_adapter_in_productive``) calculados solo sobre lo visible
(seguimiento de VIG-159; VIG-165).

``heartbeat_history``: la historia del nodo (90 días, ``payload_summary``, cursor por
``(received_at, heartbeat_id)`` descendente). ``thresholds`` y ``save_thresholds``: la fila de la
planta (``INSERT … ON CONFLICT``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.fleet.domain.communication_state import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    MUTE_FACTOR,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_thresholds import (
    DEFAULT_CLOCK_DRIFT_THRESHOLD_MS,
    DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES,
    DEFAULT_QUEUE_PENDING_THRESHOLD,
    FleetThresholds,
)
from vigia_platform.fleet.domain.fleet_warnings import (
    CERTIFICATE_ALERT_BEFORE,
    ORPHAN_CLIPS_MAX,
    ORPHAN_CLIPS_PERCENT,
    ORPHAN_CLIPS_WINDOW,
)
from vigia_platform.fleet.ports import CameraState, NodeInventory, ZoneState
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.context import ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "HISTORY_RETENTION",
    "HeartbeatCursor",
    "HeartbeatEntry",
    "InventoryFilters",
    "PostgresInventoryQueries",
    "inventory_parameters",
    "scope_parameters",
]

HISTORY_RETENTION: Final = timedelta(days=90)
"""``HeartbeatHistory`` en línea 90 días `[estimación propia]` (BR-GOB-72)."""

# La consulta del inventario. ``base`` lee cada nodo visible con sus laterales (una fila por nodo)
# y calcula ``warnings``; la sentencia exterior filtra por estado y aviso, ordena por ``code``
# (único en la organización: el cursor) y agrega zonas, cámaras y estados de zona del último
# latido. Los parámetros nulos desactivan su filtro. Escrita entera (``text()`` solo admite
# literales, VIG001).
_INVENTORY: Final = text(
    "WITH base AS ("
    " SELECT n.node_id, n.plant_id, n.code, n.status, n.created_at,"
    " f.decommissioned_at, f.replaces_node_id, f.live_view_local_url,"
    " v.software_version, v.contract_version, v.model_version,"
    " v.contract_notice::text AS contract_notice, v.last_heartbeat_at,"
    " v.local_queue::text AS local_queue, v.clock::text AS clock,"
    " v.signal_reader::text AS signal_reader,"
    # Versión objetivo y último resultado (TASK-226): la proyección del inventario o, si el nodo
    # aún no tiene fila (nunca envió latido), lo último que consta para él.
    " COALESCE(v.target_version, (SELECT p.target_version"
    " FROM fleet.target_version_publication AS p WHERE p.organization_id = n.organization_id"
    " AND p.plant_id = n.plant_id AND n.node_id = ANY(p.node_ids)"
    " ORDER BY p.published_at DESC, p.publication_id DESC LIMIT 1)) AS target_version,"
    " COALESCE(v.last_update_result, (SELECT u.result FROM fleet.update_result AS u"
    " WHERE u.organization_id = n.organization_id AND u.plant_id = n.plant_id"
    " AND u.node_id = n.node_id ORDER BY u.reported_at DESC, u.update_result_id DESC LIMIT 1))"
    " AS last_update_result,"
    " v.updated_at AS inventory_updated_at,"
    " COALESCE(comm.state, v.communication_state, 'unknown') AS communication_state,"
    " COALESCE(comm.since, n.created_at) AS since,"
    " CASE WHEN n.status = 'revoked' OR f.decommissioned_at IS NOT NULL THEN ARRAY[]::text[]"
    " ELSE array_remove(ARRAY["
    # node_mute: now - last_heartbeat_at > 5 veces el intervalo efectivo (BR-GOB-74).
    " CASE WHEN v.last_heartbeat_at IS NOT NULL AND CAST(:now AS timestamptz)"
    " - v.last_heartbeat_at > interval '1 second' * (CAST(:mute_factor AS integer)"
    " * COALESCE(cfg.heartbeat_interval_seconds, CAST(:default_interval AS integer)))"
    " THEN 'node_mute' END,"
    # queue_over_threshold: pendientes o antigüedad del más viejo (BR-GOB-79).
    " CASE WHEN CAST(v.local_queue ->> 'pending' AS bigint)"
    " > COALESCE(t.queue_pending_threshold, CAST(:default_pending AS integer))"
    " OR (v.local_queue ->> 'oldest_pending_at' IS NOT NULL AND CAST(:now AS timestamptz)"
    " - CAST(v.local_queue ->> 'oldest_pending_at' AS timestamptz) > interval '1 minute'"
    " * COALESCE(t.queue_age_threshold_minutes, CAST(:default_age AS integer)))"
    " THEN 'queue_over_threshold' END,"
    # clock_drift: |offset_ms| > umbral (BR-GOB-79).
    " CASE WHEN abs(CAST(v.clock ->> 'offset_ms' AS bigint))"
    " > COALESCE(t.clock_drift_threshold_ms, CAST(:default_drift AS integer))"
    " THEN 'clock_drift' END,"
    # version_retiring: la fecha de retiro que entrega el contrato (BR-GOB-80).
    " CASE WHEN v.contract_notice ->> 'retires_at' IS NOT NULL THEN 'version_retiring' END,"
    # simulated_adapter_in_productive: con el modo de la compuerta de la plataforma (BR-GOB-78).
    " CASE WHEN v.signal_reader ->> 'adapter' IN ('simulated', 'file') AND gate.productive"
    " THEN 'simulated_adapter_in_productive' END,"
    # certificate_expiring: la credencial vigente vence en 15 días o menos (BR-GOB-65).
    " CASE WHEN cred.expires_at <= CAST(:certificate_until AS timestamptz)"
    " THEN 'certificate_expiring' END,"
    # camera_below_min_fps: alguna cámara del último latido bajo su mínimo (BR-GOB-49).
    " CASE WHEN cam.below THEN 'camera_below_min_fps' END,"
    # orphan_clips_growing: > 50 huérfanos o > 5 % de los clips del día (BR-GOB-94).
    " CASE WHEN clips.orphan_clips > CAST(:orphan_max AS integer)"
    " OR clips.orphan_clips * 100 > CAST(:orphan_percent AS integer) * clips.day_clips"
    " THEN 'orphan_clips_growing' END"
    " ], NULL) END AS warnings"
    " FROM identity.node_identity AS n"
    " LEFT JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = n.organization_id AND f.node_id = n.node_id"
    " LEFT JOIN fleet.node_inventory AS v"
    " ON v.organization_id = n.organization_id AND v.node_id = n.node_id"
    " LEFT JOIN fleet.node_configuration AS cfg"
    " ON cfg.organization_id = n.organization_id AND cfg.node_id = n.node_id"
    " LEFT JOIN fleet.plant_fleet_thresholds AS t"
    " ON t.organization_id = n.organization_id AND t.plant_id = n.plant_id"
    # El último cambio de estado del nodo; ``mute`` empieza en su ``last_heartbeat_at`` (BR-GOB-74),
    # como en la cobertura de U-02.
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
    " LEFT JOIN LATERAL (SELECT max(c.expires_at) AS expires_at FROM fleet.node_credential AS c"
    " WHERE c.organization_id = n.organization_id AND c.plant_id = n.plant_id"
    " AND c.node_id = n.node_id AND c.status IN ('active', 'overlapping')) AS cred ON true"
    # Con alcance de zona, solo las cámaras que declara el catálogo vigente de una zona visible del
    # nodo y solo el modo de esas zonas (seguimiento de VIG-159): nada de las zonas hermanas.
    " LEFT JOIN LATERAL (SELECT bool_or(ci.measured_fps < ci.declared_min_fps) AS below"
    " FROM fleet.camera_inventory AS ci WHERE ci.organization_id = n.organization_id"
    " AND ci.node_id = n.node_id AND ci.updated_at = v.updated_at"
    " AND (CAST(:whole_organization AS boolean)"
    " OR n.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone_node_assignment AS va"
    " JOIN catalog.zone_catalog_version AS vc ON vc.organization_id = va.organization_id"
    " AND vc.zone_id = va.zone_id AND vc.superseded_at IS NULL"
    " CROSS JOIN LATERAL jsonb_array_elements(vc.payload -> 'cameras') AS ve"
    " WHERE va.organization_id = n.organization_id AND va.node_id = n.node_id"
    " AND va.unassigned_at IS NULL AND va.zone_id = ANY(CAST(:scope_zones AS uuid[]))"
    " AND ve ->> 'camera_id' = ci.camera_id::text))) AS cam ON true"
    " LEFT JOIN LATERAL (SELECT bool_or(gs.resulting_mode = 'productive') AS productive"
    " FROM identity.zone_node_assignment AS a JOIN catalog.zone_gate_state AS gs"
    " ON gs.organization_id = a.organization_id AND gs.zone_id = a.zone_id"
    " WHERE a.organization_id = n.organization_id AND a.node_id = n.node_id"
    " AND a.unassigned_at IS NULL AND (CAST(:whole_organization AS boolean)"
    " OR n.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR a.zone_id = ANY(CAST(:scope_zones AS uuid[])))) AS gate ON true"
    " LEFT JOIN LATERAL (SELECT"
    " count(*) FILTER (WHERE g.status = 'orphan' AND g.orphaned_at >= CAST(:clips_since AS"
    " timestamptz) AND g.orphaned_at < CAST(:now AS timestamptz)) AS orphan_clips,"
    " count(*) FILTER (WHERE g.issued_at >= CAST(:clips_since AS timestamptz)"
    " AND g.issued_at < CAST(:now AS timestamptz)) AS day_clips"
    " FROM fleet.clip_upload_grant AS g"
    " WHERE g.organization_id = n.organization_id AND g.plant_id = n.plant_id"
    " AND g.node_id = n.node_id AND g.purpose = 'evidence'"
    " AND ((g.status = 'orphan' AND g.orphaned_at >= CAST(:clips_since AS timestamptz)"
    " AND g.orphaned_at < CAST(:now AS timestamptz))"
    " OR (g.issued_at >= CAST(:clips_since AS timestamptz)"
    " AND g.issued_at < CAST(:now AS timestamptz)))) AS clips ON true"
    " WHERE n.organization_id = :organization_id"
    " AND (CAST(:whole_organization AS boolean)"
    " OR n.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR EXISTS (SELECT 1 FROM identity.zone_node_assignment AS sa"
    " WHERE sa.organization_id = n.organization_id AND sa.node_id = n.node_id"
    " AND sa.unassigned_at IS NULL AND sa.zone_id = ANY(CAST(:scope_zones AS uuid[]))))"
    " AND (CAST(:node_id AS uuid) IS NULL OR n.node_id = CAST(:node_id AS uuid))"
    " AND (CAST(:plant_id AS uuid) IS NULL OR n.plant_id = CAST(:plant_id AS uuid))"
    " AND (CAST(:after AS text) IS NULL OR n.code > CAST(:after AS text)))"
    " SELECT b.*,"
    " (SELECT COALESCE(array_agg(a.zone_id ORDER BY a.zone_id), ARRAY[]::uuid[])"
    " FROM identity.zone_node_assignment AS a"
    " WHERE a.organization_id = :organization_id AND a.node_id = b.node_id"
    " AND a.unassigned_at IS NULL AND (CAST(:whole_organization AS boolean)"
    " OR b.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR a.zone_id = ANY(CAST(:scope_zones AS uuid[])))) AS zones,"
    " (SELECT COALESCE(json_agg(json_build_object('camera_id', ci.camera_id, 'code', cc.code,"
    " 'connected', ci.connected, 'measured_fps', ci.measured_fps,"
    " 'declared_min_fps', ci.declared_min_fps, 'observability_state', ci.observability_state)"
    " ORDER BY ci.camera_id), '[]'::json)::text"
    " FROM fleet.camera_inventory AS ci"
    # Las cámaras declaradas por el catálogo vigente de las zonas visibles del nodo, con el
    # ``code`` de la primera zona: **una** lectura por nodo, no una por cámara (VIG-188: bajo la
    # concesión del proveedor cada fila leída paga la comprobación de RLS).
    " LEFT JOIN (SELECT DISTINCT ON (e ->> 'camera_id') e ->> 'camera_id' AS camera_id,"
    " e ->> 'code' AS code"
    " FROM identity.zone_node_assignment AS ca"
    " JOIN catalog.zone_catalog_version AS zc ON zc.organization_id = ca.organization_id"
    " AND zc.zone_id = ca.zone_id AND zc.superseded_at IS NULL"
    " CROSS JOIN LATERAL jsonb_array_elements(zc.payload -> 'cameras') AS e"
    " WHERE ca.organization_id = :organization_id AND ca.node_id = b.node_id"
    " AND ca.unassigned_at IS NULL"
    " AND (CAST(:whole_organization AS boolean)"
    " OR b.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR ca.zone_id = ANY(CAST(:scope_zones AS uuid[])))"
    " ORDER BY e ->> 'camera_id', ca.zone_id) AS cc ON cc.camera_id = ci.camera_id::text"
    " WHERE ci.organization_id = :organization_id AND ci.node_id = b.node_id"
    " AND ci.updated_at = b.inventory_updated_at"
    " AND (CAST(:whole_organization AS boolean)"
    " OR b.plant_id = ANY(CAST(:scope_plants AS uuid[])) OR cc.camera_id IS NOT NULL))"
    " AS cameras,"
    " (SELECT COALESCE(json_agg(json_build_object('zone_id', s.zone_id, 'mode', s.mode,"
    " 'observability_state', s.observability_state,"
    " 'catalog_version', s.catalog_version_in_node, 'coverage_ok', s.coverage_ok)"
    " ORDER BY s.zone_id), '[]'::json)::text"
    " FROM fleet.zone_node_state AS s"
    " WHERE s.organization_id = :organization_id AND s.node_id = b.node_id"
    " AND s.updated_at = b.inventory_updated_at AND (CAST(:whole_organization AS boolean)"
    " OR b.plant_id = ANY(CAST(:scope_plants AS uuid[]))"
    " OR s.zone_id = ANY(CAST(:scope_zones AS uuid[])))) AS zone_states"
    " FROM base AS b"
    " WHERE (CAST(:communication_state AS text) IS NULL"
    " OR b.communication_state = CAST(:communication_state AS text))"
    " AND (CAST(:warning AS text) IS NULL OR CAST(:warning AS text) = ANY(b.warnings))"
    " ORDER BY b.code LIMIT :limit"
)

_HISTORY: Final = text(
    "SELECT h.heartbeat_id, h.received_at, h.sent_at, h.payload_summary::text AS payload_summary"
    " FROM fleet.heartbeat_history AS h"
    " WHERE h.organization_id = :organization_id AND h.plant_id = :plant_id"
    " AND h.node_id = :node_id AND h.received_at >= :since"
    " AND (CAST(:after_received_at AS timestamptz) IS NULL"
    " OR (h.received_at, h.heartbeat_id)"
    " < (CAST(:after_received_at AS timestamptz), CAST(:after_heartbeat_id AS uuid)))"
    " ORDER BY h.received_at DESC, h.heartbeat_id DESC LIMIT :limit"
)

_PLANT: Final = text(
    "SELECT 1 FROM identity.plant WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_NODE_PLANT: Final = text(
    "SELECT plant_id FROM identity.node_identity"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
)
_THRESHOLDS: Final = text(
    "SELECT queue_pending_threshold, queue_age_threshold_minutes, clock_drift_threshold_ms,"
    " updated_by, updated_at FROM fleet.plant_fleet_thresholds"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_SAVE_THRESHOLDS: Final = text(
    "INSERT INTO fleet.plant_fleet_thresholds (plant_id, organization_id, queue_pending_threshold,"
    " queue_age_threshold_minutes, clock_drift_threshold_ms, updated_by, updated_at)"
    " VALUES (:plant_id, :organization_id, :queue_pending_threshold,"
    " :queue_age_threshold_minutes, :clock_drift_threshold_ms, :updated_by, :updated_at)"
    " ON CONFLICT (plant_id) DO UPDATE SET"
    " queue_pending_threshold = EXCLUDED.queue_pending_threshold,"
    " queue_age_threshold_minutes = EXCLUDED.queue_age_threshold_minutes,"
    " clock_drift_threshold_ms = EXCLUDED.clock_drift_threshold_ms,"
    " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at"
    " WHERE fleet.plant_fleet_thresholds.organization_id = EXCLUDED.organization_id"
)


def scope_parameters(context: ScopeContext) -> dict[str, Any]:
    """``allowed_scopes`` del contexto como parámetros (organización entera, plantas, zonas)."""
    scopes = context.allowed_scopes
    return {
        "whole_organization": any(
            s.scope_level is ScopeLevel.ORGANIZATION and s.scope_id == context.organization_id
            for s in scopes
        ),
        "scope_plants": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.PLANT)
        ),
        "scope_zones": list(
            dict.fromkeys(s.scope_id for s in scopes if s.scope_level is ScopeLevel.ZONE)
        ),
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class InventoryFilters:
    """Filtros de ``GET /fleet/nodes`` (``None``: sin filtro)."""

    plant_id: uuid.UUID | None = None
    communication_state: CommunicationState | None = None
    warning: FleetAlarmKind | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatCursor:
    """Posición en la historia de latidos: el último ``(received_at, heartbeat_id)`` devuelto."""

    received_at: datetime
    heartbeat_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class HeartbeatEntry:
    """Una fila de ``HeartbeatHistory``: solo identificadores, marcas y ``payload_summary``."""

    heartbeat_id: uuid.UUID
    received_at: datetime
    sent_at: datetime
    payload_summary: Mapping[str, Any]


def _uuid(value: object) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(str(value))


def _document(value: object) -> Any:
    return None if value is None else json.loads(str(value))


def _inventory(row: Row[Any]) -> NodeInventory:
    cameras = tuple(
        CameraState(
            camera_id=_uuid(item["camera_id"]),
            code=item["code"],
            connected=bool(item["connected"]),
            measured_fps=float(item["measured_fps"]),
            declared_min_fps=float(item["declared_min_fps"]),
            observability_state=str(item["observability_state"]),
        )
        for item in _document(row.cameras)
    )
    zone_states = tuple(
        ZoneState(
            zone_id=_uuid(item["zone_id"]),
            mode=str(item["mode"]),
            observability_state=str(item["observability_state"]),
            catalog_version=int(item["catalog_version"]),
            coverage_ok=bool(item["coverage_ok"]),
        )
        for item in _document(row.zone_states)
    )
    return NodeInventory(
        node_id=_uuid(row.node_id),
        code=str(row.code),
        plant_id=_uuid(row.plant_id),
        zones=tuple(_uuid(zone) for zone in row.zones),
        status=str(row.status),
        decommissioned_at=row.decommissioned_at,
        replaces_node_id=None if row.replaces_node_id is None else _uuid(row.replaces_node_id),
        software_version=row.software_version,
        contract_version=row.contract_version,
        contract_notice=_document(row.contract_notice),
        model_version=row.model_version,
        last_heartbeat_at=row.last_heartbeat_at,
        communication_state=CommunicationState(row.communication_state),
        since=row.since,
        local_queue=_document(row.local_queue),
        clock=_document(row.clock),
        signal_reader=_document(row.signal_reader),
        cameras=cameras,
        zone_states=zone_states,
        warnings=tuple(FleetAlarmKind(kind) for kind in row.warnings),
        target_version=row.target_version,
        last_update_result=row.last_update_result,
        live_view_local_url=row.live_view_local_url,
    )


def inventory_parameters(
    context: ScopeContext,
    now: datetime,
    *,
    filters: InventoryFilters | None = None,
    node_id: uuid.UUID | None = None,
    after: str | None = None,
    limit: int,
) -> dict[str, Any]:
    """Los parámetros de ``_INVENTORY``: alcance, filtros, ``now`` y constantes de los avisos."""
    filters = filters if filters is not None else InventoryFilters()
    return {
        "organization_id": context.organization_id,
        **scope_parameters(context),
        "node_id": node_id,
        "plant_id": filters.plant_id,
        "communication_state": (
            None if filters.communication_state is None else filters.communication_state.value
        ),
        "warning": None if filters.warning is None else filters.warning.value,
        "after": after,
        "limit": limit,
        "now": now,
        "mute_factor": MUTE_FACTOR,
        "default_interval": DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        "default_pending": DEFAULT_QUEUE_PENDING_THRESHOLD,
        "default_age": DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES,
        "default_drift": DEFAULT_CLOCK_DRIFT_THRESHOLD_MS,
        "certificate_until": now + CERTIFICATE_ALERT_BEFORE,
        "clips_since": now - ORPHAN_CLIPS_WINDOW,
        "orphan_max": ORPHAN_CLIPS_MAX,
        "orphan_percent": ORPHAN_CLIPS_PERCENT,
    }


@repository
class PostgresInventoryQueries:
    """Lecturas del inventario y de los umbrales: una sentencia cada una."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def read_inventory(
        self, context: ScopeContext, parameters: Mapping[str, Any]
    ) -> tuple[NodeInventory, ...]:
        """Una página (o un nodo) en una transacción ``READ ONLY`` de una sola sentencia."""
        rows: Sequence[Row[Any]] = await self._database.read(context, _INVENTORY, dict(parameters))
        return tuple(_inventory(row) for row in rows)

    async def inventory(
        self, transaction: Transaction, parameters: Mapping[str, Any]
    ) -> tuple[NodeInventory, ...]:
        """Una página (o un nodo) del inventario, dentro de la transacción del llamador."""
        result = await transaction.execute(_INVENTORY, dict(parameters))
        return tuple(_inventory(row) for row in result)

    async def heartbeat_history(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        now: datetime,
        after: HeartbeatCursor | None,
        limit: int,
    ) -> tuple[HeartbeatEntry, ...]:
        """La historia de latidos del nodo dentro de la retención, la más reciente primero."""
        result = await transaction.execute(
            _HISTORY,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": node_id,
                "since": now - HISTORY_RETENTION,
                "after_received_at": None if after is None else after.received_at,
                "after_heartbeat_id": None if after is None else after.heartbeat_id,
                "limit": limit,
            },
        )
        return tuple(
            HeartbeatEntry(
                heartbeat_id=_uuid(row.heartbeat_id),
                received_at=row.received_at,
                sent_at=row.sent_at,
                payload_summary=json.loads(row.payload_summary),
            )
            for row in result
        )

    async def plant_exists(self, context: ScopeContext, plant_id: uuid.UUID) -> bool:
        """¿Existe la planta en la organización del contexto (y la deja ver la RLS)?"""
        rows = await self._database.read(
            context, _PLANT, {"organization_id": context.organization_id, "plant_id": plant_id}
        )
        return bool(rows)

    async def node_plant(self, context: ScopeContext, node_id: uuid.UUID) -> uuid.UUID | None:
        """La planta del nodo de la organización del contexto, o ``None`` (inexistente o ajeno)."""
        rows = await self._database.read(
            context, _NODE_PLANT, {"organization_id": context.organization_id, "node_id": node_id}
        )
        return _uuid(rows[0].plant_id) if rows else None

    async def thresholds(self, transaction: Transaction, plant_id: uuid.UUID) -> FleetThresholds:
        """Los umbrales de la planta; sin fila, los valores por defecto (BR-GOB-79)."""
        result = await transaction.execute(
            _THRESHOLDS,
            {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
        )
        row = result.first()
        if row is None:
            return FleetThresholds(plant_id=plant_id)
        return FleetThresholds(
            plant_id=plant_id,
            queue_pending_threshold=int(row.queue_pending_threshold),
            queue_age_threshold_minutes=int(row.queue_age_threshold_minutes),
            clock_drift_threshold_ms=int(row.clock_drift_threshold_ms),
            updated_by=_uuid(row.updated_by),
            updated_at=row.updated_at,
        )

    async def save_thresholds(self, transaction: Transaction, thresholds: FleetThresholds) -> None:
        """Inserta o sustituye la fila de la planta (una sentencia, sin carrera de inserción)."""
        if thresholds.updated_by is None or thresholds.updated_at is None:
            raise ValueError("los umbrales guardados llevan autor y momento")
        await transaction.execute(
            _SAVE_THRESHOLDS,
            {
                "plant_id": thresholds.plant_id,
                "organization_id": transaction.context.organization_id,
                "queue_pending_threshold": thresholds.queue_pending_threshold,
                "queue_age_threshold_minutes": thresholds.queue_age_threshold_minutes,
                "clock_drift_threshold_ms": thresholds.clock_drift_threshold_ms,
                "updated_by": thresholds.updated_by,
                "updated_at": thresholds.updated_at,
            },
        )
