"""NFR-LAZ-01: banco de ``evaluate_deadlines`` en una organización (TASK-313; LC-LAZ-11).

``laz_evaluate_deadlines_org`` mide **una pasada** de la tarea real (``DeadlineEvaluator`` en el
contexto de iteración periódica del planificador) sobre una organización con
``PENDING_PER_ROUND`` acciones recién vencidas: fase 1 sin candados (candidatos por el índice
parcial y claves ya escritas), los candados de todos los hallazgos y de sus acciones, y la
escritura de cada ``action_overdue`` con sus eventos y la proyección en la misma transacción.
Cada ronda prepara **fuera de la medida** sus hallazgos (ingresados por la ruta del contrato,
clasificados y con su acción asignada) y lleva el reloj simulado hasta su plazo.

El objetivo, **p95 ≤ 30 s por organización**, es ``[objetivo propio]`` (NFR-LAZ-01): el informe
dice si se cumple y nunca decide el resultado. TASK-330 fija su línea base y
regresión relativa del 50 % bloqueante, con 100 rondas en ci y nightly. Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

import pytest

from tests.benchmarks.conftest import Measure
from tests.benchmarks.laz_support import measured
from tests.integration.conftest import PostgresEndpoint
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack

pytestmark = pytest.mark.integration

OBJECTIVE_MS: Final = 30_000.0
PENDING_PER_ROUND: Final = 5
ROUNDS: Final = 100


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DeadlinesStack]:
    with deadlines_stack(postgres_endpoint, "laz_evaluate_deadlines_bench") as built:
        yield built


def test_laz_evaluate_deadlines_org(stack: DeadlinesStack, measure: Measure) -> None:
    """Fila 6. [objetivo propio] p95 ≤30 s por organización.

    Composición: candidatos por índice + candados ordenados + action_overdue
    y sus eventos + proyección atómica, cinco acciones nuevas por ronda.
    Tramos: total → evaluate_deadlines (contexto de tarea periódica real).
    """
    organization = stack.actions.world.organization()
    declared: list[int] = []

    def prepare() -> None:
        coordinator = stack.actions.member(organization)
        assignee = stack.assignee(organization)
        dues = [
            stack.with_action(organization, coordinator, assignee)[2]
            for _ in range(PENDING_PER_ROUND)
        ]
        stack.tick_to(max(dues))

    def run() -> None:
        declared.append(len(stack.evaluate(organization)))

    result = measured(
        measure,
        "laz_evaluate_deadlines_org",
        OBJECTIVE_MS,
        lambda trace: trace.call("evaluate_deadlines", run),
        setup=prepare,
        details={"pending_per_round": PENDING_PER_ROUND},
    )
    assert result.samples == ROUNDS
    # Cada medida declara al menos los de su ronda y al final no queda ninguno sin declarar.
    assert min(declared) >= PENDING_PER_ROUND
    assert stack.evaluate(organization) == ()
