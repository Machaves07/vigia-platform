"""Aislamiento de los consumidores y tareas registrados por U-04 (TASK-327).

La composición real es el catálogo bajo prueba: un consumidor o una tarea nuevos no pueden quedar
sin caso, y cada manejador recibe la transacción que ya abrió el planificador con su
``ScopeContext``. Las pruebas de comportamiento de cada caso viven con el módulo que siembra su
evento o su trabajo pendiente; este archivo mantiene la cobertura transversal bloqueante.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from unittest.mock import Mock

import pytest

from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    PeriodicTaskRegistry,
    TaskIteration,
)
from vigia_platform.shared.runtime.units import UnitServices, free_text_registry, registered_units


@dataclass(frozen=True)
class Case:
    """La prueba que demuestra el efecto aislado de una entrada de fondo de U-04."""

    evidence: str


CONSUMER_CASES: Final[Mapping[str, Case]] = {
    "loop_projection": Case("test_loop_projection_consumer.py"),
    "review_queue": Case("test_loop_review_queue.py"),
    "assistant_worker": Case("test_loop_assistant_flow.py"),
    "export_worker": Case("test_export_worker_resume.py"),
    "notification_engine": Case("test_pr_laz_39_recipients_scope.py"),
    "email_delivery_events": Case("test_pr_laz_58_bounces_complaints.py"),
}
TASK_CASES: Final[Mapping[str, Case]] = {
    "evaluate_deadlines": Case("test_loop_deadlines_task.py"),
    "review_reminders": Case("test_loop_review_queue.py"),
    "unclosable_review_due": Case("test_loop_unclosable.py"),
    "monthly_package": Case("test_monthly_package_task.py"),
    "metrics_materialize": Case("test_metrics_routes.py"),
    "adoption_check": Case("test_adoption_task.py"),
    "digest_weekly": Case("test_notification_grouping_tasks.py"),
    "email_grouping": Case("test_notification_grouping_tasks.py"),
    "verify_loop_projection": Case("test_loop_projection_rebuild.py"),
    "email_delivery_events_poll": Case("test_pr_laz_58_bounces_complaints.py"),
}


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


def uncovered_consumers(names: set[str], cases: Mapping[str, Case] = CONSUMER_CASES) -> list[str]:
    """Consumidores de U-04 registrados sin prueba de aislamiento asignada."""
    return sorted(names - set(cases))


def uncovered_tasks(names: set[str], cases: Mapping[str, Case] = TASK_CASES) -> list[str]:
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
