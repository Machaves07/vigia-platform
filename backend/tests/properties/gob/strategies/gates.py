"""``gate_sequences``: secuencias de aprobaciones y revocaciones de compuertas (TASK-211).

Generador de U-03 para C-PLA-09 (business-logic-model §6, tech-stack §2.6): un ``GateScenario`` es
una secuencia de pasos; cada paso avanza el reloj inyectado (``0`` incluido, para que se repitan
instantes) y ejecuta una orden o **dos a la vez** (``concurrent``). Las órdenes son aprobar o
revocar una de las dos compuertas; algunas **deben** rechazarse (revocar una compuerta que no está
``approved``), y las propiedades comprueban que esas no cambian nada.

PR-GOB-03 aplica la secuencia sobre el dominio puro; PR-GOB-21, sobre el servicio de transición y
PostgreSQL real. Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from hypothesis import strategies as st

from vigia_platform.catalog.domain.enums import GateKind

__all__ = ["STEPS", "Approve", "Command", "GateScenario", "Revoke", "Step", "gate_sequences"]

STEPS: Final = (
    timedelta(0),
    timedelta(milliseconds=1),
    timedelta(seconds=1),
    timedelta(hours=1),
)
"""Avances del reloj entre pasos: el cero repite el instante (el relevo suma 1 ms)."""


@dataclass(frozen=True)
class Approve:
    """Un acta (montaje) o un acuerdo (uso) nuevos: ``approved`` desde cualquier estado."""

    gate: GateKind


@dataclass(frozen=True)
class Revoke:
    """Revocación con motivo: solo desde ``approved``."""

    gate: GateKind


Command = Approve | Revoke


@dataclass(frozen=True)
class Step:
    advance: timedelta
    commands: tuple[Command, ...]
    """Una orden, o dos que corren a la vez (concurrentes)."""

    @property
    def concurrent(self) -> bool:
        return len(self.commands) > 1


@dataclass(frozen=True)
class GateScenario:
    steps: tuple[Step, ...]


_COMMANDS: Final = st.one_of(
    st.builds(Approve, st.sampled_from(tuple(GateKind))),
    st.builds(Revoke, st.sampled_from(tuple(GateKind))),
)


def gate_sequences(
    *, max_steps: int = 8, concurrent: bool = True
) -> st.SearchStrategy[GateScenario]:
    """Escenarios de 1 a ``max_steps`` pasos; con ``concurrent``, algunos llevan dos órdenes."""
    width = st.integers(min_value=1, max_value=2 if concurrent else 1)
    step = st.builds(
        Step,
        st.sampled_from(STEPS),
        width.flatmap(lambda n: st.tuples(*(_COMMANDS for _ in range(n)))),
    )
    return st.builds(GateScenario, st.lists(step, min_size=1, max_size=max_steps).map(tuple))
