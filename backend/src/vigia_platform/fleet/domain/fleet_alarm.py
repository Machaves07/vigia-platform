"""``FleetAlarm``: la alarma de flota por transición (DE §3.9; BL §3.6; BR-GOB-76, 81; LC-GOB-16).

Una alarma nace (``raised_at``, evento ``fleet_alarm_raised``) cuando su condición **entra** en
vigor y se cierra una sola vez (``cleared_at``, ``fleet_alarm_cleared``) cuando **sale**: entre un
``raised`` y su ``cleared`` nunca hay otro ``raised`` de la misma clase y nodo, aunque la condición
dure días. La base lo garantiza con la ranura ``fleet.open_fleet_alarm`` (gob_0018): un índice
único no cruza las particiones mensuales de ``fleet_alarm``, así que la garantía vive en esa
proyección, que solo escriben los disparadores.

**Quién decide cada clase** (nota de BL §2.6 y NFR-GOB-12):

- ``detect_mute_nodes`` levanta ``node_mute`` (con la transición a ``mute``);
- ``alert_expiring_certificates`` levanta ``certificate_expiring``;
- ``evaluate_fleet_alarms`` levanta y baja las otras seis y **baja** ``node_mute`` (el nodo volvió
  a ``reachable``) y ``certificate_expiring`` (tras la rotación);
- un nodo revocado o dado de baja no se evalúa ni levanta nada; sus alarmas abiertas las cierra
  ``evaluate_fleet_alarms`` una sola vez (decisión del redactor de TASK-225, BR-GOB-76).

**``since``** de los dos eventos es desde cuándo se observa la condición de esa transición: la
primera de las evaluaciones consecutivas que la confirmaron (histéresis), ``last_heartbeat_at`` en
``node_mute`` (el silencio empieza en el último latido, BR-GOB-74) o el instante de la evaluación
en las clases sin estado. ``FleetAlarm`` no guarda ``since``: lo lleva el evento.

``zone_id`` solo va en ``simulated_adapter_in_productive``: la zona productiva que el nodo atiende
(la de menor ``zone_id``), la que la alerta de seguridad cita (NFR-GOB-36). Las demás clases son
de nodo.

Las cargas son solo identificadores, la clase y marcas (BR-NUC-75): nunca texto libre.

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLEARED_EVENT",
    "EVALUATED_KINDS",
    "RAISED_EVENT",
    "RETIRED_STATUS",
    "STATELESS_EVALUATED_KINDS",
    "AlarmStatus",
    "FleetAlarm",
    "NewAlarm",
    "cleared_payload",
    "is_retired",
    "raised_payload",
]

RAISED_EVENT: Final = "fleet_alarm_raised"
CLEARED_EVENT: Final = "fleet_alarm_cleared"
RETIRED_STATUS: Final = "revoked"
"""``NodeIdentity.status`` de un nodo revocado (BR-GOB-76)."""

EVALUATED_KINDS: Final = frozenset(
    {
        FleetAlarmKind.QUEUE_OVER_THRESHOLD,
        FleetAlarmKind.CLOCK_DRIFT,
        FleetAlarmKind.VERSION_RETIRING,
        FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE,
        FleetAlarmKind.CAMERA_BELOW_MIN_FPS,
        FleetAlarmKind.ORPHAN_CLIPS_GROWING,
    }
)
"""Las seis clases que ``evaluate_fleet_alarms`` levanta y baja."""
STATELESS_EVALUATED_KINDS: Final = frozenset(
    {FleetAlarmKind.VERSION_RETIRING, FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE}
)
"""Las dos de ``EVALUATED_KINDS`` sin histéresis: deciden en la primera evaluación."""


class AlarmStatus(enum.StrEnum):
    """Filtro de ``GET /fleet/alarms``: abiertas o ya cerradas."""

    ACTIVE = "active"
    CLEARED = "cleared"


def is_retired(
    status: str, *, revoked_at: datetime | None, decommissioned_at: datetime | None
) -> bool:
    """¿Revocado o dado de baja? Entonces no se evalúa ni levanta alarmas (BR-GOB-76)."""
    return status == RETIRED_STATUS or revoked_at is not None or decommissioned_at is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetAlarm:
    """Una fila de ``fleet.fleet_alarm`` (DE §3.9)."""

    alarm_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    alarm_kind: FleetAlarmKind
    node_id: uuid.UUID
    zone_id: uuid.UUID | None
    raised_at: datetime
    cleared_at: datetime | None
    raised_event_id: uuid.UUID
    cleared_event_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if (self.cleared_at is None) != (self.cleared_event_id is None):
            raise ValueError("cleared_at y cleared_event_id van juntos")
        if self.cleared_at is not None and self.cleared_at < self.raised_at:
            raise ValueError("una alarma no se cierra antes de levantarse")
        if self.zone_id is not None and (
            self.alarm_kind is not FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE
        ):
            raise ValueError("solo simulated_adapter_in_productive lleva zona")

    @property
    def open(self) -> bool:
        return self.cleared_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class NewAlarm:
    """Una alarma por levantar: su clase, su nodo y desde cuándo se observa la condición."""

    alarm_kind: FleetAlarmKind
    plant_id: uuid.UUID
    node_id: uuid.UUID
    since: datetime
    zone_id: uuid.UUID | None = None


def raised_payload(
    *,
    alarm_id: uuid.UUID,
    kind: FleetAlarmKind,
    node_id: uuid.UUID,
    zone_id: uuid.UUID | None,
    since: datetime,
) -> dict[str, Any]:
    """La carga de ``fleet_alarm_raised`` (``FleetAlarmRaised``; interfaces §2, nota T-08)."""
    payload: dict[str, Any] = {
        "alarm_id": str(alarm_id),
        "alarm_kind": kind.value,
        "node_id": str(node_id),
        "since": format_timestamp(since),
    }
    if zone_id is not None:
        payload["zone_id"] = str(zone_id)
    return payload


def cleared_payload(alarm: FleetAlarm, *, since: datetime, cleared_at: datetime) -> dict[str, Any]:
    """La carga de ``fleet_alarm_cleared`` (``FleetAlarmCleared``; interfaces §2, nota T-08)."""
    payload = raised_payload(
        alarm_id=alarm.alarm_id,
        kind=alarm.alarm_kind,
        node_id=alarm.node_id,
        zone_id=alarm.zone_id,
        since=since,
    )
    payload["cleared_at"] = format_timestamp(cleared_at)
    return payload
