"""Proyección de un latido aceptado (LC-GOB-14; BR-GOB-70 a 73 y 97; DE §3.5 a §3.8; PR-GOB-25).

``project`` calcula, sin tocar la base, lo que un latido aceptado deja escrito:

- ``NodeInventory`` (una fila por nodo): versiones, ``contract_notice {result, retires_at?}``,
  ``last_heartbeat_at`` (reloj de la plataforma; **nunca retrocede**: el máximo con el anterior),
  ``communication_state = reachable``, cola local, reloj ``{synchronized, offset_ms}``, lector de
  señales ``{available, adapter}`` y ``uptime_seconds``;
- ``CameraInventory`` de cada cámara informada (1 a 8);
- ``ZoneNodeState`` de cada zona informada **que el nodo tiene asignada ahora**: una zona que el
  nodo reporta y ya no tiene asignada se ignora (no rechaza el latido; el nodo se entera por la
  respuesta, BR-GOB-70). ``coverage_ok`` sale del catálogo vigente de la zona (``coverage``);
- la fila de ``HeartbeatHistory`` con ``payload_summary``: **solo** identificadores, enumeraciones,
  booleanos y números (NFR-GOB-25): ni versiones, ni la fuente de tiempo, ni la URL local, ni nada
  que el nodo escriba con texto;
- si escribe la vuelta a ``reachable`` (``communication_state``) y si cambió ``model_version``
  frente al último latido aceptado (BR-GOB-51: un cambio solo de ``software_version`` no marca
  regresión).

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema (``received_at`` llega del
``Clock`` de la ruta).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_contracts.models.enumerations import (
    CompatibilityResult,
    ObservabilityState,
    SignalReaderAdapter,
    ZoneMode,
)
from vigia_contracts.models.heartbeat import Heartbeat

from vigia_platform.fleet.domain.communication_state import (
    CommunicationState,
    returns_to_reachable,
)
from vigia_platform.fleet.domain.coverage import CameraObservation, zone_coverage_ok
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "GATE_RENEWAL_MARGIN",
    "HISTORY_RETENTION",
    "CameraRow",
    "ContractNoticeState",
    "HeartbeatProjection",
    "HistoryRow",
    "InventoryRow",
    "PreviousInventory",
    "ZoneRow",
    "parse_instant",
    "payload_summary",
    "project",
]

HISTORY_RETENTION: Final = timedelta(days=90)
"""``HeartbeatHistory`` en línea 90 días `[estimación propia]` (BR-GOB-72): la búsqueda del
duplicado se acota a esa ventana (y a sus particiones)."""
GATE_RENEWAL_MARGIN: Final = timedelta(hours=24)
"""A-55: un sobre de compuertas al que le quedan menos de 24 h se renueva al responder."""


def parse_instant(value: str) -> datetime:
    """Un ``Timestamp`` del contrato (``…Z``) como instante UTC."""
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("el instante del contrato lleva zona horaria")
    return moment


@dataclass(frozen=True, slots=True)
class ContractNoticeState:
    """``contract_notice`` de la versión del nodo (A-44): resultado y fecha de retiro, si la hay."""

    result: CompatibilityResult
    retires_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", CompatibilityResult(self.result))
        if self.result is CompatibilityResult.ACCEPTED_WITH_NOTICE and self.retires_at is None:
            raise ValueError("un aviso de retiro lleva su fecha (BR-CTR-21)")

    def as_inventory(self) -> dict[str, str]:
        """La forma de ``NodeInventory.contract_notice`` (sin texto)."""
        notice = {"result": self.result.value}
        if self.retires_at is not None:
            notice["retires_at"] = self.retires_at
        return notice

    def message_es(self, version: str) -> str:
        """Mensaje genérico (BR-CTR-16): nombra la versión y la fecha de retiro, nada más."""
        if self.retires_at is not None:
            return f"La versión {version} del contrato del nodo se retira el {self.retires_at}."
        return f"La versión {version} del contrato del nodo es compatible con la plataforma."


@dataclass(frozen=True, slots=True)
class PreviousInventory:
    """Lo que el latido necesita de la fila ``NodeInventory`` anterior, ya bloqueada."""

    model_version: str
    last_heartbeat_at: datetime | None
    communication_state: CommunicationState


@dataclass(frozen=True, slots=True)
class InventoryRow:
    """``NodeInventory`` (DE §3.5) tras el latido."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    software_version: str
    contract_version: str
    model_version: str
    contract_notice: Mapping[str, str]
    last_heartbeat_at: datetime
    communication_state: CommunicationState
    local_queue: Mapping[str, Any]
    clock: Mapping[str, Any]
    signal_reader: Mapping[str, Any]
    uptime_seconds: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CameraRow:
    """``CameraInventory`` (DE §3.6) de una cámara del latido."""

    camera_id: uuid.UUID
    connected: bool
    measured_fps: float
    declared_min_fps: float
    observability_state: ObservabilityState


@dataclass(frozen=True, slots=True)
class ZoneRow:
    """``ZoneNodeState`` (DE §3.7) de una zona asignada que el latido informa."""

    zone_id: uuid.UUID
    mode: ZoneMode
    observability_state: ObservabilityState
    catalog_version_in_node: int
    gate_state_valid_until: datetime
    open_episodes: int
    coverage_ok: bool


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """``HeartbeatHistory`` (DE §3.8)."""

    heartbeat_id: uuid.UUID
    received_at: datetime
    sent_at: datetime
    payload_summary: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HeartbeatProjection:
    """Lo que escribe un latido aceptado y las dos decisiones que lo acompañan."""

    inventory: InventoryRow
    cameras: tuple[CameraRow, ...]
    zones: tuple[ZoneRow, ...]
    history: HistoryRow
    reachable_transition: bool
    """Escribe ``node_communication_state_changed`` con ``reachable`` (BR-GOB-73)."""
    model_changed: bool
    """``model_version`` distinto del último latido aceptado: marca la regresión (BR-GOB-51)."""
    ignored_zones: tuple[uuid.UUID, ...] = field(default=())
    """Zonas informadas que el nodo ya no tiene asignadas (BR-GOB-70)."""


def _uuid(value: Any) -> uuid.UUID:
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def payload_summary(heartbeat: Heartbeat, assigned_zones: frozenset[uuid.UUID]) -> dict[str, Any]:
    """El resumen de ``HeartbeatHistory``: identificadores, enumeraciones, booleanos y números.

    Acotado (16 zonas y 8 cámaras como máximo se resumen en recuentos): muy por debajo de 1,5 KB
    por fila (NFR-GOB-07). Nunca lleva versiones, la fuente de tiempo, la URL local ni etiquetas.
    """
    cameras = heartbeat.cameras
    states = [camera.observability_state for camera in cameras]
    zones = heartbeat.zones
    queue = heartbeat.local_queue
    summary_queue: dict[str, Any] = {
        "pending": queue.pending,
        "dead_letter": sum(entry.count for entry in queue.dead_letter),
        "retained_sent": queue.retained_sent,
    }
    if queue.circuit_state is not None:
        summary_queue["circuit_state"] = queue.circuit_state.value
    if queue.lost_episodes is not None:
        summary_queue["lost_episodes"] = queue.lost_episodes
    return {
        "cameras": {
            "total": len(cameras),
            "connected": sum(1 for camera in cameras if camera.connected),
            **{state.value: states.count(state) for state in ObservabilityState},
        },
        "zones": {
            "reported": len(zones),
            "ignored": sum(1 for zone in zones if _uuid(zone.zone_id) not in assigned_zones),
            **{mode.value: sum(1 for zone in zones if zone.mode is mode) for mode in ZoneMode},
        },
        "local_queue": summary_queue,
        "clock": {
            "synchronized": heartbeat.node_clock.synchronized,
            "offset_ms": heartbeat.node_clock.offset_ms,
        },
        "signal_reader": {
            "available": heartbeat.signal_reader.available,
            "adapter": SignalReaderAdapter(heartbeat.signal_reader.adapter).value,
        },
        "uptime_seconds": heartbeat.uptime_seconds,
        "live_view_accesses": len(heartbeat.live_view_accesses or ()),
        "live_view_local_url": heartbeat.live_view_local_url is not None,
    }


def project(
    heartbeat: Heartbeat,
    *,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    received_at: datetime,
    previous: PreviousInventory | None,
    assigned_zones: frozenset[uuid.UUID],
    catalogs: Mapping[uuid.UUID, Mapping[str, Any] | None],
    notice: ContractNoticeState,
    retired: bool,
) -> HeartbeatProjection:
    """La proyección del latido aceptado ``heartbeat`` recibido en ``received_at``.

    ``previous`` es la fila anterior del inventario (``None`` en el primer latido); ``catalogs``,
    el ``ZoneCatalog`` vigente de cada zona asignada (para ``coverage_ok``); ``retired``, si el nodo
    está revocado o dado de baja (sin transición de comunicación ni regresión, BR-GOB-76).
    """
    received = to_millisecond(received_at)
    last = received
    if previous is not None and previous.last_heartbeat_at is not None:
        last = max(last, to_millisecond(previous.last_heartbeat_at))
    node_id = _uuid(heartbeat.node_id)
    observations = tuple(
        CameraObservation(_uuid(camera.camera_id), ObservabilityState(camera.observability_state))
        for camera in heartbeat.cameras
    )
    cameras = tuple(
        CameraRow(
            camera_id=_uuid(camera.camera_id),
            connected=camera.connected,
            measured_fps=float(camera.measured_fps),
            declared_min_fps=float(camera.declared_min_fps),
            observability_state=ObservabilityState(camera.observability_state),
        )
        for camera in heartbeat.cameras
    )
    zones: list[ZoneRow] = []
    ignored: list[uuid.UUID] = []
    for zone in heartbeat.zones:
        zone_id = _uuid(zone.zone_id)
        if zone_id not in assigned_zones:
            ignored.append(zone_id)
            continue
        zones.append(
            ZoneRow(
                zone_id=zone_id,
                mode=ZoneMode(zone.mode),
                observability_state=ObservabilityState(zone.observability_state),
                catalog_version_in_node=zone.catalog_version,
                gate_state_valid_until=parse_instant(zone.gate_state_valid_until),
                open_episodes=zone.open_episodes,
                coverage_ok=zone_coverage_ok(catalogs.get(zone_id), observations),
            )
        )
    queue = heartbeat.local_queue.to_json_value()
    inventory = InventoryRow(
        node_id=node_id,
        organization_id=organization_id,
        plant_id=plant_id,
        software_version=heartbeat.software_version,
        contract_version=heartbeat.contract_version,
        model_version=heartbeat.model_version,
        contract_notice=notice.as_inventory(),
        last_heartbeat_at=last,
        communication_state=CommunicationState.REACHABLE,
        local_queue=queue,
        clock={
            "synchronized": heartbeat.node_clock.synchronized,
            "offset_ms": heartbeat.node_clock.offset_ms,
        },
        signal_reader={
            "available": heartbeat.signal_reader.available,
            "adapter": SignalReaderAdapter(heartbeat.signal_reader.adapter).value,
        },
        uptime_seconds=heartbeat.uptime_seconds,
        updated_at=received,
    )
    previous_state = None if previous is None else previous.communication_state
    return HeartbeatProjection(
        inventory=inventory,
        cameras=tuple(sorted(cameras, key=lambda row: str(row.camera_id))),
        zones=tuple(sorted(zones, key=lambda row: str(row.zone_id))),
        history=HistoryRow(
            heartbeat_id=_uuid(heartbeat.heartbeat_id),
            received_at=received,
            sent_at=parse_instant(heartbeat.sent_at),
            payload_summary=payload_summary(heartbeat, assigned_zones),
        ),
        reachable_transition=returns_to_reachable(previous_state, retired=retired),
        model_changed=(
            not retired
            and previous is not None
            and previous.model_version != heartbeat.model_version
        ),
        ignored_zones=tuple(ignored),
    )
