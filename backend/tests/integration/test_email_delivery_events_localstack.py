"""``email_delivery_events`` contra una cola de SQS real de LocalStack (TASK-325; LC-LAZ-34;
PR-LAZ-58) y PostgreSQL 16 real.

Una cola efímera con su propia cola muerta (``RedrivePolicy``, ``maxReceiveCount`` pequeño para
que la prueba no espere de más) y ``VisibilityTimeout = 0`` (un mensaje no borrado vuelve a estar
visible de inmediato, sin esperar el tiempo de producción): un mensaje de rebote duro termina en
la marca del usuario y en ``DeleteMessage``; uno malformado, agotado ``maxReceiveCount``, termina
en la cola muerta sin que el consumidor haga nada por su cuenta (la redirección es de
infraestructura).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from tests.factories import uuid7
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.loop.http.client_factory import EgressAllowlist, HttpClientFactory
from vigia_platform.notifications.adapters.ses.delivery_events import EmailDeliveryEventsConsumer
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

pytestmark = pytest.mark.integration

MAX_RECEIVE_COUNT = 2


class _AcceptingSesClient:
    """``send_email`` siempre acepta, sin red: solo para dejar un ``EmailMessage`` real
    ``sent`` con su clave de idempotencia reclamada, como haría ``SesEmailSender`` de verdad."""

    def send_email(self, **kwargs: Any) -> dict[str, str]:
        return {"MessageId": f"ses-{uuid.uuid4().hex}"}


def _bounce_body(provider_message_id: str) -> str:
    inner = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": "Permanent", "bouncedRecipients": []},
        "mail": {"messageId": provider_message_id},
    }
    return json.dumps({"Type": "Notification", "Message": json.dumps(inner)})


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "email_delivery_events_localstack") as built:
        yield built


@pytest.fixture
def queues(localstack_endpoint: LocalStackEndpoint) -> Iterator[tuple[Any, str, str]]:
    """Una cola efímera con su cola muerta, como ``vigia-email-events-<entorno>`` (sin esperar el
    tiempo de producción: visibilidad 0)."""
    sqs = localstack_endpoint.aws_client("sqs")
    suffix = uuid.uuid4().hex[:12]
    dlq_url = sqs.create_queue(QueueName=f"vigia-email-events-dlq-test-{suffix}")["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    queue_url = sqs.create_queue(
        QueueName=f"vigia-email-events-test-{suffix}",
        Attributes={
            "VisibilityTimeout": "0",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEIVE_COUNT}
            ),
        },
    )["QueueUrl"]
    try:
        yield sqs, queue_url, dlq_url
    finally:
        sqs.delete_queue(QueueUrl=queue_url)
        sqs.delete_queue(QueueUrl=dlq_url)


def _consumer(world: NotificationWorld, sqs: Any, queue_url: str) -> EmailDeliveryEventsConsumer:
    return EmailDeliveryEventsConsumer(
        sqs=sqs,
        queue_url=queue_url,
        database=world.database,
        provider_context=world.contexts.provider_audit_context(),
        contexts=world.contexts,
        clock=world.clock,
    )


def _dummy_event(world: NotificationWorld) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid.uuid4(),
        organization_id=world.provider_id,
        plant_id=None,
        event_name="email_delivery_events_poll",
        partition_key=partition_key(world.provider_id, None),
        ledger_sequence=None,
        payload={},
        correlation_id=uuid.uuid4(),
        created_at=world.clock.now(),
    )


def _run_once(world: NotificationWorld, consumer: EmailDeliveryEventsConsumer) -> None:
    world.run(consumer(_dummy_event(world), None))  # type: ignore[arg-type]


def _seed_sent_message(
    world: NotificationWorld, organization_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    """Un ``EmailMessage`` real ``sent`` con su clave de idempotencia reclamada, por el camino
    real de ``SesEmailSender`` (sin red: cliente doble que siempre acepta)."""
    allowlist = EgressAllowlist(
        assistant_host="api.anthropic.com", email_host="email.us-east-1.amazonaws.com"
    )
    sender = SesEmailSender(
        database=world.database,
        http_clients=HttpClientFactory(allowlist),
        region_name="us-east-1",
        sender_email="no-responder@mail.vigia.test",
        configuration_set_name="vigia-email-test",
        clock=world.clock,
    )
    sender._client = _AcceptingSesClient()
    context = world.contexts.context_from_event(
        OutboxEvent(
            event_id=uuid7(),
            organization_id=organization_id,
            plant_id=None,
            event_name="dummy",
            partition_key=partition_key(organization_id, None),
            ledger_sequence=None,
            payload={},
            correlation_id=uuid7(),
            created_at=world.clock.now(),
        ),
        unit=ActorUnit.U04,
    )
    message = InvitationEmail(
        organization_id=organization_id,
        to_user_id=user_id,
        to_email="persona@example.test",
        subject_es="Invitación a Vigía",
        body_es="Cuerpo sintético",
        related_ids=(uuid.uuid4(),),
    )
    assert world.run(sender.send(context, message)) == "queued"
    row = world.fetch(
        "SELECT provider_message_id FROM notifications.email_message"
        " WHERE organization_id = $1 AND to_user_id = $2",
        organization_id,
        user_id,
    )[0]
    provider_message_id: str = row["provider_message_id"]
    return provider_message_id


def test_a_bounce_message_marks_the_user_and_is_deleted(
    world: NotificationWorld, queues: tuple[Any, str, str]
) -> None:
    sqs, queue_url, _dlq_url = queues
    organization_id, _plant_id, _zone_id = world.new_organization()
    user_id = world.add_member(
        organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
    ).user_id
    provider_message_id = _seed_sent_message(world, organization_id, user_id)

    sqs.send_message(QueueUrl=queue_url, MessageBody=_bounce_body(provider_message_id))
    _run_once(world, _consumer(world, sqs, queue_url))

    remaining = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
    assert remaining.get("Messages", []) == []

    updated = world.fetch(
        "SELECT status, bounce_kind FROM notifications.email_message"
        " WHERE organization_id = $1 AND to_user_id = $2",
        organization_id,
        user_id,
    )[0]
    assert updated["status"] == "failed" and updated["bounce_kind"] == "hard"


def test_a_malformed_message_ends_in_the_dead_letter_queue(
    world: NotificationWorld, queues: tuple[Any, str, str]
) -> None:
    sqs, queue_url, dlq_url = queues
    consumer = _consumer(world, sqs, queue_url)
    sqs.send_message(QueueUrl=queue_url, MessageBody="no es JSON en absoluto")

    for _ in range(MAX_RECEIVE_COUNT):
        _run_once(world, consumer)

    source_remaining = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
    assert source_remaining.get("Messages", []) == []
    dead_lettered = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=10)
    assert len(dead_lettered.get("Messages", [])) == 1
