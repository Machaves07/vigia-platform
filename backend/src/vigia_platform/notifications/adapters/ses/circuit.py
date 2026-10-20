"""``EmailCircuitBreaker``: el cortacircuito privado, en memoria del proceso, de un adaptador de
correo (``SesEmailSender`` y ``FakeEmailSender``; PAT-LAZ-RES-04).

Reutiliza ``ConsumerBreaker`` de U-02 **tal cual** (máquina de estados pura, apertura al primer
fallo de dependencia declarado, sonda cada 60 s): el adaptador de correo no es un consumidor de
la bandeja de salida (no hay fila de ``shared.consumer`` que compartir entre procesos), así que
cada proceso guarda y sirve su propio estado, protegido por un candado de proceso (``asyncio
.Lock``) que solo serializa las transiciones, nunca la llamada de red.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.outbox.breaker import (
    BreakerAction,
    BreakerState,
    CircuitState,
    ConsumerBreaker,
)

__all__ = ["EmailCircuitBreaker"]


def _state_literal(state: CircuitState) -> Literal["closed", "open", "half_open"]:
    if state is CircuitState.CLOSED:
        return "closed"
    if state is CircuitState.OPEN:
        return "open"
    return "half_open"


class EmailCircuitBreaker:
    """El circuito de un adaptador de correo: ``guard()`` antes de intentar, ``on_success`` o
    ``on_dependency_failure`` después."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._state = BreakerState.closed()
        self._lock = asyncio.Lock()

    def circuit_state(self) -> Literal["closed", "open", "half_open"]:
        return _state_literal(self._state.state)

    async def guard(self) -> bool:
        """``True`` si esta llamada intenta la dependencia (cerrado o la sonda que reclama)."""
        async with self._lock:
            now = self._clock.now()
            action = ConsumerBreaker.tick(self._state, now)
            if action is BreakerAction.PAUSE:
                return False
            if action is BreakerAction.PROBE:
                self._state = ConsumerBreaker.on_probe(self._state, now)
            return True

    async def on_success(self) -> None:
        async with self._lock:
            probe = self._state.state is CircuitState.HALF_OPEN
            self._state = ConsumerBreaker.on_success(self._state, probe=probe)

    async def on_dependency_failure(self) -> None:
        async with self._lock:
            now = self._clock.now()
            probe = self._state.state is CircuitState.HALF_OPEN
            self._state = ConsumerBreaker.on_dependency_failure(self._state, now, probe=probe)
