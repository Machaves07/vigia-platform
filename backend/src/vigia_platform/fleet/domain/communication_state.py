"""Estado de comunicación del nodo desde el latido (BR-GOB-73, 74 y 76; D-9; PR-GOB-08).

``unknown`` lo escribe la declaración del nodo; ``mute`` lo escribe la tarea ``detect_mute_nodes``
(TASK-224) cuando pasan cinco veces ``heartbeat_interval_seconds`` sin latido aceptado, con el
intervalo ``mute`` empezando en ``last_heartbeat_at``; la vuelta a ``reachable`` la escribe **la
propia ruta del latido**, dentro de su transacción, en el primer latido aceptado tras ``unknown`` o
``mute``. Así se escriben solo los cambios de estado, nunca cada latido (BR-NUC-69), y la unión de
los intervalos es una partición del periodo: cada transición cierra el intervalo anterior en el
instante en que abre el siguiente.

Un nodo revocado o dado de baja no genera más transiciones (BR-GOB-76). El estado de comunicación
describe al observador: nunca significa «zona despejada» ni «sin eventos» (BR-GOB-75, P2).

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from vigia_platform.ledger.domain.coverage import CommunicationState

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "MAX_HEARTBEAT_INTERVAL_SECONDS",
    "MIN_HEARTBEAT_INTERVAL_SECONDS",
    "MUTE_FACTOR",
    "CommunicationState",
    "is_mute_at",
    "mute_after_seconds",
    "mute_starts_at",
    "returns_to_reachable",
]

MUTE_FACTOR: Final = 5
"""El umbral de mudo es cinco veces el intervalo efectivo (BR-GOB-74, A-04, A-09, nº 35)."""
MIN_HEARTBEAT_INTERVAL_SECONDS: Final = 15
MAX_HEARTBEAT_INTERVAL_SECONDS: Final = 600
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 60
"""``NodeConfiguration.heartbeat_interval_seconds`` por defecto (D-11) `[objetivo propio]`."""


def mute_after_seconds(heartbeat_interval_seconds: int) -> int:
    """Cinco veces ``heartbeat_interval_seconds`` (300 con 60 s); el intervalo va de 15 a 600."""
    if (
        type(heartbeat_interval_seconds) is not int
        or not MIN_HEARTBEAT_INTERVAL_SECONDS
        <= heartbeat_interval_seconds
        <= MAX_HEARTBEAT_INTERVAL_SECONDS
    ):
        raise ValueError("heartbeat_interval_seconds va de 15 a 600")
    return MUTE_FACTOR * heartbeat_interval_seconds


def mute_starts_at(last_heartbeat_at: datetime) -> datetime:
    """El intervalo ``mute`` empieza en el último latido aceptado, no al detectarlo (BR-GOB-74)."""
    return last_heartbeat_at


def is_mute_at(last_heartbeat_at: datetime, now: datetime, heartbeat_interval_seconds: int) -> bool:
    """¿Pasó el umbral sin latido aceptado? La regla de la tarea de mudo (TASK-224)."""
    threshold = timedelta(seconds=mute_after_seconds(heartbeat_interval_seconds))
    return now - last_heartbeat_at >= threshold


def returns_to_reachable(previous: CommunicationState | None, *, retired: bool) -> bool:
    """¿Escribe este latido aceptado la vuelta a ``reachable``?

    ``previous`` es el estado del inventario del nodo antes del latido (``None`` si es el primero:
    el nodo recién declarado está ``unknown``). Solo cuando no estaba ``reachable`` y el nodo no
    está revocado ni dado de baja (``retired``): nunca dos ``reachable`` seguidos.
    """
    if retired:
        return False
    return previous is not CommunicationState.REACHABLE
