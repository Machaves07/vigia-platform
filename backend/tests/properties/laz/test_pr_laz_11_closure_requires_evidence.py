"""PR-LAZ-11 (TASK-343, VIG-233): ningún hallazgo cerrado sin evidencia ni sin firmante con
licencia (F02 a F04, BR-LAZ-36, 42 y 43; H-25).

Contra PostgreSQL 16 real como ``vigia_app``, con ``ActionService``, ``EvidenceService``,
``ClosureService`` y ``ReopeningService`` reales (``actions_stack``): a lo largo de las
secuencias generadas por ``lifecycle_command_sequences`` (ahora con ``CloseFinding`` y
``ReopenFinding``, TASK-343), tras **cada** comando se comprueba el invariante directamente
contra la proyección y el expediente: si ``loop.finding_state.lifecycle_state`` es ``closed``,
su registro ``closure`` tiene **al menos una** entrada en ``evidence_entry_ids`` y un
``signer.professional_license`` no vacío.

El modelo no intenta predecir cada rechazo (eso ya lo hace ``test_laz_action_deadline_frozen``
para el plazo): aplica cada comando por el servicio real, sigue el resultado (éxito o rechazo) y
comprueba el invariante **después de cada paso**, con independencia de qué combinación de
comandos produjo el estado. Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    CloseFinding,
    LifecycleCommand,
    Reassign,
    ReopenFinding,
    Tick,
    lifecycle_command_sequences,
)

pytestmark = pytest.mark.integration

EXAMPLES: Final = 12
"""Cada ejemplo ingresa y clasifica un hallazgo y escribe varias transacciones reales."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "pr_laz_11_closure_requires_evidence") as built:
        yield built


def _invariant(stack: ActionsStack, finding: IngestedSubject) -> None:
    """Si el hallazgo está cerrado, su registro ``closure`` tiene evidencia y licencia."""
    row = stack.world.finding_state(finding)
    assert row is not None
    if row["lifecycle_state"] != "closed":
        return
    closure_id = row["closure_record_id"]
    assert closure_id is not None, "closed sin closure_record_id"
    matches = [r for r in stack.records_of(finding, "closure") if r["record_id"] == closure_id]
    assert len(matches) == 1, "closure_record_id no apunta a un registro de cierre del sujeto"
    content = matches[0]["content"]
    assert len(content["evidence_entry_ids"]) >= 1, "cerrado sin ninguna entrada de evidencia"
    assert content["signer"].get("professional_license"), "cerrado sin licencia del firmante"


def _apply(
    stack: ActionsStack,
    coordinator: Who,
    assignee: uuid.UUID,
    finding: IngestedSubject,
    model: dict[str, Any],
    command: LifecycleCommand,
) -> None:
    match command:
        case Assign(action_type=action_type, deadline_days=days):
            if model["current"] is not None:
                return
            response = stack.assign(
                coordinator,
                finding,
                stack.action_body(assignee, action_type=action_type, deadline_days=days),
            )
            if response.status_code == 201:
                model["current"] = uuid.UUID(response.json()["action_id"])
        case Reassign(deadline_days=days):
            if model["current"] is None:
                return
            response = stack.assign(
                coordinator,
                finding,
                stack.action_body(
                    assignee, deadline_days=days, supersedes_action_id=str(model["current"])
                ),
            )
            if response.status_code == 201:
                model["current"] = uuid.UUID(response.json()["action_id"])
        case AddEvidence():
            if model["current"] is None:
                return
            response = stack.add_evidence(coordinator, model["current"], stack.evidence_body())
            if response.status_code != 201:
                return
        case Tick(seconds=seconds):
            stack.tick(seconds)
        case CloseFinding():
            response = stack.close(coordinator, finding, stack.closure_body())
            if response.status_code == 201:
                model["current"] = None
        case ReopenFinding(action_type=action_type, deadline_days=days):
            response = stack.reopen(
                coordinator,
                finding,
                stack.reopening_body(assignee, action_type=action_type, deadline_days=days),
            )
            if response.status_code == 201:
                model["current"] = uuid.UUID(response.json()["action_id"])


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(commands=lifecycle_command_sequences())
def test_closed_finding_always_has_evidence_and_licensed_signer(
    stack: ActionsStack, commands: list[LifecycleCommand]
) -> None:
    coordinator = stack.member(stack.world.a)
    assignee = stack.user(stack.world.a)
    finding = stack.opened()
    model: dict[str, Any] = {"current": None}
    _invariant(stack, finding)
    for command in commands:
        _apply(stack, coordinator, assignee, finding, model, command)
        _invariant(stack, finding)
    stack.world.stack.run(stack.world.stack.release())
