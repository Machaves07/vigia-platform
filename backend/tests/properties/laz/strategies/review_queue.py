"""Generador ``review_queue_commands`` de la cola de revisión (PR-LAZ-08; TASK-309).

Un comando por sorteo (como ``analysis_step_commands`` de ``strategies/root_cause.py``), para un
``RuleBasedStateMachine`` sobre el dominio puro (``loop.domain.lifecycle``):

- ``ArriveCommand``: una detección nueva llega a la cola (T02, desde «—»);
- ``ResolveCommand``: una resolución humana sobre una de las ya llegadas, por índice de llegada
  (``% n``, así que siempre nombra una detección real una vez que hay al menos una);
- ``AdvanceClockCommand``: el reloj del modelo avanza (nunca retrocede);
- ``RemindCommand``: la tarea horaria evalúa recordatorios sobre el estado del modelo.

La generación depende solo de lo dibujado (retro 14): ningún comando lee el reloj real ni la base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hypothesis import strategies as st

from vigia_platform.loop.domain.enums import ReviewResolutionOutcome

__all__ = [
    "MAX_SUBJECT_INDEX",
    "AdvanceClockCommand",
    "ArriveCommand",
    "RemindCommand",
    "ResolveCommand",
    "ReviewQueueCommand",
    "review_queue_commands",
]

MAX_SUBJECT_INDEX: Final = 7
"""Índices de llegada que ``ResolveCommand`` puede nombrar (``% n`` sobre lo ya llegado)."""


@dataclass(frozen=True, slots=True)
class ArriveCommand:
    pass


@dataclass(frozen=True, slots=True)
class ResolveCommand:
    subject_index: int
    outcome: ReviewResolutionOutcome


@dataclass(frozen=True, slots=True)
class AdvanceClockCommand:
    hours: float


@dataclass(frozen=True, slots=True)
class RemindCommand:
    pass


type ReviewQueueCommand = ArriveCommand | ResolveCommand | AdvanceClockCommand | RemindCommand


def review_queue_commands() -> st.SearchStrategy[ReviewQueueCommand]:
    return st.one_of(
        st.builds(ArriveCommand),
        st.builds(
            ResolveCommand,
            subject_index=st.integers(min_value=0, max_value=MAX_SUBJECT_INDEX - 1),
            outcome=st.sampled_from(ReviewResolutionOutcome),
        ),
        st.builds(
            AdvanceClockCommand,
            hours=st.floats(min_value=0.5, max_value=48.0, allow_nan=False, allow_infinity=False),
        ),
        st.builds(RemindCommand),
    )
