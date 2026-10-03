"""Bucle de ``asyncio`` con reloj virtual para pruebas de topes (VIG-134).

Con ``run_virtual`` los topes (``wait_for``, ``asyncio.timeout``, ``sleep``, ``call_later``)
vencen en el orden de sus plazos y el tiempo medido con ``loop.time()`` es exacto. El reloj solo
avanza cuando el bucle no tiene nada listo: entonces salta al próximo temporizador en vez de
dormir. Un runner cargado tarda más en pared, pero no cambia qué tope vence antes.

Solo sirve para código que mide el tiempo con el reloj del bucle y no espera E/S real ni hilos.
"""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass
class _VirtualClock:
    now: float = 0.0


class _VirtualClockSelector(selectors.DefaultSelector):
    """Selector que no duerme: adelanta el reloj virtual hasta el próximo temporizador."""

    def __init__(self, clock: _VirtualClock) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is not None and timeout > 0:
            self._clock.now += timeout
            timeout = 0
        return super().select(timeout)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """``SelectorEventLoop`` cuyo ``time()`` es el reloj virtual."""

    def __init__(self) -> None:
        self._clock = _VirtualClock()
        super().__init__(_VirtualClockSelector(self._clock))

    def time(self) -> float:
        return self._clock.now


def run_virtual[T](scenario: Coroutine[Any, Any, T], *, cap_seconds: float) -> T:
    """``asyncio.run(scenario)`` sobre ``VirtualTimeLoop``, con un tope externo (virtual) de
    ``cap_seconds``: si una mutación dejara el escenario colgado, sale ``TimeoutError`` al
    instante en vez de colgar la suite."""
    return asyncio.run(asyncio.wait_for(scenario, cap_seconds), loop_factory=VirtualTimeLoop)
