"""PR-LAZ-08 (con estado): en cola + resueltos = llegados en todo momento.

TASK-309; LC-LAZ-08; domain-entities.md de U-04 §4.1 (T02, T05 y T06); H-05 y H-23. Tras cualquier
secuencia de ``review_queue_commands`` (llegadas, resoluciones por índice, avances de reloj y
recordatorios):

- todo elemento llegado está, en todo momento, o bien ``in_review`` (en cola) o bien resuelto
  (``open`` por T05 o ``discarded`` por T06): nunca ningún otro estado, y el total coincide con
  el número de llegadas (T02 solo existe desde «—»: una detección nace una sola vez);
- **ninguna transición a resuelto sin su comando**: cada elemento resuelto lo fue por un
  ``ResolveCommand`` que esta máquina ejecutó, nunca por un avance de reloj ni por un
  recordatorio (``RemindCommand`` nunca saca nada de la cola, BR del resumen de la tarea);
- un ``ResolveCommand`` sobre un elemento que ya no está ``in_review`` se rechaza
  (``transition_not_allowed``), sin cambiar su estado: la máquina usa ``loop.domain.lifecycle``
  real, no una reimplementación;
- **un recordatorio por ventana** (U04-H-07): nunca se envían dos recordatorios del mismo
  elemento con menos de ``REMINDER_INTERVAL_HOURS`` de modelo entre ellos.

La máquina sobre el dominio puro contrasta la propiedad estructural (PR-LAZ-08); la garantía de
exclusión de la escritura real (dos resoluciones simultáneas, el candado de la detección) y la de
``send_reminders`` (la actualización condicional sobre PostgreSQL) están en
``tests/integration/test_loop_review_queue.py``, con su mutación en rojo.

Solo datos generados; ningún reloj real.
"""

from __future__ import annotations

from typing import Final

from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.properties.laz.strategies.review_queue import (
    AdvanceClockCommand,
    ArriveCommand,
    ResolveCommand,
    ReviewQueueCommand,
    review_queue_commands,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, ReviewResolutionOutcome
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    LifecycleCode,
    LifecycleSnapshot,
    ReceiveDetection,
    Rejected,
    ResolveReview,
    transition,
)

REMINDER_INTERVAL_HOURS: Final = 24.0
REVIEW_DEADLINE_HOURS: Final = 24.0
"""Plazos fijos del modelo (``[estimación propia]`` para la prueba; el valor real lo decide la
configuración de la planta, U-04 §2.13)."""

_RESOLVED_STATES: Final = frozenset({FindingLifecycleState.OPEN, FindingLifecycleState.DISCARDED})


class ReviewQueueDomain(RuleBasedStateMachine):
    """El dominio puro: cada elemento llegado, su plazo y sus recordatorios, en el modelo."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = 0.0
        self.state: list[FindingLifecycleState] = []
        self.due_at: list[float] = []
        self.last_reminder_at: list[float | None] = []
        self.reminders_sent: list[int] = []
        self.resolved_via_command: set[int] = set()

    def _arrive(self) -> None:
        accepted = transition(LifecycleSnapshot(None), ReceiveDetection())
        assert isinstance(accepted, Accepted)
        assert accepted.state is FindingLifecycleState.IN_REVIEW
        self.state.append(accepted.state)
        self.due_at.append(self.clock + REVIEW_DEADLINE_HOURS)
        self.last_reminder_at.append(None)
        self.reminders_sent.append(0)

    def _resolve(self, command: ResolveCommand) -> None:
        if not self.state:
            return
        index = command.subject_index % len(self.state)
        current = self.state[index]
        result = transition(LifecycleSnapshot(current), ResolveReview(command.outcome))
        if current is FindingLifecycleState.IN_REVIEW:
            assert isinstance(result, Accepted)
            confirmed = command.outcome is ReviewResolutionOutcome.REVIEW_CONFIRMED
            expected = FindingLifecycleState.OPEN if confirmed else FindingLifecycleState.DISCARDED
            assert result.state is expected
            self.state[index] = result.state
            self.resolved_via_command.add(index)
        else:
            assert isinstance(result, Rejected)
            assert result.code is LifecycleCode.TRANSITION_NOT_ALLOWED
            assert self.state[index] is current

    def _remind(self) -> None:
        for index, state in enumerate(self.state):
            if state is not FindingLifecycleState.IN_REVIEW:
                continue
            if self.due_at[index] > self.clock:
                continue
            last = self.last_reminder_at[index]
            if last is not None and self.clock - last < REMINDER_INTERVAL_HOURS:
                continue
            self.reminders_sent[index] += 1
            self.last_reminder_at[index] = self.clock

    @rule(command=review_queue_commands())
    def apply(self, command: ReviewQueueCommand) -> None:
        if isinstance(command, ArriveCommand):
            self._arrive()
        elif isinstance(command, ResolveCommand):
            self._resolve(command)
        elif isinstance(command, AdvanceClockCommand):
            self.clock += command.hours
        else:
            self._remind()

    @invariant()
    def queue_plus_resolved_equals_arrived(self) -> None:
        in_queue = sum(1 for s in self.state if s is FindingLifecycleState.IN_REVIEW)
        resolved = sum(1 for s in self.state if s in _RESOLVED_STATES)
        assert in_queue + resolved == len(self.state)
        only_known = FindingLifecycleState.IN_REVIEW, *_RESOLVED_STATES
        assert all(s in only_known for s in self.state)

    @invariant()
    def nothing_resolves_without_its_command(self) -> None:
        for index, state in enumerate(self.state):
            if state in _RESOLVED_STATES:
                assert index in self.resolved_via_command

    @invariant()
    def reminders_never_sent_again_within_the_window(self) -> None:
        for index, sent in enumerate(self.reminders_sent):
            if sent == 0:
                assert self.last_reminder_at[index] is None
            if self.state[index] in _RESOLVED_STATES:
                # Un elemento resuelto no vuelve a recordarse: su reloj se congela aquí.
                continue
            last = self.last_reminder_at[index]
            if last is not None:
                assert last >= self.due_at[index]


def test_pr_laz_08_the_domain_review_queue() -> None:
    """Con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(ReviewQueueDomain)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=40))
