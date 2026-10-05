"""``clock_offsets`` y el oráculo de la compuerta de uso de la ingesta (TASK-221, PR-GOB-04 y 17).

- ``clock_offsets()``: el reloj que declara el nodo (``synchronized`` y ``offset_ms``) con los
  bordes de la cota de 5 minutos (``±300 000`` y uno más o menos), cero, valores enormes y
  negativos; y ``fact_offsets()``: dónde cae ``node_time.started_at`` respecto de un instante de
  referencia (cero, 1 ms, justo dentro y fuera de la tolerancia máxima, horas, y en el futuro).
- ``UsageHistory``: el oráculo **independiente** de la compuerta de uso: la lista de cambios
  ``(instante, aprobado)`` en orden y «aprobado en algún instante de ``[a, b]``» calculado
  recorriéndola, como la plataforma simulada de U-01 (``_Zone.usage_approved_during``); no usa
  ``fleet.domain`` ni los intervalos que guarda la ingesta.
- ``expected_window``: la ventana de la decisión, escrita aquí a partir de la regla (BR-GOB-90 y su
  lectura de TASK-221): ``[t - tol, t + tol]`` con ``tol = min(|offset|, 300 s)`` o el instante de
  recepción si el reloj no está sincronizado.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Final

from hypothesis import strategies as st

__all__ = [
    "LIMIT_MS",
    "ClockDeclaration",
    "UsageHistory",
    "clock_offsets",
    "expected_window",
    "fact_offsets",
]

LIMIT_MS: Final = 300_000
"""La cota de la tolerancia (5 minutos)."""


@dataclass(frozen=True)
class ClockDeclaration:
    synchronized: bool
    offset_ms: int


def clock_offsets() -> st.SearchStrategy[ClockDeclaration]:
    """El reloj declarado, con los bordes de la cota."""
    edges = st.sampled_from(
        [0, 1, -1, LIMIT_MS - 1, LIMIT_MS, LIMIT_MS + 1, -LIMIT_MS, -(LIMIT_MS + 1), 10**12]
    )
    offset = st.one_of(edges, st.integers(-(10**9), 10**9))
    return st.builds(ClockDeclaration, st.booleans(), offset)


def fact_offsets() -> st.SearchStrategy[dt.timedelta]:
    """Distancia de ``started_at`` a la referencia (positiva: antes; negativa: después)."""
    ms = st.sampled_from(
        [
            0,
            1,
            1000,
            LIMIT_MS - 1,
            LIMIT_MS,
            LIMIT_MS + 1,
            3_600_000,
            -1,
            -LIMIT_MS,
            -(LIMIT_MS + 1),
        ]
    )
    return st.one_of(ms, st.integers(-2 * LIMIT_MS, 2 * LIMIT_MS)).map(
        lambda value: dt.timedelta(milliseconds=value)
    )


def expected_window(
    started_at: dt.datetime, clock: ClockDeclaration, received_at: dt.datetime
) -> tuple[dt.datetime, dt.datetime]:
    if not clock.synchronized:
        return received_at, received_at
    tolerance = dt.timedelta(milliseconds=min(abs(clock.offset_ms), LIMIT_MS))
    return started_at - tolerance, started_at + tolerance


@dataclass
class UsageHistory:
    """Los cambios de la compuerta de uso en orden: ``(instante, aprobado)``."""

    changes: list[tuple[dt.datetime, bool]] = field(default_factory=list)

    @property
    def approved_now(self) -> bool:
        return bool(self.changes) and self.changes[-1][1]

    def change(self, at: dt.datetime, approved: bool) -> None:
        if self.changes:
            assert at > self.changes[-1][0], "los cambios avanzan"
        self.changes.append((at, approved))

    def approved_during(self, start: dt.datetime, end: dt.datetime) -> bool:
        status = False
        for at, approved in self.changes:
            if at > end:
                break
            if at <= start:
                status = approved
            elif approved:
                return True
        return status
