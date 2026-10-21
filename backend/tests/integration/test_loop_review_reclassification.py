"""PR-LAZ-10, VIG-268: regresiones reducidas de la semilla 1111 (8 ejemplos, 12 pasos).

Resolver la cola no crea una fila ``classification``. La primera clasificación posterior
mantiene su secuencia propia, pero debe respetar el motivo reforzado de T15/T17/T18.
Servicios, expediente, outbox y PostgreSQL reales; clasificación por HTTP.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_classification_support import REASON, ClassificationStack, classification_stack
from vigia_platform.loop.application.review_queue import ReviewQueueService
from vigia_platform.loop.projection.fold import fold

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ClassificationStack]:
    with classification_stack(postgres_endpoint, "review_reclassification") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: ClassificationStack) -> Iterator[None]:
    yield
    stack.run(stack.world.stack.release())


@pytest.mark.parametrize(
    ("resolution", "category", "previous", "state", "effect", "event"),
    [
        (
            "review_discarded",
            "object_mistaken_for_person",
            "false_positive",
            "discarded",
            "none",
            "finding_reclassified",
        ),
        (
            "review_discarded",
            "scheduled_maintenance",
            "authorized_operation",
            "discarded",
            "none",
            "finding_reclassified",
        ),
        (
            "review_confirmed",
            "real_coexistence",
            "confirmed",
            "suspended_by_reclassification",
            "loop_suspended",
            "loop_suspended",
        ),
    ],
)
def test_resolution_then_false_positive_keeps_the_lifecycle_and_history_coherent(
    stack: ClassificationStack,
    resolution: str,
    category: str,
    previous: str,
    state: str,
    effect: str,
    event: str,
) -> None:
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    detection = stack.world.ingest_detection(organization)
    consumer, repository = stack.world.review_queue_consumer(database=stack.database)
    delivered = stack.world.review_queue_event(detection)
    assert stack.world.deliver_review_queue(consumer, delivered, database=stack.database)
    service = ReviewQueueService(
        database=stack.database,
        writer=stack.writer,
        authorizer=stack.world.stack.authz.authorizer,
        repository=repository,
        clock=stack.world.stack.authz.sessions.clock,
    )
    resolved = stack.run(
        service.resolve(
            stack.context(coordinator),
            detection.subject_id,
            outcome=resolution,
            reason_category=category,
            reason_text=REASON,
        )
    )
    before = stack.world.finding_state(detection)
    assert before is not None
    assert before["classification_count"] == 0
    assert before["current_classification_outcome"] is None
    assert stack.history(detection) == []

    counts = stack.counts(organization)
    rejected = stack.classify(
        coordinator,
        detection,
        "false_positive",
        "no_person_present",
        reinforced=False,
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail_code"] == "loop_reason_required"
    assert stack.counts(organization) == counts
    assert stack.world.finding_state(detection) == before

    accepted = stack.classify(
        coordinator,
        detection,
        "false_positive",
        "no_person_present",
        reinforced=True,
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert (body["lifecycle_state"], body["effect_on_loop"]) == (state, effect)
    assert (body["sequence_in_subject"], body["classification_count"]) == (1, 1)
    assert body["is_reclassification"] is False
    assert body["reinforced_reason"] is True
    (record,) = stack.classifications(detection)
    assert record["content"]["reinforced_reason"] is True
    assert record["content"].get("previous_classification_record_id") is None
    (history,) = stack.history(detection)
    assert history["reinforced_reason"] is True
    assert history["sequence_in_subject"] == 1
    record_id = uuid.UUID(body["record_id"])
    assert stack.events(organization, record_id) == [event]
    (payload_row,) = stack.fetch(
        "SELECT payload::text AS payload FROM shared.outbox_event"
        " WHERE organization_id = $1 AND payload ->> 'record_id' = $2",
        organization.organization_id,
        str(record_id),
    )
    payload = json.loads(payload_row["payload"])
    if event == "finding_reclassified":
        assert payload["previous_outcome"] == previous
    labels = stack.labels(organization)
    assert {label["source_record_id"] for label in labels} == {resolved.record_id, record_id}

    records = stack.world.ledger_records(organization)
    resolution_before = next(item for item in records if item.record_id == resolved.record_id)
    rebuilt = fold(records)[organization.organization_id, organization.plant_id]
    projected = stack.world.finding_state(detection)
    assert projected is not None
    restored = rebuilt.findings[detection.subject_id]
    assert restored.lifecycle_state == projected["lifecycle_state"] == state
    assert restored.classification_count == projected["classification_count"] == 1
    assert restored.current_classification_outcome == "false_positive"

    # La clasificación siguiente sí tiene otra clasificación anterior: secuencia 2 y referencia.
    again = stack.classify(coordinator, detection, reinforced=True)
    assert again.status_code == 201, again.text
    assert again.json()["lifecycle_state"] == "open"
    assert again.json()["sequence_in_subject"] == 2
    assert again.json()["is_reclassification"] is True
    assert stack.classifications(detection)[1]["content"][
        "previous_classification_record_id"
    ] == str(record_id)
    assert (
        next(
            item
            for item in stack.world.ledger_records(organization)
            if item.record_id == resolved.record_id
        )
        == resolution_before
    )


@pytest.mark.parametrize("state", ["closed", "unclosable"])
def test_confirmed_review_can_be_classified_after_closure_without_inventing_history(
    stack: ClassificationStack, state: str
) -> None:
    """T19/T20 también pueden partir de una resolución sin ninguna clasificación previa."""
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    detection = stack.world.ingest_detection(organization)
    consumer, repository = stack.world.review_queue_consumer(database=stack.database)
    assert stack.world.deliver_review_queue(
        consumer, stack.world.review_queue_event(detection), database=stack.database
    )
    service = ReviewQueueService(
        database=stack.database,
        writer=stack.writer,
        authorizer=stack.world.stack.authz.authorizer,
        repository=repository,
        clock=stack.world.stack.authz.sessions.clock,
    )
    stack.run(
        service.resolve(
            stack.context(coordinator),
            detection.subject_id,
            outcome="review_confirmed",
            reason_category="real_coexistence",
            reason_text=REASON,
        )
    )
    if state == "closed":
        stack.close(detection, stack.assign_action(detection))
    else:
        stack.declare_unclosable(detection)
    original_records = stack.subject_records(detection)
    accepted = stack.classify(
        coordinator,
        detection,
        "false_positive",
        "no_person_present",
        reinforced=True,
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["lifecycle_state"] == state
    assert accepted.json()["closed_with_later_different_classification"] is True
    assert accepted.json()["reinforced_reason"] is True
    assert accepted.json()["classification_count"] == 1
    assert stack.subject_records(detection)[:-1] == original_records
    (event,) = stack.fetch(
        "SELECT payload::text AS payload FROM shared.outbox_event"
        " WHERE organization_id = $1 AND payload ->> 'record_id' = $2",
        organization.organization_id,
        accepted.json()["record_id"],
    )
    assert json.loads(event["payload"])["previous_outcome"] == "confirmed"
    rebuilt = fold(stack.world.ledger_records(organization))[
        organization.organization_id, organization.plant_id
    ].findings[detection.subject_id]
    assert rebuilt.lifecycle_state == state
    assert rebuilt.closed_with_later_different_classification is True
