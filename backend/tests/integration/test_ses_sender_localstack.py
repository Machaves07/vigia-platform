"""``SesEmailSender`` contra LocalStack (TASK-325; LC-LAZ-33) y PostgreSQL 16 real.

**Limitación medida de LocalStack, edición comunitaria** (la única permitida por el proyecto:
la Pro exige licencia de pago, fuera de lo que este repositorio instala): la operación
``SendEmail`` de la API ``sesv2`` que ``loop.http.client_factory.email_client`` usa (y que esta
tarea no cambia; la API v1 es de otra decisión, VIG-210) responde
``InternalFailure`` ("The API for service sesv2 is ... not yet been emulated by LocalStack"):
solo la API clásica (v1, ``ses.send_email``) está emulada en la edición comunitaria. Medido a
mano contra el mismo contenedor que esta prueba usa (``tests/integration/conftest.py``
``aws_client("sesv2")`` y ``aws_client("ses")``): v1 acepta y devuelve ``MessageId``, v2
responde el fallo de arriba.

Esta prueba no puede ejercer entonces la ruta de éxito de ``sesv2`` contra una red de verdad (la
ruta de éxito, con un cliente doble, ya está en
``tests/properties/laz/test_ses_sender_resend_limit_concurrency.py``, con PostgreSQL real). Lo
que **sí** es una prueba de integración legítima y de valor aquí es que ``SesEmailSender``
trata un fallo real de un cliente de ``botocore`` de verdad (no un doble) exactamente como
``FS-LAZ-02`` exige: marca ``failed``, abre el circuito y no se pierde nada (``EmailMessage``
escrito en la admisión sigue en la base).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests.factories import uuid7
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.loop.http.client_factory import EgressAllowlist, HttpClientFactory
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "ses_sender_localstack") as built:
        yield built


def _sender(world: NotificationWorld, localstack_endpoint: LocalStackEndpoint) -> SesEmailSender:
    allowlist = EgressAllowlist(
        assistant_host="api.anthropic.com", email_host="email.us-east-1.amazonaws.com"
    )
    sender = SesEmailSender(
        database=world.database,
        http_clients=HttpClientFactory(allowlist),
        region_name=localstack_endpoint.region,
        sender_email="no-responder@mail.vigia.test",
        configuration_set_name="vigia-email-test",
        clock=world.clock,
    )
    # El cliente real de ``sesv2`` contra LocalStack (no un doble): el mismo constructor que
    # las demás pruebas de LocalStack usan (tiempos de espera VIG003).
    sender._client = localstack_endpoint.aws_client("sesv2")
    return sender


def _context(world: NotificationWorld, organization_id: uuid.UUID) -> object:
    event = OutboxEvent(
        event_id=uuid7(),
        organization_id=organization_id,
        plant_id=None,
        event_name="dummy",
        partition_key=partition_key(organization_id, None),
        ledger_sequence=None,
        payload={},
        correlation_id=uuid7(),
        created_at=world.clock.now(),
    )
    return world.contexts.context_from_event(event, unit=ActorUnit.U04)


def test_a_real_client_failure_marks_failed_and_opens_the_circuit_without_losing_anything(
    world: NotificationWorld, localstack_endpoint: LocalStackEndpoint
) -> None:
    organization_id, _plant_id, _zone_id = world.new_organization()
    user_id = world.add_member(
        organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
    ).user_id
    sender = _sender(world, localstack_endpoint)
    message = InvitationEmail(
        organization_id=organization_id,
        to_user_id=user_id,
        to_email="persona@example.test",
        subject_es="Invitación a Vigía",
        body_es="Cuerpo sintético",
        related_ids=(uuid.uuid4(),),
    )

    result = world.run(sender.send(_context(world, organization_id), message))

    assert result == "unavailable"
    assert sender.circuit_state() == "open"
    rows = world.fetch(
        "SELECT * FROM notifications.email_message WHERE organization_id = $1 AND to_user_id = $2",
        organization_id,
        user_id,
    )
    assert len(rows) == 1  # El EmailMessage de la admisión no se pierde (P4, BR-LAZ-130).
    row = rows[0]
    assert row["status"] == "failed"
    assert row["provider_message_id"] is None
