"""Caso de ejemplo: la lámina colgante que el nodo no se atrevió a publicar (BR-LAZ-35; TASK-309).

Una lámina plástica colgando en el borde de una zona, agitada por el viento, produce una silueta
que el modelo del nodo confunde con una persona con confianza entre el umbral de revisión y el de
publicación: el nodo la manda a la cola de revisión en vez de descartarla por su cuenta (P6, RF-
BOR-05: nunca al descarte automático) o publicarla como hallazgo sin corroborar. Un coordinador
revisa el clip, confirma que es un objeto y no una persona, y la resuelve como falso positivo
(T06): el lazo nunca se abre y el elemento sale de la cola **solo** por esa resolución humana
(H-05, H-23), nunca por su cuenta ni por el paso del tiempo.

Sobre PostgreSQL 16 real, con el consumidor y el servicio reales (``review_queue_stack``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_review_queue_support import ReviewQueueStack, Who, review_queue_stack

pytestmark = [pytest.mark.integration, pytest.mark.laz_examples]

HANGING_SHEET_REASON = (
    "El clip muestra una lámina plástica colgante en el borde de la zona, agitada por el "
    "viento; no hay ninguna persona en el encuadre durante el tramo completo"
)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ReviewQueueStack]:
    with review_queue_stack(postgres_endpoint, "laz_hanging_sheet") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: ReviewQueueStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: ReviewQueueStack) -> Who:
    return stack.member(stack.world.a)


def test_the_hanging_sheet_goes_to_review_and_is_discarded_as_a_false_positive(
    stack: ReviewQueueStack, coordinator: Who
) -> None:
    # El nodo no descartó la detección por su cuenta: llega a la cola, en in_review.
    detection = stack.in_review()
    pending = stack.queue_row(detection)
    assert pending is not None and pending["state"] == "pending"
    projected_before = stack.world.finding_state(detection)
    assert projected_before is not None and projected_before["lifecycle_state"] == "in_review"

    # El coordinador revisa el clip y la resuelve como objeto confundido con una persona.
    response = stack.resolve(
        coordinator,
        detection,
        outcome="review_discarded",
        reason_category="object_mistaken_for_person",
        reason_text=HANGING_SHEET_REASON,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome"] == "review_discarded"
    assert body["lifecycle_state"] == "discarded"

    # El lazo nunca se abrió; el elemento salió de la cola solo por esta resolución.
    resolved = stack.queue_row(detection)
    assert resolved is not None
    assert resolved["state"] == "resolved" and resolved["resolved_at"] is not None
    projected_after = stack.world.finding_state(detection)
    assert projected_after is not None
    assert projected_after["lifecycle_state"] == "discarded"
    assert projected_after["current_classification_outcome"] is None, (
        "T06 descarta la cola sin clasificación: no es la misma transición que T04"
    )
    (resolution,) = stack.resolutions(detection)
    assert resolution["reason_category"] == "object_mistaken_for_person"
    assert resolution["reason_text"] == HANGING_SHEET_REASON

    # Nada de esto reabre el lazo ni requiere una segunda resolución: un intento repetido
    # se rechaza (transition_not_allowed), el elemento sigue resuelto.
    again = stack.resolve(coordinator, detection, outcome="review_confirmed")
    assert again.status_code == 409, again.text
    assert again.json()["detail_code"] == "loop_transition_not_allowed"
