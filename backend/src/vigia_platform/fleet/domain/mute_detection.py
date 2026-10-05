"""Detección de nodos mudos (BR-GOB-74, 76; BL §2.6; D-9; A-04, A-09; PR-GOB-08).

``detect_mute_nodes`` marca ``mute`` al nodo ``enrolled``, no revocado ni dado de baja, del que
pasaron **más de** cinco veces ``heartbeat_interval_seconds`` efectivo sin latido aceptado (la
condición de BL §2.6, la misma del aviso ``node_mute`` de ``fleet_warnings``):

- el intervalo ``mute`` **empieza en** ``last_heartbeat_at``, nunca en el instante del barrido ni
  en la declaración (``mute_transition``): es la marca que la línea de cobertura de U-04 usa, así
  que nunca se inventa un tramo;
- un nodo que nunca latió no tiene ``last_heartbeat_at`` y sigue ``unknown``;
- un nodo ya ``mute`` no escribe otra transición (nunca dos estados iguales seguidos).

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.fleet.domain.communication_state import is_mute_at
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MUTE_ELIGIBLE_STATUS",
    "MuteCandidate",
    "is_silent",
    "mute_transition",
]

MUTE_ELIGIBLE_STATUS: Final = "enrolled"
"""Solo un nodo dado de alta pasa a ``mute`` (BL §2.6)."""


@dataclass(frozen=True, slots=True)
class MuteCandidate:
    """Un nodo silencioso, leído con su fila de ``NodeInventory`` bloqueada."""

    node_id: uuid.UUID
    plant_id: uuid.UUID
    last_heartbeat_at: datetime
    communication_state: CommunicationState

    @property
    def transitions(self) -> bool:
        """``False`` si ya estaba ``mute`` (solo le falta la alarma, sin otra transición)."""
        return self.communication_state is not CommunicationState.MUTE


def is_silent(last_heartbeat_at: datetime | None, now: datetime, interval_seconds: int) -> bool:
    """¿Pasaron **más de** cinco veces el intervalo sin latido? Sin latido nunca, ``False``."""
    if last_heartbeat_at is None:
        return False
    return is_mute_at(last_heartbeat_at, now, interval_seconds)


def mute_transition(node_id: uuid.UUID, last_heartbeat_at: datetime) -> dict[str, Any]:
    """El contenido de ``node_communication_state_changed`` con ``mute`` (BR-GOB-74): ``since``
    es ``last_heartbeat_at``."""
    stamp = format_timestamp(last_heartbeat_at)
    return {
        "node_id": str(node_id),
        "state": CommunicationState.MUTE.value,
        "since": stamp,
        "last_heartbeat_at": stamp,
    }
