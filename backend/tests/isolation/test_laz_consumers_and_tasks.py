"""TASK-327: efectos reales de cada consumidor y tarea de U-04 con dos organizaciones.

Una huella incluye toda tabla con organization_id, también U-04, expediente, auditoría y outbox.
Cada iteración debe cambiar su propia organización y ninguna fila de la otra. La tarea global
solo publica un pulso sin carga en la proveedora. Sin contexto, la frontera del despachador o
planificador debe rechazar antes de entrar al manejador y dejar security_alert.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final
from unittest.mock import Mock

import pytest

from tests.integration.test_monthly_package_task import world as monthly_world  # noqa: F401
from tests.isolation import laz_background as background
from tests.isolation.gob_world import changed_tables
from tests.laz_support import loop_world
from tests.notification_support import notification_world
from vigia_platform.identity.adapters.authz_store import PostgresAuthorizationAudit
from vigia_platform.identity.authz.context import ContextAbsentAuditor
from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.shared.context import ActorUnit, ContextAbsent
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    PeriodicTaskRegistry,
    TaskIteration,
)
from vigia_platform.shared.runtime.units import UnitServices, free_text_registry, registered_units

CONSUMER_CASES: Final[Mapping[str, Callable[..., Any]]] = {
    "loop_projection": background.loop_case,
    "review_queue": background.loop_case,
    "assistant_worker": background.assistant_case,
    "export_worker": background.export_case,
    "notification_engine": background.notification_case,
    "email_delivery_events": background.notification_case,
}
TASK_CASES: Final[Mapping[str, Callable[..., Any]]] = {
    "evaluate_deadlines": background.deadlines_case,
    "review_reminders": background.loop_case,
    "unclosable_review_due": background.unclosable_case,
    "monthly_package": background.monthly_case,
    "metrics_materialize": background.metrics_case,
    "adoption_check": background.adoption_case,
    "digest_weekly": background.notification_case,
    "email_grouping": background.notification_case,
    "verify_loop_projection": background.loop_case,
    "email_delivery_events_poll": background.notification_case,
}


@pytest.fixture
def loop_background_world(postgres_endpoint: Any) -> Any:
    """Base por caso: las otras pilas regeneran los roles globales de PostgreSQL al migrar.

    No se retiene un pool con la configuración anterior entre casos que abren otra pila.
    """
    with loop_world(postgres_endpoint, "isolation_loop_background") as world:
        yield world


@pytest.fixture
def notification_background_world(postgres_endpoint: Any) -> Any:
    with notification_world(postgres_endpoint, "isolation_notifications_background") as world:
        yield world


def _services() -> UnitServices:
    """Servicios inertes: se registran los manejadores reales sin ejecutarlos."""
    inert: Any = Mock()
    return UnitServices(
        clock=inert,
        metrics=get_metrics(),
        provider_organization_id=uuid.uuid4(),
        database=inert,
        contexts=inert,
        authorizer=inert,
        audit=inert,
        outbox=inert,
        writer=inert,
        free_text=free_text_registry(registered_units()),
        signing=inert,
        checkpoints=inert,
        kms=inert,
    )


def _registries() -> tuple[ConsumerRegistry, PeriodicTaskRegistry]:
    consumers, tasks = ConsumerRegistry(), PeriodicTaskRegistry()
    services = _services()
    for unit in registered_units():
        unit.consumers(consumers, services)
        unit.periodic_tasks(tasks, services)
    return consumers, tasks


def uncovered_consumers(names: set[str], cases: Mapping[str, Any] = CONSUMER_CASES) -> list[str]:
    """Consumidores de U-04 registrados sin prueba de aislamiento asignada."""
    return sorted(names - set(cases))


def uncovered_tasks(names: set[str], cases: Mapping[str, Any] = TASK_CASES) -> list[str]:
    """Tareas de U-04 registradas sin prueba de aislamiento asignada."""
    return sorted(names - set(cases))


def _requires_transaction(handler: Callable[..., Any]) -> bool:
    """Los manejadores reciben la transacción que porta el ``ScopeContext`` del despachador."""
    return "transaction" in inspect.signature(handler).parameters


def _requires_context_or_transaction(handler: Callable[..., Any]) -> bool:
    """La tarea global recibe su alcance global; las demás, la transacción contextual."""
    parameters = inspect.signature(handler).parameters
    return "transaction" in parameters or "scope" in parameters


def test_every_u04_consumer_and_periodic_task_has_an_isolation_case() -> None:
    consumers, tasks = _registries()
    u04_consumers = {
        consumer.consumer_name: consumer
        for consumer in consumers.consumers()
        if consumer.unit is ActorUnit.U04
    }
    u04_tasks = {task.task_name: task for task in tasks.tasks() if task.unit is ActorUnit.U04}

    assert set(u04_consumers) == set(U04_CONSUMERS)
    assert set(u04_tasks) == set(U04_TASKS)
    assert uncovered_consumers(set(u04_consumers)) == []
    assert uncovered_tasks(set(u04_tasks)) == []
    assert set(CONSUMER_CASES) == set(u04_consumers)
    assert set(TASK_CASES) == set(u04_tasks)
    assert all(callable(factory) for factory in (*CONSUMER_CASES.values(), *TASK_CASES.values()))
    assert all(_requires_transaction(consumer.handler) for consumer in u04_consumers.values())
    assert all(_requires_context_or_transaction(task.handler) for task in u04_tasks.values())
    assert {name for name, task in u04_tasks.items() if task.iteration is TaskIteration.GLOBAL} == {
        "email_delivery_events_poll"
    }
    assert all(
        task.iteration is TaskIteration.PER_ORGANIZATION
        for name, task in u04_tasks.items()
        if name != "email_delivery_events_poll"
    )


def test_a_consumer_or_task_without_case_is_named() -> None:
    assert uncovered_consumers({*CONSUMER_CASES, "consumer_sin_caso"}) == ["consumer_sin_caso"]
    assert uncovered_tasks({*TASK_CASES, "tarea_sin_caso"}) == ["tarea_sin_caso"]
    with pytest.raises(AssertionError, match="consumer_sin_caso"):
        assert not uncovered_consumers({*CONSUMER_CASES, "consumer_sin_caso"})
    with pytest.raises(AssertionError, match="tarea_sin_caso"):
        assert not uncovered_tasks({*TASK_CASES, "tarea_sin_caso"})


def _assert_isolated(scenario: background.Scenario, mine: uuid.UUID, other: uuid.UUID) -> list[str]:
    own_before, other_before = scenario.prints(mine), scenario.prints(other)
    scenario.prepare(mine)
    scenario.run(scenario.invoke(mine))
    leaked = changed_tables(other_before, scenario.prints(other))
    assert leaked == [], f"{scenario.name} cambió tablas de la otra organización: {leaked}"
    return changed_tables(own_before, scenario.prints(mine))


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted({**CONSUMER_CASES, **TASK_CASES}))
def test_each_handler_changes_only_its_organization(
    request: pytest.FixtureRequest, name: str
) -> None:
    factory = {**CONSUMER_CASES, **TASK_CASES}[name]
    with factory(request, name) as scenario:
        registered_name = getattr(scenario.registration, "consumer_name", None) or getattr(
            scenario.registration, "task_name", None
        )
        assert registered_name == name
        a, b = scenario.organizations
        if name == "email_delivery_events_poll":
            provider = scenario.contexts.provider_audit_context().organization_id
            before = scenario.fetch(
                "SELECT event_id FROM shared.outbox_event"
                " WHERE organization_id=$1 AND event_name=$2",
                provider,
                name,
            )
            assert _assert_isolated(scenario, a, b) == []
            after = scenario.fetch(
                "SELECT payload::text AS payload FROM shared.outbox_event"
                " WHERE organization_id=$1 AND event_name=$2",
                provider,
                name,
            )
            assert len(after) == len(before) + 1
            assert all(row["payload"] == "{}" for row in after)
        else:
            for mine, other in ((a, b), (b, a)):
                changed = _assert_isolated(scenario, mine, other)
                assert changed, f"{name} no hizo nada en su propia organización"
                assert any(
                    table.startswith(("loop.", "accreditation.", "notifications."))
                    for table in changed
                ), changed

        # Misma apertura de transacción que el despachador/planificador, con la fábrica de
        # contexto ausente. No se confunde con pasar un objeto Transaction falso al servicio.
        audit = PostgresAuthorizationAudit(
            database=scenario.database,
            audit=scenario.audit,
            outbox=scenario.outbox,
            clock=scenario.clock,
        )
        auditor = ContextAbsentAuditor(contexts=scenario.contexts, audit=audit)
        provider = scenario.contexts.provider_audit_context().organization_id

        def alerts() -> list[Any]:
            return scenario.fetch(
                "SELECT payload FROM shared.outbox_event WHERE organization_id=$1"
                " AND event_name='security_alert'"
                " AND payload->>'alert_kind'='context_absent_attempt'",
                provider,
            )

        before_alerts = len(alerts())
        before_a, before_b = scenario.prints(a), scenario.prints(b)

        async def attempt() -> None:
            with pytest.raises(ContextAbsent):
                await scenario.invoke(a, absent=True)
            await auditor.drain()

        auditor.install()
        try:
            scenario.run(attempt())
        finally:
            auditor.uninstall()
        assert len(alerts()) == before_alerts + 1
        assert scenario.prints(a) == before_a
        assert scenario.prints(b) == before_b


@pytest.mark.integration
def test_a_handler_dispatched_into_the_other_organization_is_detected(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sonda del arnés: iterando A se ejecuta el manejador real de B; debe quedar en rojo."""
    with background.loop_case(request, "loop_projection") as scenario:
        a, b = scenario.organizations
        invoke = scenario.invoke

        async def misplaced(organization_id: uuid.UUID) -> None:
            await invoke(b)

        monkeypatch.setattr(scenario, "invoke", misplaced)
        with pytest.raises(AssertionError, match=r"otra organización.*loop\.finding_state"):
            _assert_isolated(scenario, a, b)
