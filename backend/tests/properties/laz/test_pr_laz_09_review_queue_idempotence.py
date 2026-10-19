"""PR-LAZ-09: dos entregas o cualquier permutación de llegada producen la misma cola (TASK-309).

LC-LAZ-08; BR-LAZ-151. El consumidor ``review_queue`` relee siempre el contenido completo del
expediente por ``LectorExpediente.get`` con el ``platform_record_id`` del evento: el evento de
U-03 (``detection_for_review_received``) nunca trae el contenido en la carga, solo la referencia
(BR-NUC-75), así que la única variante que el contrato admite —«el evento trae la referencia»— es
la que esta propiedad ejercita; no existe una segunda variante «con contenido» en este contrato
(confirmado contra ``vigia_contracts.models.detection_for_review.DetectionForReview``).

Sobre PostgreSQL 16 real (``review_queue_stack``), con pocos ejemplos por el costo de la base:

- **conmutatividad**: cualquier permutación de llegada de un lote de detecciones distintas deja
  el mismo conjunto de elementos de la cola (mismo contenido, mismo ``review_due_at``);
- **reentrega**: repetir la entrega de un evento ya aplicado, con cualquier otra actividad entre
  medio, no cambia nada;
- la entrega concurrente de la misma detección desde dos conexiones a la vez (dos trabajadores) y
  su mutación (quitar ``ON CONFLICT (detection_id)`` del ``INSERT``) están en
  ``tests/integration/test_loop_review_queue.py``, con la salida en rojo en la tabla de garantías.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization
from tests.loop_review_queue_support import ReviewQueueStack, review_queue_stack

pytestmark = pytest.mark.integration

BATCH_SIZE = 3


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ReviewQueueStack]:
    with review_queue_stack(postgres_endpoint, "pr_laz_09") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: ReviewQueueStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


def _queue_snapshot(stack: ReviewQueueStack, organization: LoopOrganization) -> list[Any]:
    rows = stack.fetch(
        "SELECT detection_id, plant_id, zone_id, family, max_confidence, review_threshold,"
        " publication_threshold, review_reason, review_due_at, state, source_event_id"
        " FROM loop.review_queue_item WHERE organization_id = $1 ORDER BY detection_id",
        organization.organization_id,
    )
    return [dict(row) for row in rows]


def _clear(stack: ReviewQueueStack, organization: LoopOrganization) -> None:
    organization_id = organization.organization_id
    stack.world.stack.execute(
        "DELETE FROM loop.review_queue_item WHERE organization_id = $1", organization_id
    )
    stack.world.stack.execute(
        "DELETE FROM loop.finding_state WHERE organization_id = $1", organization_id
    )
    stack.world.stack.execute(
        "DELETE FROM loop.loop_projection_checkpoint WHERE organization_id = $1", organization_id
    )


@settings(
    max_examples=4,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(order=st.permutations(range(BATCH_SIZE)))
def test_any_arrival_order_of_a_batch_leaves_the_same_queue(
    stack: ReviewQueueStack, order: list[int]
) -> None:
    organization = stack.world.organization()
    detections = [stack.world.ingest_detection(organization) for _ in range(BATCH_SIZE)]
    events = [stack.world.review_queue_event(detection) for detection in detections]

    for event in events:
        stack.world.deliver_review_queue(stack.consumer, event)
    in_order_snapshot = _queue_snapshot(stack, organization)
    assert len(in_order_snapshot) == BATCH_SIZE

    _clear(stack, organization)
    for index in order:
        stack.world.deliver_review_queue(stack.consumer, events[index])
    shuffled_snapshot = _queue_snapshot(stack, organization)

    assert shuffled_snapshot == in_order_snapshot


def test_a_duplicate_delivery_is_a_no_op_regardless_of_how_many_other_detections_arrived(
    stack: ReviewQueueStack,
) -> None:
    organization = stack.world.organization()
    detections = [stack.world.ingest_detection(organization) for _ in range(BATCH_SIZE)]
    events = [stack.world.review_queue_event(detection) for detection in detections]
    for event in events:
        stack.world.deliver_review_queue(stack.consumer, event)
    before = _queue_snapshot(stack, organization)
    for event in events:
        inserted = stack.world.deliver_review_queue(stack.consumer, event)
        assert inserted is False
    after = _queue_snapshot(stack, organization)
    assert after == before
