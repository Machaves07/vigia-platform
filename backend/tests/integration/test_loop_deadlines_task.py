"""``evaluate_deadlines`` y el panel de vencidos (TASK-313; LC-LAZ-11; BR-LAZ-46 a 48; H-27).

Sobre PostgreSQL 16 real como ``vigia_app``, con la tarea real (``DeadlineEvaluator`` en el
contexto de iteración periódica del planificador), los servicios reales del lazo y la aplicación
real para ``GET /loop/overdue`` (``deadlines_stack``):

- **Registro**: ``evaluate_deadlines`` está en ``LOOP_TASKS`` y en ``U04_TASKS``, horaria y por
  organización, y la compone la raíz con ``unit = U-04``.
- **Bordes del plazo** (BR-LAZ-46): un segundo antes de ``due_at`` no se declara nada; en
  ``due_at`` se escribe ``action_overdue`` (``coordinator``, T08) con su clave
  ``UUID v5 (action_id, due_at, level)`` y sus dos eventos; un segundo antes del umbral
  (``manager_highlight_overdue_days``) no hay escalamiento y al cumplirlo se escribe el registro
  ``manager_weekly`` con su propia clave. Ejecuciones repetidas no escriben nada más.
- **Lo que no se declara**: un hallazgo suspendido por reclasificación, cerrado o incerrable con el
  plazo pasado; la acción sustituida (la nueva tiene su propio plazo y su propia clave). Al
  reanudarse un suspendido vencido (T16), se declara su vencimiento sin transición.
- **Exclusión** (tabla de garantías del PR): dos ejecuciones a la vez con el arrendamiento vencido
  (FS-LAZ-10) dejan un registro por clave y ningún fallo, en tres rondas; la tarea y un cierre del
  mismo hallazgo a la vez terminan sin interbloqueo en los dos órdenes (prueba cruzada del orden de
  candados de ``loop.application.locks``).
- **Lotes**: con ``batch_size`` 1, cada ejecución declara uno y la siguiente el resto.
- **Organización**: la tarea solo ve y escribe la de su transacción.
- **Panel** (SUP-U05-15): alcance de planta (``executive_summary.read``), filtro de planta y de
  zona, periodo, días mínimos, destacados, paginación por clave, particiones en reconstrucción,
  ``no-store`` y ``not_found`` para lo ajeno.

Solo datos generados. Un solo reloj (retro 15): todo instante sale del reloj simulado del mundo.
Topes de las esperas de la base: 60 s (no deciden el resultado).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_actions_support import LICENSE, Who
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.loop.application.closure import ClosureResult, ClosureService
from vigia_platform.loop.application.deadlines import (
    EVALUATE_DEADLINES_SCHEDULE,
    EVALUATE_DEADLINES_TASK,
    DeadlineEvaluator,
    days_overdue,
    overdue_id,
)
from vigia_platform.loop.domain.enums import EscalationLevel
from vigia_platform.loop.registration import LOOP_TASKS, U04_TASKS
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import Schedule, ScheduleKind, TaskIteration
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

DAY: Final = timedelta(days=1)
SECOND: Final = timedelta(seconds=1)
LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver a la otra operación esperando un candado (no decide el resultado)."""
CLOSURE_TEXT: Final = "Resguardo fijo verificado y anclado en el acceso de la zona"
_NOT_FOUND: Final = (404, "not_found")
_INVALID: Final = (400, "invalid_request")


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "loop_deadlines_task") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: DeadlinesStack) -> Iterator[None]:
    yield
    stack.actions.world.stack.run(stack.actions.world.stack.release())


def _people(stack: DeadlinesStack, organization: LoopOrganization) -> tuple[Who, uuid.UUID]:
    return stack.actions.member(organization), stack.actions.user(organization)


async def _waiting_locks(stack: DeadlinesStack) -> int:
    """Candados pedidos y no concedidos en la base de la prueba (de cualquier tipo)."""
    rows = await stack.actions.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


# --- registro -------------------------------------------------------------------------------------


def test_the_task_is_hourly_per_organization_in_the_u04_manifest() -> None:
    expected = (EVALUATE_DEADLINES_SCHEDULE, TaskIteration.PER_ORGANIZATION)
    assert LOOP_TASKS[EVALUATE_DEADLINES_TASK] == expected
    assert U04_TASKS[EVALUATE_DEADLINES_TASK] == expected
    assert Schedule(ScheduleKind.EVERY, 3600) == EVALUATE_DEADLINES_SCHEDULE


# --- bordes ---------------------------------------------------------------------------------------


def test_the_deadline_borders_and_the_escalation_borders(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 2)
    coordinator, assignee = _people(stack, organization)
    finding, action_id, due_at = stack.with_action(organization, coordinator, assignee)

    stack.tick_to(due_at - SECOND)
    assert stack.evaluate(organization) == ()
    assert stack.state(finding) == "action_assigned"

    stack.tick_to(due_at)
    (first,) = stack.evaluate(organization)
    assert (first.escalation_level, first.transition_id) == (EscalationLevel.COORDINATOR, "T08")
    assert first.overdue_id == overdue_id(action_id, due_at, EscalationLevel.COORDINATOR)
    assert stack.state(finding) == "overdue"
    (record,) = stack.overdue_records(finding)
    assert record["content"] == {
        "overdue_id": str(first.overdue_id),
        "action_id": str(action_id),
        "subject_kind": finding.subject_kind,
        "subject_id": str(finding.subject_id),
        "due_at": format_timestamp(due_at),
        "declared_at": format_timestamp(due_at),
        "days_overdue": 0,
        "escalation_level": "coordinator",
        "assignee_user_id": str(assignee),
        "assignee_inactive": False,
    }
    events = stack.record_events(organization, first.record_id)
    assert [name for name, _ in events] == ["finding_escalated", "finding_overdue"]
    for _, payload in events:
        assert payload == {
            "zone_id": str(organization.zone_id),
            "subject_kind": finding.subject_kind,
            "subject_id": str(finding.subject_id),
            "action_id": str(action_id),
            "overdue_id": str(first.overdue_id),
            "record_id": str(first.record_id),
            "due_at": format_timestamp(due_at),
            "days_overdue": 0,
            "escalation_level": "coordinator",
            "assignee_user_id": str(assignee),
        }
    (row,) = stack.actions.action_rows(finding)
    assert row["state"] == "overdue"
    assert stack.evaluate(organization) == ()

    stack.tick_to(due_at + 2 * DAY - SECOND)
    assert stack.evaluate(organization) == ()
    stack.tick_to(due_at + 2 * DAY)
    (second,) = stack.evaluate(organization)
    assert (second.escalation_level, second.transition_id) == (
        EscalationLevel.MANAGER_WEEKLY,
        None,
    )
    assert second.overdue_id == overdue_id(action_id, due_at, EscalationLevel.MANAGER_WEEKLY)
    assert second.overdue_id != first.overdue_id
    records = stack.overdue_records(finding)
    assert [r["content"]["escalation_level"] for r in records] == ["coordinator", "manager_weekly"]
    assert records[1]["content"]["declared_at"] == format_timestamp(due_at + 2 * DAY)
    assert records[1]["content"]["days_overdue"] == 2
    assert [name for name, _ in stack.record_events(organization, second.record_id)] == [
        "finding_escalated"
    ]
    projection = stack.actions.world.finding_state(finding)
    assert projection is not None
    assert (projection["lifecycle_state"], projection["overdue_days"]) == ("overdue", 2)
    assert projection["overdue_since"] == due_at
    assert stack.evaluate(organization) == ()
    stack.tick(5 * 86400)
    assert stack.evaluate(organization) == ()
    assert len(stack.overdue_records(finding)) == 2


def test_a_long_outage_declares_both_levels_in_one_run_with_their_own_instants(
    stack: DeadlinesStack,
) -> None:
    """Puesta al día: tras más días que el umbral sin correr, una sola ejecución escribe los dos
    niveles, cada uno con la marca del día en que se alcanzó (no la de la ejecución)."""
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 1)
    coordinator, assignee = _people(stack, organization)
    finding, action_id, due_at = stack.with_action(organization, coordinator, assignee)
    stack.tick_to(due_at + 3 * DAY + 5 * 3600 * SECOND)

    declared = stack.evaluate(organization)

    assert [(d.escalation_level, d.transition_id) for d in declared] == [
        (EscalationLevel.COORDINATOR, "T08"),
        (EscalationLevel.MANAGER_WEEKLY, None),
    ]
    contents = [r["content"] for r in stack.overdue_records(finding)]
    assert [(c["declared_at"], c["days_overdue"]) for c in contents] == [
        (format_timestamp(due_at), 0),
        (format_timestamp(due_at + DAY), 1),
    ]
    assert {c["overdue_id"] for c in contents} == {
        str(overdue_id(action_id, due_at, level)) for level in EscalationLevel
    }


# --- lo que no se declara -------------------------------------------------------------------------


def test_suspended_closed_and_unclosable_findings_are_not_declared(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.organization()
    coordinator, assignee = _people(stack, organization)
    suspended, _, due_s = stack.with_action(organization, coordinator, assignee)
    closed, closed_action, due_c = stack.with_action(organization, coordinator, assignee)
    unclosable, _, due_u = stack.with_action(organization, coordinator, assignee)
    response = stack.actions.add_evidence(coordinator, closed_action, stack.actions.evidence_body())
    assert response.status_code == 201, response.text
    response = stack.actions.close(coordinator, closed, stack.actions.closure_body())
    assert response.status_code == 201, response.text
    stack.actions.write_unclosable(unclosable)
    stack.actions.classify(suspended, "false_positive", "no_person_present", reinforced=True)
    assert stack.state(suspended) == "suspended_by_reclassification"
    stack.tick_to(max(due_s, due_c, due_u) + 40 * DAY)

    assert stack.evaluate(organization) == ()
    for subject in (suspended, closed, unclosable):
        assert stack.overdue_records(subject) == []
    assert (stack.state(closed), stack.state(unclosable)) == ("closed", "unclosable")

    # Al reanudarse (T16) con el plazo pasado, el pliegue ya lo deja overdue; la tarea declara
    # entonces su vencimiento, sin transición (el hallazgo ya estaba vencido).
    stack.actions.classify(suspended, "confirmed", "real_coexistence", reinforced=True)
    assert stack.state(suspended) == "overdue"
    declared = stack.evaluate(organization)
    assert [(d.escalation_level, d.transition_id) for d in declared] == [
        (EscalationLevel.COORDINATOR, None),
        (EscalationLevel.MANAGER_WEEKLY, None),
    ]
    assert stack.state(suspended) == "overdue"


def test_a_reassigned_action_has_its_own_deadline_and_key(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.organization()
    coordinator, assignee = _people(stack, organization)
    finding, old_action, old_due = stack.with_action(organization, coordinator, assignee)
    stack.tick_to(old_due)
    (first,) = stack.evaluate(organization)
    assert first.action_id == old_action
    coordinator = stack.actions.member(organization)  # la sesión anterior caducó con el reloj
    response = stack.actions.assign(
        coordinator,
        finding,
        stack.actions.action_body(
            stack.actions.user(organization), deadline_days=2, supersedes_action_id=str(old_action)
        ),
    )
    assert response.status_code == 201, response.text
    new_action = uuid.UUID(response.json()["action_id"])
    new_due = datetime.fromisoformat(response.json()["due_at"].replace("Z", "+00:00"))
    # El vencimiento declarado sigue declarado (P5); la acción nueva vence con su propio plazo.
    assert stack.state(finding) == "overdue"
    stack.tick_to(new_due - SECOND)
    assert stack.evaluate(organization) == ()
    stack.tick_to(new_due)
    (second,) = stack.evaluate(organization)
    assert (second.action_id, second.transition_id) == (new_action, None)
    assert second.overdue_id == overdue_id(new_action, new_due, EscalationLevel.COORDINATOR)
    assert second.overdue_id != first.overdue_id


# --- exclusión ------------------------------------------------------------------------------------


class _HoldsFirst(DeadlineEvaluator):
    """Con todos sus candados tomados, avisa (``holding``) y espera a que la otra ejecución esté
    esperando un candado o haya llegado a su propio ``_checked``; después escribe."""

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


@pytest.mark.parametrize("round_", range(3))
def test_two_runs_at_once_with_an_expired_lease_write_one_record_per_key(
    stack: DeadlinesStack, round_: int
) -> None:
    """FS-LAZ-10 y PR-LAZ-48: dos ejecuciones simultáneas (el arrendamiento venció mientras la
    primera seguía viva) sobre dos acciones vencidas, una de ellas ya en el nivel del gerente."""
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 1)
    coordinator, assignee = _people(stack, organization)
    one, _, due_one = stack.with_action(organization, coordinator, assignee, deadline_days=1)
    two, _, due_two = stack.with_action(organization, coordinator, assignee, deadline_days=3)
    stack.tick_to(max(due_one + DAY, due_two))
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
    assert sorted(len(r) for r in results) == [0, 3]
    keys = [c["overdue_id"] for c in stack.organization_overdue(organization)]
    assert len(keys) == len(set(keys)) == 3
    assert [len(stack.overdue_records(s)) for s in (one, two)] == [2, 1]
    assert stack.evaluate(organization) == ()


class _ClosureHolds(ClosureService):
    """Entre la exclusión del hallazgo y sus acciones, espera a que la tarea esté esperando."""

    stack: DeadlinesStack
    holding: asyncio.Event

    async def _between_locks(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


class _TaskHolds(DeadlineEvaluator):
    """Entre la exclusión de los hallazgos y sus acciones, espera a que el cierre esté esperando."""

    stack: DeadlinesStack
    holding: asyncio.Event

    async def _between_locks(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


@pytest.mark.parametrize("first", ["task", "closure"])
@pytest.mark.parametrize("round_", range(3))
def test_the_task_and_a_closure_at_once_share_one_lock_order(
    stack: DeadlinesStack, first: str, round_: int
) -> None:
    """Prueba cruzada del orden de candados (AGENTS.md): ``evaluate_deadlines`` y el cierre firmado
    del mismo hallazgo vencido, a la vez y desde dos conexiones. La primera en llegar se detiene
    **entre** la exclusión del hallazgo y las filas de sus acciones hasta ver a la otra esperando.
    Con el orden único (hallazgo → acciones → cadena) nunca hay interbloqueo: la tarea primero deja
    el vencimiento y el cierre llega a un ``overdue`` (T09); el cierre primero deja ``closed`` y la
    tarea, al releer, no declara nada."""
    organization = stack.actions.world.organization()
    coordinator, assignee = _people(stack, organization)
    finding, action_id, due_at = stack.with_action(organization, coordinator, assignee)
    response = stack.actions.add_evidence(coordinator, action_id, stack.actions.evidence_body())
    assert response.status_code == 201, response.text
    stack.tick_to(due_at + SECOND)
    holding = asyncio.Event()
    evaluator: Any = stack.evaluator_like(_TaskHolds if first == "task" else DeadlineEvaluator)
    closure: Any = stack.actions.service_like(
        _ClosureHolds if first == "closure" else ClosureService
    )
    for service in (evaluator, closure):
        service.stack, service.holding = stack, holding
    context = stack.actions.context(stack.actions.member(organization))  # sesión tras el reloj
    fsid = stack.actions.finding_state_id(finding)

    async def task() -> Any:
        if first != "task":
            await holding.wait()
        return await stack.evaluate_async(organization, evaluator)

    async def close() -> Any:
        if first != "closure":
            await holding.wait()
        return await closure.close(
            context, fsid, closure_description=CLOSURE_TEXT, professional_license=LICENSE
        )

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(task(), close(), return_exceptions=True))

    declared, closed = stack.run(race())
    assert isinstance(closed, ClosureResult), repr(closed)
    assert not isinstance(declared, BaseException), repr(declared)
    assert stack.state(finding) == "closed"
    if first == "task":
        assert [d.transition_id for d in declared] == ["T08"]
        assert [r["record_type"] for r in stack.actions.subject_records(finding)][-2:] == [
            "action_overdue",
            "closure",
        ]
    else:
        assert declared == ()
        assert stack.overdue_records(finding) == []
    assert stack.evaluate(organization) == ()


# --- lotes y organización -------------------------------------------------------------------------


def test_a_batch_of_one_leaves_the_rest_for_the_next_run(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.organization()
    coordinator, assignee = _people(stack, organization)
    one, _, due_one = stack.with_action(organization, coordinator, assignee)
    two, _, due_two = stack.with_action(organization, coordinator, assignee)
    stack.tick_to(max(due_one, due_two))
    small = stack.evaluator_like(batch_size=1)

    assert len(stack.evaluate(organization, small)) == 1
    assert len(stack.evaluate(organization, small)) == 1
    assert stack.evaluate(organization, small) == ()
    assert [len(stack.overdue_records(s)) for s in (one, two)] == [1, 1]


def test_the_task_only_touches_its_own_organization(stack: DeadlinesStack) -> None:
    """``evaluate_deadlines`` es ``PER_ORGANIZATION``: la transacción del planificador trae la
    organización (``test_task_isolation.py``, caso ``INHERITED``). Las sentencias filtran por
    ``organization_id``, pero la garantía es la RLS que fuerza el ``ScopeContext``: quitar el
    filtro explícito de ``_CANDIDATES`` no vuelve esta prueba roja (RLS ya descarta las filas de B;
    comprobado y revertido, evidencia en el PR)."""
    own = stack.actions.world.organization()
    foreign = stack.actions.world.organization()
    mine, _, due_mine = stack.with_action(own, *_people(stack, own))
    theirs, _, due_theirs = stack.with_action(foreign, *_people(stack, foreign))
    stack.tick_to(max(due_mine, due_theirs))

    declared = stack.evaluate(own)

    assert [d.finding_state_id for d in declared] == [stack.actions.finding_state_id(mine)]
    assert stack.overdue_records(theirs) == []
    assert stack.state(theirs) == "action_assigned"


# --- el panel de vencidos (SUP-U05-15) ------------------------------------------------------------


def _panel(stack: DeadlinesStack, who: Who, **params: Any) -> tuple[int, dict[str, Any]]:
    response = stack.get_overdue(who, params)
    return response.status_code, response.json()


def _overdue_world(
    stack: DeadlinesStack,
) -> tuple[LoopOrganization, list[tuple[IngestedSubject, uuid.UUID, datetime]]]:
    organization = stack.actions.world.organization()
    stack.set_highlight_days(organization, 3)
    coordinator, assignee = _people(stack, organization)
    built = [
        stack.with_action(organization, coordinator, assignee, deadline_days=days)
        for days in (1, 2, 3)
    ]
    return organization, built


def test_the_panel_lists_by_due_at_highlights_and_pages_by_key(stack: DeadlinesStack) -> None:
    organization, built = _overdue_world(stack)
    dues = [due for _, _, due in built]
    stack.tick_to(max(dues) + 4 * DAY)
    manager = stack.manager(organization)

    status, before = _panel(stack, manager, plant_id=str(organization.plant_id))
    assert status == 200
    assert [i["declared"] for i in before["items"]] == [False, False, False]
    stack.evaluate(organization)
    response = stack.get_overdue(manager, {"plant_id": str(organization.plant_id)})
    assert response.headers["cache-control"] == "no-store"
    page = response.json()
    now = stack.now()
    assert set(page) == {
        "plant_id",
        "highlight_threshold_days",
        "items",
        "zones",
        "partition_notice",
        "next_cursor",
    }
    assert (page["plant_id"], page["highlight_threshold_days"]) == (
        str(organization.plant_id),
        3,
    )
    assert [i["finding_state_id"] for i in page["items"]] == [
        str(stack.actions.finding_state_id(f)) for f, _, _ in built
    ]
    for item, (finding, action_id, due) in zip(page["items"], built, strict=True):
        days = days_overdue(due, now)
        assert item == {
            "finding_state_id": str(stack.actions.finding_state_id(finding)),
            "plant_id": str(organization.plant_id),
            "zone_id": str(organization.zone_id),
            "subject_kind": finding.subject_kind,
            "family": item["family"],
            "tier": item["tier"],
            "lifecycle_state": "overdue",
            "declared": True,
            "action_id": str(action_id),
            "action_type": "engineering",
            "assignee_user_id": item["assignee_user_id"],
            "assignee_inactive": False,
            "due_at": format_timestamp(due),
            "overdue_days": days,
            "highlighted": days >= 3,
        }
    highlighted = sum(1 for i in page["items"] if i["highlighted"])
    assert page["zones"] == [
        {
            "zone_id": str(organization.zone_id),
            "overdue_count": 3,
            "highlighted_count": highlighted,
        }
    ]
    assert (page["partition_notice"], page["next_cursor"]) == (None, None)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(4):
        params: dict[str, Any] = {"plant_id": str(organization.plant_id), "limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        status, chunk = _panel(stack, manager, **params)
        assert status == 200
        seen.extend(i["finding_state_id"] for i in chunk["items"])
        cursor = chunk["next_cursor"]
        if cursor is None:
            break
    assert seen == [i["finding_state_id"] for i in page["items"]]

    status, later = _panel(
        stack,
        manager,
        plant_id=str(organization.plant_id),
        min_overdue_days=days_overdue(dues[1], now),
    )
    assert [i["due_at"] for i in later["items"]] == [format_timestamp(d) for d in dues[:2]]
    status, period = _panel(
        stack,
        manager,
        plant_id=str(organization.plant_id),
        **{"from": format_timestamp(dues[1]), "to": format_timestamp(dues[1] + SECOND)},
    )
    assert [i["due_at"] for i in period["items"]] == [format_timestamp(dues[1])]


def test_the_panel_filters_by_plant_and_by_zone(stack: DeadlinesStack) -> None:
    """Guardas de alcance (AGENTS.md): una fila vencida en **otra zona de la planta** solo sale sin
    filtro de zona, y una en **otra planta de la organización** nunca sale en el panel de esta.
    Mutaciones: quitar ``f.zone_id = ANY(...)`` o ``f.plant_id = :plant_id`` de ``_ITEMS`` deja
    esta prueba en rojo (evidencia en el PR)."""
    organization, built = _overdue_world(stack)
    stack.tick_to(max(due for _, _, due in built) + DAY)
    manager = stack.manager(organization)
    stack.evaluate(organization)
    world = stack.actions.world
    other_zone = organization.site.other_zone_id
    in_other_zone = world.clone_row(built[0][0], zone_id=other_zone)
    other_plant = world.add_plant(organization)
    far_zone = uuid.uuid4()
    world.stack.authz.add_zone(organization.organization_id, other_plant, far_zone)
    in_other_plant = world.clone_row(built[0][0], zone_id=far_zone, plant_id=other_plant)

    def subjects(**params: Any) -> set[str]:
        status, page = _panel(stack, manager, **params)
        assert status == 200, page
        rows = world.rows(organization)
        by_fsid = {str(r["finding_state_id"]): str(r["subject_id"]) for r in rows}
        return {by_fsid[i["finding_state_id"]] for i in page["items"]}

    own = {str(f.subject_id) for f, _, _ in built}
    plant = str(organization.plant_id)
    assert subjects(plant_id=plant) == own | {str(in_other_zone)}
    assert subjects(plant_id=plant, zone_id=str(organization.zone_id)) == own
    assert subjects(plant_id=plant, zone_id=str(other_zone)) == {str(in_other_zone)}
    assert subjects(plant_id=str(other_plant)) == {str(in_other_plant)}


def test_the_panel_declares_a_projection_being_rebuilt(stack: DeadlinesStack) -> None:
    organization, _ = _overdue_world(stack)
    manager = stack.manager(organization)
    admin = stack.actions.world.stack
    admin.execute(
        "UPDATE loop.loop_projection_checkpoint SET state = 'rebuilding'"
        " WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    try:
        status, page = _panel(stack, manager, plant_id=str(organization.plant_id))
        assert (status, page["partition_notice"]) == (200, "projection_rebuilding")
    finally:
        admin.execute(
            "UPDATE loop.loop_projection_checkpoint SET state = 'healthy'"
            " WHERE organization_id = $1 AND plant_id = $2",
            organization.organization_id,
            organization.plant_id,
        )


def test_the_panel_answers_not_found_for_what_is_not_in_scope(stack: DeadlinesStack) -> None:
    organization, _ = _overdue_world(stack)
    foreign = stack.actions.world.organization()
    actions = stack.actions
    plant = str(organization.plant_id)
    manager = stack.manager(organization)
    zone_coordinator = actions.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, organization.zone_id
    )
    line_manager = actions.member(organization, Role.LINE_MANAGER)
    other_plant = actions.world.add_plant(organization)
    cases: list[tuple[Who, dict[str, Any]]] = [
        (manager, {"plant_id": str(foreign.plant_id)}),
        (manager, {"plant_id": str(uuid.uuid4())}),
        (manager, {"plant_id": str(other_plant), "zone_id": str(organization.zone_id)}),
        (manager, {"plant_id": plant, "zone_id": str(foreign.zone_id)}),
        (manager, {"plant_id": plant, "zone_id": str(uuid.uuid4())}),
        (zone_coordinator, {"plant_id": plant}),
        (line_manager, {"plant_id": plant}),
    ]
    bodies = []
    for who, params in cases:
        status, body = _panel(stack, who, **params)
        assert (status, body.get("code")) == _NOT_FOUND, (params, body)
        bodies.append({k: v for k, v in body.items() if k != "correlation_id"})
    assert all(body == bodies[0] for body in bodies)
    # Un coordinador de la planta sí la ve.
    plant_coordinator = actions.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.PLANT, organization.plant_id
    )
    assert _panel(stack, plant_coordinator, plant_id=plant)[0] == 200


_PLANT: Final = "<plant>"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"plant_id": "no-es-un-uuid"},
        {"plant_id": _PLANT, "unknown": "1"},
        {"plant_id": _PLANT, "limit": "0"},
        {"plant_id": _PLANT, "limit": "201"},
        {"plant_id": _PLANT, "min_overdue_days": "-1"},
        {"plant_id": _PLANT, "min_overdue_days": "3661"},
        {"plant_id": _PLANT, "cursor": "%%%"},
        {"plant_id": _PLANT, "cursor": "bm8tc2VwYXJhZG9y"},
        {"plant_id": _PLANT, "from": "2026-10-01"},
        {"plant_id": _PLANT, "from": "2026-10-02T00:00:00.000Z", "to": "2026-10-01T00:00:00.000Z"},
    ],
    ids=lambda params: "-".join(f"{k}={v}" for k, v in params.items()) or "empty",
)
def test_the_panel_rejects_malformed_queries(stack: DeadlinesStack, params: dict[str, str]) -> None:
    organization = stack.actions.world.a
    manager = stack.manager(organization)
    plant = str(organization.plant_id)
    query = {key: plant if value == _PLANT else value for key, value in params.items()}
    status, body = _panel(stack, manager, **query)
    assert (status, body.get("code")) == _INVALID, body


def test_the_panel_rejects_too_many_or_repeated_parameters(stack: DeadlinesStack) -> None:
    organization = stack.actions.world.a
    manager = stack.manager(organization)
    zones = "&".join(f"zone_id={uuid.uuid4()}" for _ in range(51))
    for query in (
        f"plant_id={organization.plant_id}&{zones}",
        f"plant_id={organization.plant_id}&plant_id={organization.plant_id}",
        f"plant_id={organization.plant_id}&limit=1&limit=2",
    ):
        cookie, _ = manager
        response = stack.run(
            stack.client.get(
                f"/loop/overdue?{query}",
                headers={
                    "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
        )
        assert (response.status_code, response.json().get("code")) == _INVALID, query
