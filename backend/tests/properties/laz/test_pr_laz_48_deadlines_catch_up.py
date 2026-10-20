"""PR-LAZ-48 (parte de TASK-313): ``evaluate_deadlines`` se pone al día sin duplicar ni saltarse
días (NFR-LAZ-13 y 38; BR-LAZ-46; nota U04-H-07; FS-LAZ-10).

Contra PostgreSQL 16 real como ``vigia_app``, con la tarea real en el contexto de iteración
periódica del planificador y los servicios reales del lazo (``deadlines_stack``):

- **Caída de 48 h** (propiedad): para acciones con plazos y umbrales generados, una organización
  donde la tarea corre **cada hora** y otra, con las mismas acciones a la vez, donde la tarea
  **no corre durante 48 h** (desde un instante generado) y después sigue cada hora, terminan con
  **exactamente los mismos registros** ``action_overdue``: el mismo nivel, ``due_at``,
  ``declared_at`` y ``days_overdue`` por acción, uno por clave, cada vencimiento con la marca de su
  propio día (no la de la vuelta) y la clave ``UUID v5 (action_id, due_at, level)``.
- **Dos ejecuciones a la vez** (propiedad, tabla de garantías del PR): con el arrendamiento vencido
  mientras la primera sigue viva, la segunda se detiene en la exclusión de los hallazgos y, al
  releer, no escribe nada: un registro por clave y ningún fallo.
- **Escalamiento** (U04-H-07): el paso de ``coordinator`` a ``manager_weekly`` escribe un segundo
  registro con su propia clave; con la clave de BR-LAZ-46 sin el nivel, el mismo contenido del
  segundo nivel choca con el primero (``idempotency_conflict`` del escritor): por eso el nivel
  entra en la clave.

Las propiedades con base llevan la marca ``integration`` (reparto de ``propiedades-integracion``).
Solo datos generados. Un solo reloj (retro 15): las dos organizaciones y la tarea usan el reloj
simulado del mundo; nada se compara con la hora de la base.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.loop.application.deadlines import DeadlineEvaluator, overdue_id
from vigia_platform.loop.domain.enums import EscalationLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

EXAMPLES: Final = 6
"""Cada ejemplo crea dos organizaciones con sus acciones y corre la tarea cientos de veces; el
perfil ``nightly`` no sube este número."""
CONCURRENT_EXAMPLES: Final = 8
HOUR: Final = timedelta(hours=1)
DAY: Final = timedelta(days=1)
OUTAGE: Final = timedelta(hours=48)
LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver a la otra ejecución esperando un candado (no decide el resultado)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "pr_laz_48_deadlines_catch_up") as built:
        yield built


@dataclass(frozen=True)
class Plan:
    deadline_days: tuple[int, ...]
    """El plazo de cada acción, en días hábiles."""
    highlight_days: int
    """``manager_highlight_overdue_days`` de las dos plantas."""
    outage_after_hours: int
    """Horas, desde la víspera del primer plazo, hasta que la tarea deja de correr 48 h."""


plans = st.builds(
    Plan,
    st.lists(st.integers(1, 3), min_size=1, max_size=3).map(tuple),
    st.integers(1, 3),
    st.integers(0, 96),
)


def _local_hour(stack: DeadlinesStack, organization: LoopOrganization) -> int:
    (row,) = stack.fetch(
        "SELECT timezone FROM identity.plant WHERE plant_id = $1", organization.plant_id
    )
    return stack.now().astimezone(ZoneInfo(str(row["timezone"]))).hour


def _normalized(
    stack: DeadlinesStack, organization: LoopOrganization, actions: list[uuid.UUID]
) -> list[tuple[int, str, str, str, int]]:
    """``(índice de la acción, nivel, due_at, declared_at, days_overdue)`` de cada registro, con la
    clave comprobada contra ``UUID v5 (action_id, due_at, level)``."""
    out = []
    for content in stack.organization_overdue(organization):
        action_id = uuid.UUID(content["action_id"])
        level = EscalationLevel(content["escalation_level"])
        due_at = datetime.fromisoformat(content["due_at"].replace("Z", "+00:00"))
        assert content["overdue_id"] == str(overdue_id(action_id, due_at, level))
        out.append(
            (
                actions.index(action_id),
                level.value,
                content["due_at"],
                content["declared_at"],
                int(content["days_overdue"]),
            )
        )
    return sorted(out)


def _expected(
    deadlines: list[datetime], highlight_days: int
) -> list[tuple[int, str, str, str, int]]:
    """Lo que la ejecución continua debe dejar: los dos niveles de cada acción, cada uno con la
    marca del instante en que se alcanzó."""
    out = []
    for index, due_at in enumerate(deadlines):
        out.append((index, "coordinator", format_timestamp(due_at), format_timestamp(due_at), 0))
        escalated = due_at + highlight_days * DAY
        out.append(
            (
                index,
                "manager_weekly",
                format_timestamp(due_at),
                format_timestamp(escalated),
                highlight_days,
            )
        )
    return sorted(out)


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plan=plans)
def test_pr_laz_48_a_48_hour_outage_writes_exactly_what_the_hourly_run_writes(
    stack: DeadlinesStack, plan: Plan
) -> None:
    world = stack.actions.world
    continuous, outage = world.organization(), world.organization()
    for organization in (continuous, outage):
        stack.set_highlight_days(organization, plan.highlight_days)
    if _local_hour(stack, continuous) >= 21:
        stack.tick(5 * 3600)  # las dos acciones de cada par en el mismo día local
    people = {
        o.organization_id: (stack.actions.member(o), stack.actions.user(o))
        for o in (continuous, outage)
    }
    ids: dict[uuid.UUID, list[uuid.UUID]] = {
        continuous.organization_id: [],
        outage.organization_id: [],
    }
    deadlines: list[datetime] = []
    for days in plan.deadline_days:
        dues = []
        for organization in (continuous, outage):
            coordinator, assignee = people[organization.organization_id]
            _, action_id, due_at = stack.with_action(organization, coordinator, assignee, days)
            ids[organization.organization_id].append(action_id)
            dues.append(due_at)
        assert dues[0] == dues[1]
        deadlines.append(dues[0])

    start = min(deadlines) - HOUR
    end = max(deadlines) + plan.highlight_days * DAY + 2 * HOUR
    stack.tick_to(start)
    # La caída empieza dentro del horizonte (siempre deja horas sin correr).
    down_from = start + (plan.outage_after_hours % ((end - start) // HOUR)) * HOUR
    down_until = down_from + OUTAGE
    skipped = 0
    while stack.now() < end:
        stack.tick(3600)
        stack.evaluate(continuous)
        if down_from <= stack.now() < down_until:
            skipped += 1
            continue
        stack.evaluate(outage)
    stack.evaluate(outage)  # la vuelta, si la caída llegaba al final
    assert skipped > 0

    expected = _expected(deadlines, plan.highlight_days)
    assert _normalized(stack, continuous, ids[continuous.organization_id]) == expected
    assert _normalized(stack, outage, ids[outage.organization_id]) == expected
    # Ni una ejecución más escribe nada en ninguna de las dos.
    assert stack.evaluate(continuous) == () and stack.evaluate(outage) == ()
    world.stack.run(world.stack.release())


# --- dos ejecuciones a la vez ---------------------------------------------------------------------


async def _waiting_locks(stack: DeadlinesStack) -> int:
    rows = await stack.actions.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


class _HoldsFirst(DeadlineEvaluator):
    """Con sus candados tomados, espera a que la otra ejecución esté esperando un candado o haya
    llegado a su propio ``_checked`` (sin candados no la detendría nada)."""

    stack: DeadlinesStack
    holding: asyncio.Event
    other_arrived: asyncio.Event

    async def _checked(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while not self.other_arrived.is_set() and await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


class _Arrives(DeadlineEvaluator):
    arrived: asyncio.Event

    async def _checked(self, transaction: Transaction) -> None:
        self.arrived.set()


@settings(
    max_examples=CONCURRENT_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    deadline_days=st.lists(st.integers(1, 3), min_size=1, max_size=3),
    highlight_days=st.integers(1, 2),
    extra_days=st.integers(0, 3),
)
def test_pr_laz_48_two_runs_at_once_never_duplicate_a_declaration(
    stack: DeadlinesStack, deadline_days: list[int], highlight_days: int, extra_days: int
) -> None:
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, highlight_days)
    coordinator, assignee = stack.actions.member(organization), stack.actions.user(organization)
    deadlines = [
        stack.with_action(organization, coordinator, assignee, days)[2] for days in deadline_days
    ]
    stack.tick_to(max(deadlines) + extra_days * DAY + HOUR)
    now = stack.now()
    expected = sum(1 + (now >= due + highlight_days * DAY) for due in deadlines)
    holding, arrived = asyncio.Event(), asyncio.Event()
    first: Any = stack.evaluator_like(_HoldsFirst)
    first.stack, first.holding, first.other_arrived = stack, holding, arrived
    second: Any = stack.evaluator_like(_Arrives)
    second.arrived = arrived

    async def later() -> Any:
        await holding.wait()
        return await stack.evaluate_async(organization, second)

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(
                await asyncio.gather(
                    stack.evaluate_async(organization, first), later(), return_exceptions=True
                )
            )

    results = stack.run(race())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], failures
    assert sorted(len(r) for r in results) == [0, expected]
    keys = [c["overdue_id"] for c in stack.organization_overdue(organization)]
    assert len(keys) == len(set(keys)) == expected
    stack.actions.world.stack.run(stack.actions.world.stack.release())


# --- escalamiento (U04-H-07) ----------------------------------------------------------------------


def test_pr_laz_48_the_escalation_is_a_second_record_and_not_an_idempotency_conflict(
    stack: DeadlinesStack,
) -> None:
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 1)
    coordinator, assignee = stack.actions.member(organization), stack.actions.user(organization)
    finding, _, due_at = stack.with_action(organization, coordinator, assignee)
    stack.tick_to(due_at)
    (first,) = stack.evaluate(organization)
    stack.tick_to(due_at + DAY)
    (second,) = stack.evaluate(organization)

    assert (first.escalation_level, second.escalation_level) == (
        EscalationLevel.COORDINATOR,
        EscalationLevel.MANAGER_WEEKLY,
    )
    assert first.overdue_id != second.overdue_id
    records = stack.overdue_records(finding)
    assert [r["content"]["overdue_id"] for r in records] == [
        str(first.overdue_id),
        str(second.overdue_id),
    ]

    # La clave de BR-LAZ-46 sin el nivel: el segundo nivel con la clave del primero choca.
    collided = {**records[1]["content"], "overdue_id": records[0]["content"]["overdue_id"]}
    result = stack.run(
        stack.actions.writer.write(
            stack.periodic_context(organization),
            "action_overdue",
            collided,
            scope=stack.actions.world.scope(organization),
        )
    )
    assert isinstance(result, LedgerRejection), result
    assert result.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT
    assert len(stack.overdue_records(finding)) == 2
    stack.actions.world.stack.run(stack.actions.world.stack.release())
