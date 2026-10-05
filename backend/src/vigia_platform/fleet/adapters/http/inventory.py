"""Inventario de flota de SCR-07 (TASK-224; interfaces §3.4 y «Versión 1.5»; LC-GOB-15).

- ``GET /fleet/nodes`` (``fleet.read``, organización, planta o zona; ``after``, ``limit`` ≤ 100,
  ``plant_id``, ``communication_state``, ``warning``) → ``{nodes: NodeInventory[], next_after}``:
  una sentencia por página, avisos de las **ocho** clases calculados en la lectura.
- ``GET /fleet/nodes/{node_id}`` (``fleet.read``, planta; ``history_after``, ``history_limit``) →
  el mismo ``NodeInventory`` más ``heartbeat_history`` (90 días, solo ``payload_summary``) y
  ``next_history_after``.

Cada nodo lleva ``heartbeat_notice`` (``no_heartbeat_since`` o ``no_heartbeat_received``, con
etiqueta «Sin latido desde» / «Sin latido recibido todavía»): el panel dice «sin latido desde»,
nunca «sin eventos» (BR-GOB-75). ``status`` admite ``re_enrollment_pending`` (re-alta) además de los
tres valores de la interfaz, y ``last_update_result`` admite ``failed`` («Versión 1.5»).

Un nodo inexistente, de otra organización o fuera del alcance responde ``not_found``, nunca
``forbidden``; un parámetro desconocido o repetido, ``invalid_request``. ``Cache-Control:
no-store``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.adapters.postgres.inventory_queries import (
    HeartbeatCursor,
    HeartbeatEntry,
    InventoryFilters,
)
from vigia_platform.fleet.application.inventory_read import MAX_HISTORY_PAGE, MAX_NODES_PAGE
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_warnings import HeartbeatNotice, heartbeat_notice
from vigia_platform.fleet.ports import NodeInventory
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["inventory_router"]

MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NODE_CODE: Final = re.compile(r"[A-Z0-9-]{2,32}")
"""``Code`` de ``identity.node_identity`` (nuc_0004)."""
_READ: Final = PermissionKey.FLEET_READ.value

Services = Annotated[FleetHttp, Depends(fleet_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractNoticeView(_Strict):
    result: str
    retires_at: str | None = None


class DeadLetterView(_Strict):
    code: str
    count: int


class LocalQueueView(_Strict):
    pending: int
    oldest_pending_at: str | None = None
    dead_letter: tuple[DeadLetterView, ...]


class ClockView(_Strict):
    synchronized: bool
    offset_ms: int


class SignalReaderView(_Strict):
    available: bool
    adapter: str


class CameraView(_Strict):
    camera_id: uuid.UUID
    code: str | None
    connected: bool
    measured_fps: float
    declared_min_fps: float
    observability_state: str


class ZoneStateView(_Strict):
    zone_id: uuid.UUID
    mode: str
    observability_state: str
    catalog_version: int
    coverage_ok: bool


class NodeInventoryView(_Strict):
    """``NodeInventory`` (interfaces §3.4 y «Versión 1.5»); sin latido, sus campos son ``null``."""

    node_id: uuid.UUID
    code: str
    plant_id: uuid.UUID
    zones: tuple[uuid.UUID, ...]
    status: str
    decommissioned_at: str | None
    replaces_node_id: uuid.UUID | None
    software_version: str | None
    contract_version: str | None
    contract_notice: ContractNoticeView | None
    model_version: str | None
    last_heartbeat_at: str | None
    communication_state: CommunicationState
    since: str
    heartbeat_notice: HeartbeatNotice | None
    """«Sin latido desde» / «Sin latido recibido todavía»; nunca «sin eventos» (BR-GOB-75)."""
    local_queue: LocalQueueView | None
    clock: ClockView | None
    signal_reader: SignalReaderView | None
    cameras: tuple[CameraView, ...]
    zone_states: tuple[ZoneStateView, ...]
    warnings: tuple[FleetAlarmKind, ...]
    target_version: str | None
    last_update_result: str | None
    live_view_local_url: str | None


class NodeInventoryPage(_Strict):
    nodes: tuple[NodeInventoryView, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


class HeartbeatView(_Strict):
    heartbeat_id: uuid.UUID
    received_at: str
    sent_at: str
    payload_summary: dict[str, Any]


class NodeInventoryDetail(_Strict):
    node: NodeInventoryView
    heartbeat_history: tuple[HeartbeatView, ...]
    next_history_after: str | None


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else format_timestamp(value)


def node_view(node: NodeInventory) -> NodeInventoryView:
    """La forma de la respuesta de un ``NodeInventory``."""
    queue = node.local_queue
    return NodeInventoryView(
        node_id=node.node_id,
        code=node.code,
        plant_id=node.plant_id,
        zones=node.zones,
        status=node.status,
        decommissioned_at=_stamp(node.decommissioned_at),
        replaces_node_id=node.replaces_node_id,
        software_version=node.software_version,
        contract_version=node.contract_version,
        contract_notice=(
            None
            if node.contract_notice is None
            else ContractNoticeView(
                result=node.contract_notice["result"],
                retires_at=node.contract_notice.get("retires_at"),
            )
        ),
        model_version=node.model_version,
        last_heartbeat_at=_stamp(node.last_heartbeat_at),
        communication_state=node.communication_state,
        since=format_timestamp(node.since),
        heartbeat_notice=heartbeat_notice(
            node.last_heartbeat_at, node.communication_state.value, node.warnings
        ),
        local_queue=(
            None
            if queue is None
            else LocalQueueView(
                pending=queue["pending"],
                oldest_pending_at=queue.get("oldest_pending_at"),
                dead_letter=tuple(
                    DeadLetterView(code=entry["code"], count=entry["count"])
                    for entry in queue.get("dead_letter", ())
                ),
            )
        ),
        clock=None if node.clock is None else ClockView(**node.clock),
        signal_reader=None
        if node.signal_reader is None
        else SignalReaderView(**node.signal_reader),
        cameras=tuple(
            CameraView(
                camera_id=camera.camera_id,
                code=camera.code,
                connected=camera.connected,
                measured_fps=camera.measured_fps,
                declared_min_fps=camera.declared_min_fps,
                observability_state=camera.observability_state,
            )
            for camera in node.cameras
        ),
        zone_states=tuple(
            ZoneStateView(
                zone_id=zone.zone_id,
                mode=zone.mode,
                observability_state=zone.observability_state,
                catalog_version=zone.catalog_version,
                coverage_ok=zone.coverage_ok,
            )
            for zone in node.zone_states
        ),
        warnings=node.warnings,
        target_version=node.target_version,
        last_update_result=node.last_update_result,
        live_view_local_url=node.live_view_local_url,
    )


def _encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def _decode(value: str) -> str:
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def encode_node_cursor(code: str) -> str:
    """Cursor opaco de la página: el ``code`` del último nodo devuelto."""
    return _encode(code)


def decode_node_cursor(value: str) -> str:
    """El ``code`` de ``encode_node_cursor``; cualquier otra cosa, ``invalid_request``."""
    code = _decode(value)
    if _NODE_CODE.fullmatch(code) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return code


def encode_history_cursor(cursor: HeartbeatCursor) -> str:
    """Cursor opaco de la historia: la marca con microsegundos y el ``heartbeat_id``."""
    return _encode(f"{cursor.received_at.astimezone(UTC).isoformat()}|{cursor.heartbeat_id}")


def decode_history_cursor(value: str) -> HeartbeatCursor:
    """El cursor de ``encode_history_cursor``; cualquier otra cosa, ``invalid_request``."""
    moment, _, identifier = _decode(value).partition("|")
    try:
        received_at = datetime.fromisoformat(moment)
        heartbeat_id = uuid.UUID(identifier)
    except (ValueError, TypeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if received_at.utcoffset() is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return HeartbeatCursor(received_at, heartbeat_id)


def _heartbeat(entry: HeartbeatEntry) -> HeartbeatView:
    return HeartbeatView(
        heartbeat_id=entry.heartbeat_id,
        received_at=format_timestamp(entry.received_at),
        sent_at=format_timestamp(entry.sent_at),
        payload_summary=dict(entry.payload_summary),
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def inventory_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.get(
        "/fleet/nodes",
        dependencies=[
            requires(_READ),
            Depends(exact_query("after", "limit", "plant_id", "communication_state", "warning")),
        ],
        summary="Inventario de flota con sus avisos, por páginas de hasta 100 nodos",
    )
    async def list_fleet_nodes(
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_NODES_PAGE)] = MAX_NODES_PAGE,
        plant_id: uuid.UUID | None = None,
        communication_state: CommunicationState | None = None,
        warning: FleetAlarmKind | None = None,
    ) -> NodeInventoryPage:
        _no_store(response)
        cursor = None if after is None else decode_node_cursor(after)
        page = await installed(services.inventory).page(
            request_context(request),
            filters=InventoryFilters(
                plant_id=plant_id, communication_state=communication_state, warning=warning
            ),
            after=cursor,
            limit=limit,
        )
        return NodeInventoryPage(
            nodes=tuple(node_view(node) for node in page.items),
            next_after=None if page.next_after is None else encode_node_cursor(page.next_after),
        )

    @router.get(
        "/fleet/nodes/{node_id}",
        dependencies=[requires(_READ), Depends(exact_query("history_after", "history_limit"))],
        summary="Un nodo del inventario con su historia de latidos (90 días)",
    )
    async def fleet_node(
        node_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        history_after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        history_limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE)] = MAX_HISTORY_PAGE,
    ) -> NodeInventoryDetail:
        _no_store(response)
        cursor = None if history_after is None else decode_history_cursor(history_after)
        detail = await installed(services.inventory).detail(
            request_context(request), node_id, history_after=cursor, history_limit=history_limit
        )
        return NodeInventoryDetail(
            node=node_view(detail.node),
            heartbeat_history=tuple(_heartbeat(entry) for entry in detail.history),
            next_history_after=(
                None if detail.next_history is None else encode_history_cursor(detail.next_history)
            ),
        )

    return router
