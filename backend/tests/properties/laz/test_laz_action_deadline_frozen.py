"""El plazo de una acción queda congelado al asignarla (TASK-311; BR-LAZ-39; base de PR-LAZ-15).

Contra PostgreSQL 16 real como ``vigia_app``, con ``ActionService``, ``EvidenceService`` y
``LoopConfigurationService`` reales (``actions_stack``):

- **bordes** (BR-LAZ-39): con la planta en ``[5, 40]``, un plazo de 4 o de 41 días responde
  ``deadline_out_of_range`` sin escribir nada, y uno de 5 o de 40 se acepta; sin plazo, el de la
  planta (``action_deadline_default_days``);
- **congelado**: con ``lifecycle_command_sequences`` (la base que amplían TASK-343, 312 y 313),
  cada acción asignada guarda ``deadline_days`` y ``due_at`` —el fin del día hábil
  ``deadline_days`` posterior a la fecha local de ``assigned_at`` con el calendario **vigente al
  asignar**— y, después de **cualquier** comando siguiente (cambios del rango y del calendario por
  ``loop.config``, reasignaciones, evidencias, el reloj que avanza), su registro, su fila de
  ``loop.corrective_action`` y, mientras sea la vigente, la proyección conservan exactamente esos
  dos valores. Cada asignación y cada rechazo son los del oráculo (el rango y el calendario de la
  configuración leída antes del comando; ``transition_not_allowed`` sin acción vigente que
  sustituir o con una ya vigente sin ``supersedes_action_id``).

La parte de PR-LAZ-15 que depende de la tarea de vencidos (``evaluate_deadlines``) es de TASK-313.
Las propiedades con base llevan la marca ``integration`` (``propiedades-integracion`` del CI).
Solo datos generados. Un solo reloj: las marcas salen del reloj simulado del mundo (retro 15).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings

from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import (
    DESCRIPTION,
    EVIDENCE,
    ROLE,
    ActionsStack,
    Who,
    actions_stack,
)
from tests.properties.laz.strategies.lifecycle import (
    AddEvidence,
    Assign,
    ChangeDeadlineRange,
    LifecycleCommand,
    Reassign,
    Tick,
    lifecycle_command_sequences,
)
from vigia_platform.loop.application.actions import ActionRejected, AssignmentResult
from vigia_platform.loop.calendar import BusinessCalendar, business_deadline
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

EXAMPLES: Final = 15
"""Cada ejemplo ingresa y clasifica un hallazgo y escribe varias transacciones reales; el perfil
``nightly`` no sube este número."""
REASON: Final = "Ajuste de plazos acordado en el comité de seguridad"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "laz_action_deadline_frozen") as built:
        yield built


@pytest.fixture(scope="module")
def coordinator(stack: ActionsStack) -> Who:
    return stack.member(stack.world.a)


@pytest.fixture(scope="module")
def manager(stack: ActionsStack) -> Who:
    return stack.member(stack.world.a, Role.PLANT_MANAGER)


@pytest.fixture(scope="module")
def assignee(stack: ActionsStack) -> uuid.UUID:
    return stack.user(stack.world.a)


def _settings(stack: ActionsStack, manager: Who) -> LoopSettings:
    configuration = stack.run(
        stack.configuration.repository.read(stack.context(manager), stack.world.a.plant_id)
    )
    return LoopSettings.defaults() if configuration is None else configuration.settings


def _timezone(stack: ActionsStack) -> str:
    (row,) = stack.fetch(
        "SELECT timezone FROM identity.plant WHERE plant_id = $1", stack.world.a.plant_id
    )
    return str(row["timezone"])


def _change(
    stack: ActionsStack,
    manager: Who,
    low: int,
    default: int,
    high: int,
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5),
) -> None:
    stack.tick()
    integers = {
        "action_deadline_min_days": low,
        "action_deadline_default_days": default,
        "action_deadline_max_days": high,
    }
    # El orden min ≤ default ≤ max se comprueba sobre el resultado: se abre el rango primero.
    stack.run(
        stack.configuration.change(
            stack.context(manager),
            stack.world.a.plant_id,
            integers={"action_deadline_min_days": 1, "action_deadline_max_days": 180},
            business_calendar=None,
            reason=REASON,
        )
    )
    stack.run(
        stack.configuration.change(
            stack.context(manager),
            stack.world.a.plant_id,
            integers=integers,
            business_calendar={"working_weekdays": list(weekdays), "non_working_dates": []},
            reason=REASON,
        )
    )


def _assign(
    stack: ActionsStack,
    coordinator: Who,
    fsid: uuid.UUID,
    assignee: uuid.UUID,
    *,
    action_type: str = "engineering",
    deadline_days: int | None,
    supersedes: uuid.UUID | None = None,
) -> AssignmentResult | ActionRejected:
    stack.tick()
    try:
        result: AssignmentResult = stack.run(
            stack.actions.assign(
                stack.context(coordinator),
                fsid,
                action_type=action_type,
                action_description=DESCRIPTION,
                assignee_user_id=assignee,
                deadline_days=deadline_days,
                supersedes_action_id=supersedes,
            )
        )
    except ActionRejected as rejected:
        return rejected
    return result


# --- bordes ---------------------------------------------------------------------------------------


def test_deadline_borders_of_the_plant_range(
    stack: ActionsStack, coordinator: Who, manager: Who, assignee: uuid.UUID
) -> None:
    _change(stack, manager, 5, 10, 40)
    for days in (4, 41, 0, -1, 10**30):
        finding = stack.opened()
        before = stack.untouched(finding)
        response = stack.assign(
            coordinator, finding, stack.action_body(assignee, deadline_days=days)
        )
        assert (response.status_code, response.json().get("detail_code")) == (
            400,
            "loop_deadline_out_of_range",
        ), (days, response.text)
        assert stack.untouched(finding) == before
    for days, expected in ((5, 5), (40, 40), (None, 10)):
        finding = stack.opened()
        response = stack.assign(
            coordinator, finding, stack.action_body(assignee, deadline_days=days)
        )
        assert response.status_code == 201, response.text
        assert response.json()["deadline_days"] == expected
    _change(stack, manager, 1, 30, 180)


def test_a_configuration_change_moves_no_assigned_deadline(
    stack: ActionsStack, coordinator: Who, manager: Who, assignee: uuid.UUID
) -> None:
    _change(stack, manager, 1, 30, 180)
    finding = stack.opened()
    fsid = stack.finding_state_id(finding)
    result = _assign(stack, coordinator, fsid, assignee, deadline_days=40)
    assert isinstance(result, AssignmentResult)
    frozen = (result.due_at, result.deadline_days)
    # Un rango que ya no admite 40 y un calendario de tres días: lo asignado no se mueve.
    _change(stack, manager, 1, 5, 20, (1, 2, 3))
    (row,) = stack.action_rows(finding)
    (record,) = stack.records_of(finding, "action_assigned")
    projection = stack.world.finding_state(finding)
    assert projection is not None
    assert (row["due_at"], projection["due_at"]) == (frozen[0], frozen[0])
    assert (record["content"]["due_at"], record["content"]["deadline_days"]) == (
        format_timestamp(frozen[0]),
        40,
    )
    # La siguiente asignación sí usa la configuración nueva.
    other = stack.opened()
    rejected = _assign(
        stack, coordinator, stack.finding_state_id(other), assignee, deadline_days=40
    )
    assert isinstance(rejected, ActionRejected)
    assert rejected.detail_code is LoopDetailCode.DEADLINE_OUT_OF_RANGE
    _change(stack, manager, 1, 30, 180)


# --- propiedad con estado -------------------------------------------------------------------------


@dataclass
class _Model:
    """Lo que la secuencia ya fijó: por acción, ``(due_at, deadline_days)``."""

    frozen: dict[uuid.UUID, tuple[datetime, int]] = field(default_factory=dict)
    current: uuid.UUID | None = None
    action_type: str = "engineering"


def _expected_deadline(
    stack: ActionsStack, manager: Who, days: int | None
) -> tuple[int, BusinessCalendar] | None:
    settings = _settings(stack, manager)
    chosen = settings["action_deadline_default_days"] if days is None else days
    low, high = settings["action_deadline_min_days"], settings["action_deadline_max_days"]
    return (chosen, settings.business_calendar) if low <= chosen <= high else None


def _check_frozen(stack: ActionsStack, finding: Any, model: _Model) -> None:
    rows = {row["action_id"]: row for row in stack.action_rows(finding)}
    records = {
        uuid.UUID(r["content"]["action_id"]): r["content"]
        for r in stack.records_of(finding, "action_assigned")
    }
    assert set(rows) == set(records) == set(model.frozen)
    for action_id, (due_at, days) in model.frozen.items():
        assert rows[action_id]["due_at"] == due_at
        assert (records[action_id]["due_at"], records[action_id]["deadline_days"]) == (
            format_timestamp(due_at),
            days,
        )
    projection = stack.world.finding_state(finding)
    assert projection is not None
    if model.current is not None:
        assert projection["current_action_id"] == model.current
        assert projection["due_at"] == model.frozen[model.current][0]


def _assignment(
    stack: ActionsStack,
    coordinator: Who,
    manager: Who,
    assignee: uuid.UUID,
    finding: Any,
    model: _Model,
    action_type: str,
    days: int | None,
    reassign: bool = False,
) -> None:
    """Asignar (T07) o reasignar la vigente, contra el oráculo de la configuración vigente."""
    expected = _expected_deadline(stack, manager, days)
    supersedes = (model.current or uuid.uuid4()) if reassign else None
    result = _assign(
        stack,
        coordinator,
        stack.finding_state_id(finding),
        assignee,
        action_type=action_type,
        deadline_days=days,
        supersedes=supersedes,
    )
    if (model.current is None) == reassign:
        # Reasignar sin acción vigente, o asignar con una ya vigente: no hay transición.
        assert isinstance(result, ActionRejected), result
        assert result.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
        return
    if expected is None:
        assert isinstance(result, ActionRejected), result
        assert result.detail_code is LoopDetailCode.DEADLINE_OUT_OF_RANGE
        return
    assert isinstance(result, AssignmentResult), result
    chosen_days, calendar = expected
    due_at = business_deadline(result.assigned_at, chosen_days, calendar, _timezone(stack))
    assert (result.deadline_days, result.due_at) == (chosen_days, due_at)
    model.frozen[result.action_id] = (due_at, chosen_days)
    model.current, model.action_type = result.action_id, action_type


def _apply(
    stack: ActionsStack,
    coordinator: Who,
    manager: Who,
    assignee: uuid.UUID,
    finding: Any,
    model: _Model,
    command: LifecycleCommand,
) -> None:
    match command:
        case Assign(action_type=action_type, deadline_days=days):
            _assignment(stack, coordinator, manager, assignee, finding, model, action_type, days)
        case Reassign(deadline_days=days):
            _assignment(
                stack, coordinator, manager, assignee, finding, model, model.action_type, days, True
            )
        case AddEvidence():
            if model.current is None:
                return
            stack.tick()
            body = stack.evidence_body(description=EVIDENCE, performed_by_role=ROLE)
            assert stack.add_evidence(coordinator, model.current, body).status_code == 201
        case ChangeDeadlineRange(
            min_days=low, default_days=default, max_days=high, working_weekdays=weekdays
        ):
            _change(stack, manager, low, default, high, weekdays)
        case Tick(seconds=seconds):
            stack.tick(seconds)


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(commands=lifecycle_command_sequences())
def test_an_assigned_deadline_never_moves_after_any_command(
    stack: ActionsStack,
    coordinator: Who,
    manager: Who,
    assignee: uuid.UUID,
    commands: list[LifecycleCommand],
) -> None:
    finding = stack.opened()
    model = _Model()
    for command in commands:
        # Sesiones nuevas en cada comando: el reloj avanza días y una sesión caduca.
        coordinator, manager = (
            stack.member(stack.world.a),
            stack.member(stack.world.a, Role.PLANT_MANAGER),
        )
        _apply(stack, coordinator, manager, assignee, finding, model, command)
        _check_frozen(stack, finding, model)
    stack.world.stack.run(stack.world.stack.release())
