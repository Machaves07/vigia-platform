"""PR-LAZ-15 (TASK-313 completa la base de TASK-311): el plazo de una acción conserva su valor de
creación con cambios de configuración intercalados (BR-LAZ-39, 144 y 145; DE §2.5 y §2.13).

Contra PostgreSQL 16 real como ``vigia_app``, con los servicios y las rutas reales del lazo y la
tarea real ``evaluate_deadlines`` (``deadlines_stack``), sobre ``deadline_command_sequences``:
asignaciones, reasignaciones, reaperturas, cambios del **rango de plazos y del calendario** de la
planta y del **umbral del gerente**, saltos de reloj de días y la tarea intercalada. Tras **cada**
comando:

- toda acción nacida en la secuencia tiene el ``due_at`` del oráculo **en el momento de crearla**:
  el fin del día hábil ``deadline_days`` posterior a la fecha local de ``assigned_at`` con el
  calendario **vigente antes del comando** que la creó;
- ese valor no cambia nunca después: ni en el registro ``action_assigned`` ni en
  ``loop.corrective_action`` ni, mientras sea la vigente, en la proyección;
- cada ``action_overdue`` de la acción lleva **ese** ``due_at`` y su clave es
  ``UUID v5 (action_id, due_at, level)`` con él: un cambio de configuración no mueve un vencimiento
  ya declarado ni crea otra clave.

La fecha de revisión de una declaración de incerrable (la otra mitad de PR-LAZ-15) es de TASK-312.
Las propiedades con base llevan la marca ``integration``. Solo datos generados. Un solo reloj.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from tests.properties.laz.strategies.lifecycle import (
    ChangeHighlightDays,
    EvaluateDeadlines,
    LifecycleCommand,
    Reassign,
    Tick,
    deadline_command_sequences,
    deadline_ranges,
)
from vigia_platform.loop.application.deadlines import overdue_id
from vigia_platform.loop.calendar import BusinessCalendar, business_deadline
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.domain.enums import EscalationLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

EXAMPLES: Final = 12
"""Cada ejemplo ingresa y clasifica un hallazgo y escribe varias transacciones reales; el perfil
``nightly`` no sube este número."""
DAY: Final = timedelta(days=1)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "pr_laz_15_frozen_deadlines") as built:
        yield built


def _calendar(stack: DeadlinesStack, organization: LoopOrganization) -> BusinessCalendar:
    configuration = stack.run(
        stack.actions.configuration.repository.read(
            stack.actions.context(stack.manager(organization)), organization.plant_id
        )
    )
    settings_ = LoopSettings.defaults() if configuration is None else configuration.settings
    return settings_.business_calendar


def _timezone(stack: DeadlinesStack, organization: LoopOrganization) -> str:
    (row,) = stack.fetch(
        "SELECT timezone FROM identity.plant WHERE plant_id = $1", organization.plant_id
    )
    return str(row["timezone"])


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _check(
    stack: DeadlinesStack,
    finding: IngestedSubject,
    frozen: dict[uuid.UUID, datetime],
    calendar_before: BusinessCalendar,
    timezone: str,
) -> None:
    records = {
        uuid.UUID(r["content"]["action_id"]): r["content"]
        for r in stack.actions.records_of(finding, "action_assigned")
    }
    rows = {row["action_id"]: row for row in stack.actions.action_rows(finding)}
    assert set(records) == set(rows)
    for action_id, content in records.items():
        if action_id not in frozen:
            # Nació en este comando: el oráculo con el calendario vigente antes de él.
            due_at = business_deadline(
                _instant(content["assigned_at"]),
                int(content["deadline_days"]),
                calendar_before,
                timezone,
            )
            assert _instant(content["due_at"]) == due_at, (content, calendar_before)
            frozen[action_id] = due_at
        assert _instant(content["due_at"]) == frozen[action_id]
        assert rows[action_id]["due_at"] == frozen[action_id]
    assert set(frozen) == set(records)
    projection = stack.actions.world.finding_state(finding)
    assert projection is not None
    current = projection["current_action_id"]
    if current is not None and current in frozen and projection["due_at"] is not None:
        assert projection["due_at"] == frozen[current]
    for record in stack.overdue_records(finding):
        content = record["content"]
        action_id = uuid.UUID(content["action_id"])
        level = EscalationLevel(content["escalation_level"])
        assert content["due_at"] == format_timestamp(frozen[action_id])
        assert content["overdue_id"] == str(overdue_id(action_id, frozen[action_id], level))


_FOCUSED: Final = st.lists(
    st.one_of(
        st.builds(Reassign, st.one_of(st.none(), st.integers(1, 5))),
        deadline_ranges(),
        st.builds(ChangeHighlightDays, st.integers(1, 5)),
        st.builds(Tick, st.integers(3600, 6 * 24 * 3600)),
        st.just(EvaluateDeadlines()),
    ),
    max_size=10,
)
"""Lo que mueve un plazo si algo lo moviera: reasignar, cambiar rango, calendario y umbral, saltar
días y declarar vencimientos."""


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(commands=st.one_of(deadline_command_sequences(), _FOCUSED))
def test_pr_laz_15_a_deadline_keeps_its_creation_value_across_configuration_changes(
    stack: DeadlinesStack, commands: list[LifecycleCommand]
) -> None:
    organization = stack.actions.world.organization()  # sin la configuración de otro ejemplo
    timezone = _timezone(stack, organization)
    calendar = _calendar(stack, organization)
    finding, _, due_at = stack.with_action(
        organization, stack.actions.member(organization), stack.assignee(organization)
    )
    frozen: dict[uuid.UUID, datetime] = {}
    _check(stack, finding, frozen, calendar, timezone)
    stack.tick_to(due_at - timedelta(hours=1))  # que el vencimiento caiga dentro de la secuencia
    declared = 0
    for command in commands:
        calendar_before = _calendar(stack, organization)
        written = stack.apply(organization, finding, command)
        declared += written.count("action_overdue")
        _check(stack, finding, frozen, calendar_before, timezone)
    # Al final, la tarea tras una semana: declara con el plazo de creación de la vigente.
    calendar_before = _calendar(stack, organization)
    stack.tick(7 * DAY.total_seconds())
    declared += stack.apply(organization, finding, EvaluateDeadlines()).count("action_overdue")
    _check(stack, finding, frozen, calendar_before, timezone)
    event(f"acciones: {min(len(frozen), 3)}")
    event(f"vencimientos declarados: {min(declared, 2)}")
    stack.actions.world.stack.run(stack.actions.world.stack.release())


def test_pr_laz_15_a_declared_deadline_does_not_move_when_the_calendar_shrinks(
    stack: DeadlinesStack,
) -> None:
    """De ejemplo: vencida y declarada con lunes a viernes, la planta pasa a tres días hábiles y a
    un rango que ya no admite el plazo; el vencimiento declarado, su clave y la siguiente
    declaración (el escalamiento) siguen con el ``due_at`` de creación."""
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 2)
    coordinator, assignee = stack.actions.member(organization), stack.assignee(organization)
    finding, action_id, due_at = stack.with_action(organization, coordinator, assignee, 5)
    stack.tick_to(due_at)
    (first,) = stack.evaluate(organization)
    stack.change_deadline_range(organization, 1, 2, 3, (1, 2, 3))
    stack.set_highlight_days(organization, 1)
    assert first.declared_at == due_at
    stack.tick_to(due_at + DAY)

    (second,) = stack.evaluate(organization)

    contents: list[dict[str, Any]] = [r["content"] for r in stack.overdue_records(finding)]
    assert [c["due_at"] for c in contents] == [format_timestamp(due_at)] * 2
    assert second.overdue_id == overdue_id(action_id, due_at, EscalationLevel.MANAGER_WEEKLY)
    (row,) = stack.actions.action_rows(finding)
    assert row["due_at"] == due_at
