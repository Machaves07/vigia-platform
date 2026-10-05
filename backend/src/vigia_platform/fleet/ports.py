"""``FleetQueryPort``: el inventario y las asignaciones zona-nodo para U-04 (LC-GOB-23b; interfaces
§1.3; PAT-GOB-REN-04).

En proceso, **solo lectura**, siempre con ``ScopeContext``, una sentencia por operación, sin paginar
ni cachear (U-04 no puede hacerlo):

- ``nodes_by_zone(context, zone_id)``: los nodos que atienden la zona ahora, con su estado de
  comunicación y sus cámaras;
- ``node(context, node_id)``: el ``NodeInventory`` completo (§3.4, el de ``GET /fleet/nodes``);
- ``assignment_at(context, zone_id, at)``: la asignación vigente en ``at``, o el **hueco** en que la
  zona no tenía nodo (``node_id`` nulo, acotado por la asignación anterior y la siguiente), para
  rotular «no se observó» citando el nodo; un reemplazo deja el hueco trazado;
- ``assignment_history(context, zone_id, from, to)``: las asignaciones de ``ZoneNodeAssignment``
  (U-02) que se solapan con ``[from, to]``, en orden de ``assigned_at``; el rango es de
  **366 días** como mucho y superarlo es un error (``FleetRangeTooLong``), **nunca** un resultado
  truncado.

Las asignaciones se leen sobre ``assigned_at`` y ``unassigned_at`` de la tabla de U-02, con su
exclusión GiST (A-12, A-32): U-03 no la modifica. Retención: ``ZoneNodeAssignment`` no caduca.

**Alcance** (como ``CoveragePort`` de U-02): la RLS limita a la organización del contexto y la zona
o el nodo tienen que estar en ``allowed_scopes`` (la organización, su planta o la zona; un nodo, por
su planta o por una zona que atiende). Inexistente, de otra organización o fuera del alcance lanzan
``ResourceNotFound`` (``not_found``, nunca ``forbidden``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.context import ScopeContext

__all__ = [
    "MAX_ASSIGNMENT_RANGE",
    "CameraState",
    "FleetQueryInvalid",
    "FleetQueryPort",
    "FleetRangeTooLong",
    "NodeInventory",
    "ZoneAssignmentPeriod",
    "ZoneNode",
    "ZoneNodeCamera",
    "ZoneState",
]

MAX_ASSIGNMENT_RANGE: Final = timedelta(days=366)
"""Tope de ``assignment_history`` (interfaces v1.1 §1.3; PAT-GOB-REN-04)."""


class FleetQueryInvalid(ValueError):
    """Entrada del puerto mal formada (tipo, instante sin zona, ``from`` posterior a ``to``)."""


class FleetRangeTooLong(ValueError):
    """``assignment_history`` con más de 366 días: error, nunca un resultado truncado."""

    detail_code: Final = FleetDetailCode.ASSIGNMENT_RANGE_TOO_LONG

    def __init__(self) -> None:
        super().__init__("el rango de assignment_history es de 366 días como mucho")


@dataclass(frozen=True, slots=True)
class CameraState:
    """Una cámara del último latido aceptado (``CameraInventory``, DE §3.6)."""

    camera_id: uuid.UUID
    code: str | None
    """El ``code`` del catálogo vigente de una zona del nodo; ``None`` si ninguna la declara."""
    connected: bool
    measured_fps: float
    declared_min_fps: float
    observability_state: str


@dataclass(frozen=True, slots=True)
class ZoneState:
    """Una zona del último latido aceptado tal como la ve el nodo (``ZoneNodeState``, DE §3.7)."""

    zone_id: uuid.UUID
    mode: str
    observability_state: str
    catalog_version: int
    coverage_ok: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeInventory:
    """El nodo para SCR-07 y para U-04 (interfaces §3.4 y «Versión 1.5»; DE §3.5).

    Sin latido aceptado todavía (nodo declarado), los campos del latido son ``None`` y las listas
    están vacías. ``status`` admite ``re_enrollment_pending`` (re-alta, alineación con la v1.5).
    """

    node_id: uuid.UUID
    code: str
    plant_id: uuid.UUID
    zones: tuple[uuid.UUID, ...]
    status: str
    decommissioned_at: datetime | None
    replaces_node_id: uuid.UUID | None
    software_version: str | None
    contract_version: str | None
    contract_notice: dict[str, Any] | None
    model_version: str | None
    last_heartbeat_at: datetime | None
    communication_state: CommunicationState
    since: datetime
    local_queue: dict[str, Any] | None
    clock: dict[str, Any] | None
    signal_reader: dict[str, Any] | None
    cameras: tuple[CameraState, ...]
    zone_states: tuple[ZoneState, ...]
    warnings: tuple[FleetAlarmKind, ...]
    target_version: str | None
    last_update_result: str | None
    live_view_local_url: str | None


@dataclass(frozen=True, slots=True)
class ZoneNodeCamera:
    """Una cámara en ``nodes_by_zone`` (sin la tasa mínima declarada)."""

    camera_id: uuid.UUID
    code: str | None
    connected: bool
    measured_fps: float
    observability_state: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneNode:
    """Un nodo que atiende la zona, para U-04."""

    node_id: uuid.UUID
    code: str
    communication_state: CommunicationState
    since: datetime
    last_heartbeat_at: datetime | None
    cameras: tuple[ZoneNodeCamera, ...] = field(default=())


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneAssignmentPeriod:
    """Una asignación ``[assigned_at, unassigned_at)`` de la zona, o un hueco sin nodo.

    En un hueco (solo en ``assignment_at``) ``node_id`` y ``replaces_node_id`` son ``None``,
    ``assigned_at`` es el inicio del hueco (la retirada anterior o, si nunca hubo nodo, la creación
    de la zona) y ``unassigned_at`` la siguiente asignación, si la hay.
    """

    node_id: uuid.UUID | None
    assigned_at: datetime
    unassigned_at: datetime | None
    replaces_node_id: uuid.UUID | None


class FleetQueryPort(Protocol):
    """Puerto de lectura de la flota para U-04 (interfaces §1.3)."""

    async def nodes_by_zone(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> Sequence[ZoneNode]: ...

    async def node(self, context: ScopeContext, node_id: uuid.UUID) -> NodeInventory: ...

    async def assignment_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneAssignmentPeriod: ...

    async def assignment_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[ZoneAssignmentPeriod]: ...
