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

Dos máquinas con las mismas reglas (PAT-LAZ, igual que PR-LAZ-18 con ``RootCauseService``): una
sobre el dominio puro (sin base, abajo) y otra sobre el sistema real —``ReviewQueueConsumer``,
``ReviewQueueService`` y ``ReviewQueueReminder`` contra PostgreSQL 16 (``integration``)— que
compara cada paso contra la fila real de ``loop.review_queue_item``. La primera no basta sola: no
toca ``ReviewQueueRepository.send_reminders`` ni su ``UPDATE`` condicional, así que no detecta una
regresión ahí (revisión automática de la ronda 1: mutar ``_SEND_REMINDERS`` para que también
ponga ``state = 'resolved'`` no la hace fallar). La segunda máquina sí, porque su invariante
compara el ``state`` real de cada elemento con el del modelo tras cada ``RemindCommand``.

Solo datos generados; ningún reloj real.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, ClassVar, Final

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_review_queue_support import REASON, ReviewQueueStack, review_queue_stack
from tests.properties.laz.strategies.review_queue import (
    AdvanceClockCommand,
    ArriveCommand,
    ResolveCommand,
    ReviewQueueCommand,
    review_queue_commands,
)
from vigia_platform.loop.application.review_queue import (
    ReviewQueueReminder,
    ReviewResolutionRejected,
)
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
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


# --- el sistema real ------------------------------------------------------------------------------

SERVICE_EXAMPLES: Final = 8
"""Secuencias por semilla contra la base: cada orden es una transacción real (como PR-LAZ-18)."""
REMINDER_WINDOW_SECONDS: Final = REMINDER_INTERVAL_HOURS * 3600
"""``reminder_interval_hours`` por defecto de la planta (``LoopSettings.defaults()``) en segundos,
el mismo reloj que usa esta máquina (``stack.tick``, en segundos)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ReviewQueueStack]:
    with review_queue_stack(postgres_endpoint, "pr_laz_08") as built:
        yield built


class ReviewQueueServiceMachine(RuleBasedStateMachine):
    """El sistema real: ``ReviewQueueConsumer``, ``ReviewQueueService`` y ``ReviewQueueReminder``
    contra PostgreSQL 16, con el mismo reloj simulado para todos los elementos de un ejemplo.

    Cada elemento nace ya vencido (``review_due_at`` justo tras ``received_at``, como
    ``_make_overdue`` de ``test_loop_review_queue.py``): así ``RemindCommand`` tiene efecto sin
    esperar el plazo real de días hábiles, que no es lo que esta propiedad ejercita."""

    stack: ClassVar[ReviewQueueStack]

    def __init__(self) -> None:
        super().__init__()
        self.organization: LoopOrganization = self.stack.world.organization()
        self.detections: list[IngestedSubject] = []
        self.state: list[FindingLifecycleState] = []
        self.reminders_sent: list[int] = []
        self.last_reminder_at: list[float | None] = []
        self.resolved_via_command: set[int] = set()
        self.clock = 0.0

    def _index_of(self, detection_id: uuid.UUID) -> int:
        return next(i for i, d in enumerate(self.detections) if d.subject_id == detection_id)

    def _arrive(self) -> None:
        detection = self.stack.in_review(self.organization)
        self.stack.world.stack.execute(
            "UPDATE loop.review_queue_item SET review_due_at = received_at + interval '1 second'"
            " WHERE organization_id = $1 AND detection_id = $2",
            self.organization.organization_id,
            detection.subject_id,
        )
        self.stack.tick(5)
        self.clock += 5
        self.detections.append(detection)
        self.state.append(FindingLifecycleState.IN_REVIEW)
        self.reminders_sent.append(0)
        self.last_reminder_at.append(None)

    def _resolve(self, command: ResolveCommand) -> None:
        if not self.detections:
            return
        index = command.subject_index % len(self.detections)
        current = self.state[index]
        confirmed = command.outcome is ReviewResolutionOutcome.REVIEW_CONFIRMED
        category = "real_coexistence" if confirmed else "no_person_present"
        # Sesión fresca en cada resolución: una creada en __init__ y reusada tras un
        # AdvanceClockCommand grande expira por inactividad (igual que en
        # test_the_list_orders_by_due_date_and_paginates).
        who = self.stack.member(self.organization)
        try:
            self.stack.run(
                self.stack.service.resolve(
                    self.stack.context(who),
                    self.detections[index].subject_id,
                    outcome=command.outcome.value,
                    reason_category=category,
                    reason_text=REASON,
                )
            )
        except ReviewResolutionRejected as rejected:
            assert current is not FindingLifecycleState.IN_REVIEW
            assert rejected.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
            assert self.state[index] is current
            return
        assert current is FindingLifecycleState.IN_REVIEW, command
        expected = FindingLifecycleState.OPEN if confirmed else FindingLifecycleState.DISCARDED
        self.state[index] = expected
        self.resolved_via_command.add(index)

    def _is_due(self, index: int) -> bool:
        if self.state[index] is not FindingLifecycleState.IN_REVIEW:
            return False
        last = self.last_reminder_at[index]
        return last is None or self.clock - last >= REMINDER_WINDOW_SECONDS

    def _remind(self) -> None:
        due_before = {
            self.detections[i].subject_id for i in range(len(self.detections)) if self._is_due(i)
        }
        reminder = ReviewQueueReminder(
            repository=self.stack.repository,
            configuration=PostgresLoopConfigurationRepository(self.stack.database),
            outbox=self.stack.world.stack.outbox,
            clock=self.stack.world.stack.authz.sessions.clock,
        )
        context = self.stack.world.event_context(self.organization)

        async def go() -> Any:
            async with self.stack.database.transaction(context) as transaction:
                return await reminder.remind(transaction)

        sent = self.stack.run(go())
        got = {event.detection_id for event in sent}
        assert got == due_before, (got, due_before)
        for event in sent:
            index = self._index_of(event.detection_id)
            self.reminders_sent[index] += 1
            self.last_reminder_at[index] = self.clock
            assert event.reminders_sent == self.reminders_sent[index]

    @rule(command=review_queue_commands())
    def apply(self, command: ReviewQueueCommand) -> None:
        if isinstance(command, ArriveCommand):
            self._arrive()
        elif isinstance(command, ResolveCommand):
            self._resolve(command)
        elif isinstance(command, AdvanceClockCommand):
            seconds = command.hours * 3600
            self.stack.tick(seconds)
            self.clock += seconds
        else:
            self._remind()

    @invariant()
    def the_rows_match_the_model(self) -> None:
        """Compara el ``state`` y ``reminders_sent`` reales con el modelo: la mutación de la
        revisión automática (``_SEND_REMINDERS`` también pone ``state = 'resolved'``) hace fallar
        esta comprobación en el primer ``RemindCommand`` con algo pendiente, porque el modelo
        sigue esperando ``pending``/``in_review`` mientras la fila real ya dice ``resolved``."""
        for index, detection in enumerate(self.detections):
            row = self.stack.queue_row(detection)
            assert row is not None
            expected_state = (
                "pending" if self.state[index] is FindingLifecycleState.IN_REVIEW else "resolved"
            )
            assert row["state"] == expected_state, (detection.subject_id, row, self.state[index])
            assert row["reminders_sent"] == self.reminders_sent[index]


@pytest.mark.integration
def test_pr_laz_08_the_real_review_queue(stack: ReviewQueueStack) -> None:
    ReviewQueueServiceMachine.stack = stack
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(ReviewQueueServiceMachine)
        run_state_machine_as_test(
            seeded,
            settings=settings(
                max_examples=SERVICE_EXAMPLES,
                stateful_step_count=12,
                deadline=None,
                database=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
            ),
        )
