"""Avisos del inventario: las **ocho** clases de ``fleet_alarm_kind`` (LC-GOB-15; PAT-GOB-ESC-01
y su nota; DE §3.5 y su nota; BR-GOB-49, 65, 74, 76, 78, 79, 80 y 94).

``evaluate`` es el **evaluador de referencia**, puro, de lo que ``GET /fleet/nodes`` calcula en la
misma consulta que lee la proyección (``adapters.postgres.inventory_queries``), cruzándola con los
umbrales vigentes de la planta y sin materializar nada: un cambio de umbral se ve en la respuesta
siguiente sin recorrer nodos. PR-GOB-27 compara las dos fila a fila. Las alarmas por transición y
su histéresis son de ``fleet.alarms`` (TASK-225), no de los avisos.

Las ocho condiciones, con ``now`` del ``Clock`` de la plataforma:

- ``node_mute``: ``now - last_heartbeat_at > 5 * heartbeat_interval_seconds`` (el intervalo
  efectivo de ``NodeConfiguration``; 60 s sin configuración). Un nodo del que nunca llegó un latido
  no es mudo: su estado es ``unknown`` (BR-GOB-74);
- ``queue_over_threshold``: ``local_queue.pending > queue_pending_threshold`` o el pendiente más
  viejo con antigüedad ``> queue_age_threshold_minutes``;
- ``clock_drift``: ``|clock.offset_ms| > clock_drift_threshold_ms``;
- ``version_retiring``: ``contract_notice.retires_at`` presente, la fecha que entrega el contrato
  y nunca una antelación inventada (BR-GOB-80);
- ``simulated_adapter_in_productive``: ``signal_reader.adapter`` ``simulated`` o ``file`` con alguna
  zona asignada al nodo en ``productive`` según ``ZoneGateState.resulting_mode`` de la plataforma,
  no según el modo que el nodo cree (BR-GOB-78);
- ``certificate_expiring``: la credencial vigente (``active`` u ``overlapping``) de vencimiento más
  lejano vence en 15 días o menos, o ya venció (BR-GOB-65, ``alert_before_days``);
- ``camera_below_min_fps``: alguna cámara del último latido con ``measured_fps <
  declared_min_fps`` (BR-GOB-49);
- ``orphan_clips_growing``: en las últimas 24 h, más de 50 clips huérfanos o más del 5 % de los
  clips del día (concesiones ``evidence``; los de verificación nunca cuentan, nota de BR-GOB-94)
  `[objetivo propio]`.

Un nodo ``revoked`` o con ``decommissioned_at`` no muestra avisos (BR-GOB-76). Los avisos describen
al **observador**: ninguno significa «zona despejada» ni «sin eventos» (BR-GOB-75, P2).

``heartbeat_notice`` es el rótulo del panel para un nodo sin latido reciente: «Sin latido desde»
(con ``last_heartbeat_at``) o «Sin latido recibido todavía», etiquetas de ``heartbeat_notice`` en
``labels.platform.es.json``. Nunca existe un rótulo «sin eventos» (BR-GOB-75).

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.fleet.domain.communication_state import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    mute_after_seconds,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds

__all__ = [
    "CERTIFICATE_ALERT_BEFORE",
    "INVENTORY_LABEL_BINDINGS",
    "ORPHAN_CLIPS_MAX",
    "ORPHAN_CLIPS_PERCENT",
    "ORPHAN_CLIPS_WINDOW",
    "RETIRED_STATUS",
    "SIMULATED_ADAPTERS",
    "CameraReading",
    "HeartbeatNotice",
    "WarningInputs",
    "evaluate",
    "heartbeat_notice",
    "orphan_clips_growing",
]

CERTIFICATE_ALERT_BEFORE: Final = timedelta(days=15)
"""``alert_before_days`` del certificado del nodo (BR-GOB-65, NFR-CTR-13)."""
ORPHAN_CLIPS_MAX: Final = 50
"""Más de 50 huérfanos en la ventana (BR-GOB-94) `[objetivo propio]`."""
ORPHAN_CLIPS_PERCENT: Final = 5
"""O más del 5 % de los clips del día (BR-GOB-94) `[objetivo propio]`."""
ORPHAN_CLIPS_WINDOW: Final = timedelta(hours=24)
"""Ventana de los huérfanos y de los clips del día: las últimas 24 h."""
SIMULATED_ADAPTERS: Final = frozenset({"simulated", "file"})
"""Adaptadores que nunca valen en una zona productiva (BR-GOB-78, BR-CTR-54)."""
RETIRED_STATUS: Final = "revoked"
"""``NodeIdentity.status`` de un nodo revocado (sin avisos, BR-GOB-76)."""


class HeartbeatNotice(enum.StrEnum):
    """Rótulo del panel para un nodo sin latido reciente (BR-GOB-75): nunca «sin eventos»."""

    NO_HEARTBEAT_SINCE = "no_heartbeat_since"
    NO_HEARTBEAT_RECEIVED = "no_heartbeat_received"


INVENTORY_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "heartbeat_notice": HeartbeatNotice,
}
"""Etiquetas del inventario en ``labels.platform.es.json``: el arranque falla si falta una."""


@dataclass(frozen=True, slots=True)
class CameraReading:
    """Lo que el aviso de tasa mira de una cámara del último latido."""

    measured_fps: float
    declared_min_fps: float


@dataclass(frozen=True, slots=True, kw_only=True)
class WarningInputs:
    """Lo que necesitan los ocho avisos de un nodo (lo mismo que lee la consulta del inventario)."""

    status: str
    decommissioned_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    pending: int | None = None
    oldest_pending_at: datetime | None = None
    offset_ms: int | None = None
    retires_at: str | None = None
    adapter: str | None = None
    productive_zone: bool = False
    """Alguna zona asignada al nodo en ``productive`` según ``ZoneGateState.resulting_mode``."""
    certificate_expires_at: datetime | None = None
    """El mayor ``expires_at`` de las credenciales ``active`` u ``overlapping``, o ninguno."""
    cameras: Sequence[CameraReading] = field(default=())
    orphan_clips: int = 0
    day_clips: int = 0


def orphan_clips_growing(orphan_clips: int, day_clips: int) -> bool:
    """Más de 50 huérfanos o más del 5 % de los clips del día (en enteros, sin redondeo)."""
    return orphan_clips > ORPHAN_CLIPS_MAX or orphan_clips * 100 > ORPHAN_CLIPS_PERCENT * day_clips


def heartbeat_notice(
    last_heartbeat_at: datetime | None,
    communication_state: str,
    warnings: Sequence[FleetAlarmKind],
) -> HeartbeatNotice | None:
    """El rótulo del panel: sin latido nunca, sin latido desde (mudo o con aviso), o ninguno."""
    if last_heartbeat_at is None:
        return HeartbeatNotice.NO_HEARTBEAT_RECEIVED
    if communication_state == "mute" or FleetAlarmKind.NODE_MUTE in warnings:
        return HeartbeatNotice.NO_HEARTBEAT_SINCE
    return None


def evaluate(
    inputs: WarningInputs, thresholds: FleetThresholds, now: datetime
) -> tuple[FleetAlarmKind, ...]:
    """Los avisos del nodo en ``now``, en el orden de ``fleet_alarm_kind``; ninguno si está
    retirado."""
    if inputs.status == RETIRED_STATUS or inputs.decommissioned_at is not None:
        return ()
    found: set[FleetAlarmKind] = set()
    if inputs.last_heartbeat_at is not None and now - inputs.last_heartbeat_at > timedelta(
        seconds=mute_after_seconds(inputs.heartbeat_interval_seconds)
    ):
        found.add(FleetAlarmKind.NODE_MUTE)
    if inputs.pending is not None and inputs.pending > thresholds.queue_pending_threshold:
        found.add(FleetAlarmKind.QUEUE_OVER_THRESHOLD)
    if inputs.oldest_pending_at is not None and now - inputs.oldest_pending_at > timedelta(
        minutes=thresholds.queue_age_threshold_minutes
    ):
        found.add(FleetAlarmKind.QUEUE_OVER_THRESHOLD)
    if inputs.offset_ms is not None and abs(inputs.offset_ms) > thresholds.clock_drift_threshold_ms:
        found.add(FleetAlarmKind.CLOCK_DRIFT)
    if inputs.retires_at is not None:
        found.add(FleetAlarmKind.VERSION_RETIRING)
    if inputs.adapter in SIMULATED_ADAPTERS and inputs.productive_zone:
        found.add(FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE)
    if (
        inputs.certificate_expires_at is not None
        and inputs.certificate_expires_at - now <= CERTIFICATE_ALERT_BEFORE
    ):
        found.add(FleetAlarmKind.CERTIFICATE_EXPIRING)
    if any(camera.measured_fps < camera.declared_min_fps for camera in inputs.cameras):
        found.add(FleetAlarmKind.CAMERA_BELOW_MIN_FPS)
    if orphan_clips_growing(inputs.orphan_clips, inputs.day_clips):
        found.add(FleetAlarmKind.ORPHAN_CLIPS_GROWING)
    return tuple(kind for kind in FleetAlarmKind if kind in found)
