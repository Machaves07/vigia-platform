"""PR-LAZ-13 (TASK-313): lo vencido no se oculta ni es terminal (BR-LAZ-41, 46, 47 y 49; P5; H-27).

Contra PostgreSQL 16 real como ``vigia_app``, con la tarea real ``evaluate_deadlines``, los
servicios y las rutas reales del lazo (``deadlines_stack``) y la secuencia generada por
``deadline_command_sequences`` (la base de TASK-311 y TASK-343 con la tarea intercalada, saltos de
reloj de días, reclasificaciones que suspenden y reanudan, el umbral del gerente y la declaración
de incerrable):

- tras **cada** comando, si el hallazgo está ``overdue``: sigue en la **bandeja**
  (``InboxService``, el estado ``overdue`` de un coordinador de la organización), sigue en el
  **panel de vencidos** de la planta, y la máquina de estados le admite **cierre** (T09),
  **incerrable** (T10) y **reclasificación** (T15);
- la tarea (el único comando del sistema de la secuencia) **solo declara**: deja el estado igual o
  lo pasa de ``action_assigned`` a ``overdue``, no retira la fila y no escribe otra cosa que
  ``action_overdue`` (``DeadlinesStack.apply``); ninguna respuesta es un 5xx.

Además, de ejemplo: desde ``overdue`` el cierre firmado, la declaración de incerrable y la
reclasificación funcionan de verdad, y la tarea no mueve después ninguno de esos estados; un
responsable desactivado no cambia el estado de la acción (la tarea la declara igual).

Las propiedades con base llevan la marca ``integration``. Solo datos generados. Un solo reloj.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from tests.properties.laz.strategies.lifecycle import (
    LifecycleCommand,
    deadline_command_sequences,
)
from vigia_platform.loop.adapters.postgres.inbox_query import InboxFilters
from vigia_platform.loop.domain.enums import ClassificationOutcome, FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import (
    Classify,
    Close,
    DeclareUnclosable,
    LifecycleSnapshot,
    PolicyStatus,
    Rejected,
    transition,
)

pytestmark = pytest.mark.integration

EXAMPLES: Final = 10
"""Cada ejemplo ingresa, clasifica y asigna un hallazgo y escribe varias transacciones reales; el
perfil ``nightly`` no sube este número."""
DAY: Final = timedelta(days=1)
S = FindingLifecycleState


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "pr_laz_13_overdue_not_hidden") as built:
        yield built


def _in_inbox(stack: DeadlinesStack, organization: LoopOrganization, fsid: uuid.UUID) -> bool:
    context = stack.actions.context(stack.actions.member(organization))
    filters = InboxFilters(zone_ids=(organization.zone_id,), lifecycle_states=(S.OVERDUE,))
    after = None
    while True:
        page = stack.run(stack.inbox.page(context, filters=filters, after=after))
        if any(item.finding_state_id == fsid for item in page.items):
            return True
        if page.next_cursor is None:
            return False
        after = page.next_cursor


def _in_panel(stack: DeadlinesStack, organization: LoopOrganization, fsid: uuid.UUID) -> bool:
    manager = stack.manager(organization)
    params: dict[str, object] = {"plant_id": str(organization.plant_id)}
    while True:
        response = stack.get_overdue(manager, params)
        assert response.status_code == 200, response.text
        page = response.json()
        if any(item["finding_state_id"] == str(fsid) for item in page["items"]):
            return True
        if page["next_cursor"] is None:
            return False
        params = {**params, "cursor": page["next_cursor"]}


def _invariant(
    stack: DeadlinesStack, organization: LoopOrganization, finding: IngestedSubject
) -> bool:
    """El invariante de PR-LAZ-13; ``True`` si el hallazgo está ``overdue`` (lo comprobó)."""
    row = stack.actions.world.finding_state(finding)
    assert row is not None, "la fila del hallazgo desapareció"
    if row["lifecycle_state"] != S.OVERDUE.value:
        return False
    fsid = uuid.UUID(str(row["finding_state_id"]))
    assert _in_inbox(stack, organization, fsid), "un overdue no sale en la bandeja"
    assert _in_panel(stack, organization, fsid), "un overdue no sale en el panel de vencidos"
    snapshot = LifecycleSnapshot(S.OVERDUE, None, row["due_at"])
    admitted = (
        Close(evidence_entry_count=1, signer_has_license=True),
        DeclareUnclosable(
            fields_complete=True,
            policy=PolicyStatus.LOADED,
            review_date_in_range=True,
            signer_has_license=True,
        ),
        Classify(ClassificationOutcome.FALSE_POSITIVE, True, stack.now()),
    )
    for command in admitted:
        assert not isinstance(transition(snapshot, command), Rejected), command
    return True


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(commands=deadline_command_sequences())
def test_pr_laz_13_an_overdue_finding_stays_reachable_and_manageable(
    stack: DeadlinesStack, commands: list[LifecycleCommand]
) -> None:
    organization = stack.actions.world.organization()  # sin la configuración de otro ejemplo
    finding, _, due_at = stack.with_action(
        organization, stack.actions.member(organization), stack.assignee(organization)
    )
    # Que el vencimiento sea alcanzable dentro de la secuencia.
    stack.tick_to(due_at - timedelta(hours=1))
    reached = False
    for command in commands:
        stack.apply(organization, finding, command)
        reached = _invariant(stack, organization, finding) or reached
    stack.tick(DAY.total_seconds() * 3)
    stack.evaluate(organization)
    reached = _invariant(stack, organization, finding) or reached
    event(f"overdue alcanzado: {reached}")
    event(f"estado final: {stack.state(finding)}")
    stack.actions.world.stack.run(stack.actions.world.stack.release())


# --- de ejemplo: cada salida desde overdue --------------------------------------------------------


def _overdue(
    stack: DeadlinesStack, organization: LoopOrganization
) -> tuple[IngestedSubject, uuid.UUID]:
    coordinator = stack.actions.member(organization)
    finding, action_id, due_at = stack.with_action(
        organization, coordinator, stack.assignee(organization)
    )
    response = stack.actions.add_evidence(coordinator, action_id, stack.actions.evidence_body())
    assert response.status_code == 201, response.text
    stack.tick_to(due_at)
    stack.evaluate(organization)
    assert stack.state(finding) == "overdue"
    return finding, action_id


def test_pr_laz_13_from_overdue_the_closure_the_unclosable_and_the_reclassification_work(
    stack: DeadlinesStack,
) -> None:
    organization = stack.actions.world.organization()
    closed, _ = _overdue(stack, organization)
    response = stack.actions.close(
        stack.actions.member(organization), closed, stack.actions.closure_body()
    )
    assert response.status_code == 201, response.text

    unclosable, _ = _overdue(stack, organization)
    stack.actions.write_unclosable(unclosable)

    suspended, _ = _overdue(stack, organization)
    stack.actions.classify(suspended, "false_positive", "no_person_present", reinforced=True)

    states = [stack.state(f) for f in (closed, unclosable, suspended)]
    assert states == ["closed", "unclosable", "suspended_by_reclassification"]
    stack.tick(40 * DAY.total_seconds())
    assert stack.evaluate(organization) == ()
    assert [stack.state(f) for f in (closed, unclosable, suspended)] == states

    # Reanudado (T16), vuelve a overdue con el plazo original y sigue siendo gestionable.
    stack.actions.classify(suspended, "confirmed", "real_coexistence", reinforced=True)
    assert stack.state(suspended) == "overdue"


def test_pr_laz_13_a_deactivated_assignee_does_not_change_the_action_state(
    stack: DeadlinesStack,
) -> None:
    organization = stack.actions.world.organization()
    coordinator = stack.actions.member(organization)
    assignee = stack.assignee(organization)
    finding, _, due_at = stack.with_action(organization, coordinator, assignee)
    stack.actions.deactivate(assignee)
    stack.tick_to(due_at)

    (declared,) = stack.evaluate(organization)

    assert declared.transition_id == "T08"
    assert stack.state(finding) == "overdue"
    (row,) = stack.actions.action_rows(finding)
    assert row["state"] == "overdue"
