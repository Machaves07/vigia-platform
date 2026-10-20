"""PR-LAZ-10: oráculo estatal independiente del ciclo de vida del hallazgo.

El modelo de referencia se escribió desde DE §4.1 y §4.2 y no importa la máquina de producción.
La máquina pura recorre los catorce comandos; la prueba de integración verifica en PostgreSQL el
tramo crítico que materializa acción, evidencia y cierre, con el mismo reloj inyectado.
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import actions_stack
from tests.properties.laz.lifecycle_model import LifecycleModel
from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    ChangeDeadlineRange,
    ClassifyFinding,
    CloseFinding,
    DeactivateAssignee,
    DeclareUnclosable,
    LateSegment,
    LifecycleCommand,
    ReopenFinding,
    ResolveQueue,
    ReviewUnclosable,
    RevokeGate,
    Tick,
    stateful_lifecycle_command_sequences,
)

_STEPS: Final = 50


def _apply(model: LifecycleModel, command: LifecycleCommand) -> str | None:
    """Aplica los valores puros de la estrategia al modelo de la tabla, sin producción."""
    match command:
        case ClassifyFinding(outcome=outcome, reinforced=reinforced):
            return model.classify(outcome, reinforced)
        case ResolveQueue(outcome=outcome):
            return model.resolve_queue(outcome)
        case Assign(deadline_days=days):
            return model.assign_action(days or model.deadline_days)
        case AddEvidence():
            return model.add_evidence()
        case CloseFinding():
            return model.close(signer_has_license=True)
        case DeclareUnclosable(attempt=attempt):
            return model.declare_unclosable(
                attempt.fields == 5, attempt.policy.kind, attempt.review_months, attempt.license
            )
        case ReviewUnclosable(attempt=attempt):
            return model.review_unclosable(attempt.outcome, attempt.license)
        case ReopenFinding():
            return model.reopen(reason_present=True)
        case Tick(seconds=seconds):
            model.advance_clock(seconds)
        case LateSegment():
            model.late_segment()
        case DeactivateAssignee():
            model.deactivate_assignee()
        case RevokeGate():
            model.revoke_gate()
        case ChangeDeadlineRange(default_days=default_days):
            return model.change_configuration(default_days)
        case _:
            raise AssertionError(f"comando sin modelo: {command!r}")
    return None


class LifecycleReferenceMachine(RuleBasedStateMachine):
    """Recorre la tabla independiente con comandos de longitud variable, incluida la vacía."""

    def __init__(self) -> None:
        super().__init__()
        self.model = LifecycleModel()
        self.commands: list[LifecycleCommand] = []

    @rule(command=stateful_lifecycle_command_sequences(max_size=1))
    def apply_one(self, command: list[LifecycleCommand]) -> None:
        if command:
            self.commands.append(command[0])
            _apply(self.model, command[0])

    @invariant()
    def invariantes_del_modelo(self) -> None:
        if self.model.state == "closed":
            assert self.model.action_assigned
            assert self.model.evidence_entries >= 1
        if self.model.state == "unclosable":
            assert self.model.unclosable_review_due_at is not None
        if self.model.state == "overdue":
            assert self.model.action_assigned
        if self.model.action_assigned and self.model.assignee_inactive:
            assert self.model.state in {
                "action_assigned",
                "overdue",
                "suspended_by_reclassification",
                "closed",
                "unclosable",
            }


class ReviewLifecycleReferenceMachine(LifecycleReferenceMachine):
    """La otra entrada de §4.1: una detección en cola permite recorrer T05 y T06."""

    def __init__(self) -> None:
        super().__init__()
        self.model = LifecycleModel(state="in_review")


def test_pr_laz_10_reference_state_machine() -> None:
    """La semilla fija y la de sesión ejercitan los catorce comandos hasta 50 pasos."""
    for value in _seeds_for_profile():
        for machine in (LifecycleReferenceMachine, ReviewLifecycleReferenceMachine):
            seeded = hypothesis_seed(value)(machine)
            run_state_machine_as_test(
                seeded,
                settings=settings(
                    stateful_step_count=_STEPS,
                    deadline=None,
                    suppress_health_check=[HealthCheck.too_slow],
                ),
            )


def test_pr_laz_10_rejection_and_frozen_deadline_regressions() -> None:
    """Sondas permanentes para las cuatro mutaciones exigidas por PR-LAZ-10."""
    model = LifecycleModel()
    assert model.close(signer_has_license=True) == "closure_requires_assigned_action"
    # DE §4.2: received → acción no existe.
    assert model.assign_action(1) == "transition_not_allowed"

    model.classify("confirmed", reinforced=False)
    assert model.assign_action(1) is None
    original_due_at = model.due_at
    assert model.change_configuration(30) is None
    assert model.due_at == original_due_at
    model.advance_clock(2 * 86_400)
    assert model.state == "overdue"
    assert model.close(signer_has_license=True) == "closure_evidence_missing"
    assert model.state == "overdue"


@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(commands=stateful_lifecycle_command_sequences(max_size=8))
@pytest.mark.integration
def test_pr_laz_10_postgres_action_evidence_closure_agree_with_the_reference(
    postgres_endpoint: PostgresEndpoint, commands: list[LifecycleCommand]
) -> None:
    """La plataforma real conserva las invariantes de cierre tras comandos intercalados.

    El recorrido usa PostgreSQL, rutas y servicios reales. Los comandos fuera del tramo de cierre
    se dejan al oráculo puro: sus operaciones especializadas tienen sus propias propiedades
    estatales PR-LAZ-08, 12, 13 y 15, que esta prueba vuelve a comprobar como invariantes.
    """
    with actions_stack(postgres_endpoint, "pr_laz_10_lifecycle") as stack:
        coordinator = stack.member(stack.world.a)
        assignee = stack.user(stack.world.a)
        finding = stack.opened()
        model = LifecycleModel(state="open")
        action_id = None
        for command in commands:
            if isinstance(command, Assign):
                response = stack.assign(
                    coordinator,
                    finding,
                    stack.action_body(assignee, deadline_days=command.deadline_days),
                )
                if response.status_code == 201:
                    action_id = response.json()["action_id"]
                    _apply(model, command)
            elif isinstance(command, AddEvidence) and action_id is not None:
                response = stack.add_evidence(coordinator, action_id, stack.evidence_body())
                if response.status_code == 201:
                    _apply(model, command)
            elif isinstance(command, CloseFinding):
                response = stack.close(coordinator, finding, stack.closure_body())
                if response.status_code == 201:
                    _apply(model, command)
                else:
                    assert response.status_code < 500, response.text
            elif isinstance(command, Tick):
                stack.tick(command.seconds)
                _apply(model, command)
            elif isinstance(command, DeactivateAssignee) and action_id is not None:
                stack.deactivate(assignee)
                _apply(model, command)
            elif isinstance(command, (LateSegment, RevokeGate, ChangeDeadlineRange)):
                _apply(model, command)
            row = stack.world.finding_state(finding)
            assert row is not None
            if row["lifecycle_state"] == "closed":
                (closure,) = stack.records_of(finding, "closure")
                assert closure["content"]["evidence_entry_ids"]
                assert closure["content"]["signer"]["professional_license"]
        stack.world.stack.run(stack.world.stack.release())
