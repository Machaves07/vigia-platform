"""Límite exacto de reenvíos de ``SesEmailSender`` bajo peticiones concurrentes (TASK-325;
PAT-LAZ-ESC-02; LC-LAZ-33). Garantía: nunca más de 3 envíos por hora y usuario, también con varias
conexiones a la vez.

Contra PostgreSQL 16 real como ``vigia_app`` (``notification_world``, reutilizado de TASK-324),
con un cliente de SES doble que siempre "acepta" el envío (sin red): diez llamadas simultáneas a
``send`` del mismo usuario (``asyncio.gather``) dejan aceptadas exactamente
``RESEND_LIMIT_PER_USER``, nunca más. La mutación (``_admit`` sin ningún candado consultivo,
cediendo el control entre la cuenta y el ``INSERT``) se prueba en
``test_without_any_lock_the_limit_is_exceeded``: cae en rojo, repetida varias veces.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import insert

from tests.factories import uuid7 as fresh_uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.loop.http.client_factory import EgressAllowlist, HttpClientFactory
from vigia_platform.notifications.adapters.postgres.tables import EMAIL_MESSAGE
from vigia_platform.notifications.adapters.ses.sender import (
    RESEND_LIMIT_PER_ORGANIZATION,
    RESEND_LIMIT_PER_USER,
    RESEND_WINDOW,
    SesEmailSender,
    _Admitted,
    _count_since,
)
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key

pytestmark = pytest.mark.integration

REPETITIONS = 3


class _FakeSesClient:
    """``send_email`` siempre acepta, sin red; cuenta las llamadas."""

    def __init__(self) -> None:
        self.calls = 0

    def send_email(self, **kwargs: Any) -> dict[str, str]:
        self.calls += 1
        return {"MessageId": f"ses-{self.calls}-{uuid.uuid4().hex[:8]}"}


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "ses_resend_limit") as built:
        yield built


def _sender(world: NotificationWorld) -> SesEmailSender:
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
    sender._client = _FakeSesClient()
    return sender


def _context(world: NotificationWorld, organization_id: uuid.UUID) -> Any:
    event = OutboxEvent(
        event_id=fresh_uuid7(),
        organization_id=organization_id,
        plant_id=None,
        event_name="dummy",
        partition_key=partition_key(organization_id, None),
        ledger_sequence=None,
        payload={},
        correlation_id=fresh_uuid7(),
        created_at=world.clock.now(),
    )
    return world.contexts.context_from_event(event, unit=ActorUnit.U04)


def _message(organization_id: uuid.UUID, user_id: uuid.UUID) -> InvitationEmail:
    return InvitationEmail(
        organization_id=organization_id,
        to_user_id=user_id,
        to_email="persona@example.test",
        subject_es="Invitación a Vigía",
        body_es="Cuerpo sintético",
        related_ids=(uuid.uuid4(),),
    )


async def _ten_concurrent_sends(
    world: NotificationWorld, sender: SesEmailSender, organization_id: uuid.UUID, user_id: uuid.UUID
) -> list[str]:
    context = _context(world, organization_id)
    message = _message(organization_id, user_id)
    return list(await asyncio.gather(*(sender.send(context, message) for _ in range(10))))


@pytest.mark.parametrize("attempt", range(REPETITIONS))
def test_ten_concurrent_sends_accept_exactly_the_user_limit(
    world: NotificationWorld, attempt: int
) -> None:
    organization_id, _plant_id, _zone_id = world.new_organization()
    user_id = world.add_member(
        organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
    ).user_id
    sender = _sender(world)
    results = world.run(_ten_concurrent_sends(world, sender, organization_id, user_id))
    assert results.count("queued") == RESEND_LIMIT_PER_USER
    assert results.count("unavailable") == 10 - RESEND_LIMIT_PER_USER


async def _admit_without_any_lock(
    self: SesEmailSender, context: Any, message: InvitationEmail
) -> _Admitted | None:
    """Copia de ``SesEmailSender._admit`` sin ningún candado consultivo (la mutación): con un
    solo candado de organización, diez peticiones de la misma organización ya quedan serializadas
    por él aunque se quite el del usuario, así que la mutación observable quita los dos. Cede el
    control (``asyncio.sleep(0)``) entre la cuenta y el ``INSERT`` para que otra corrutina lea el
    mismo recuento todavía sin confirmar."""
    now: datetime = self._clock.now()
    window_start = now - RESEND_WINDOW
    organization_id = message.organization_id
    to_user_id = message.to_user_id
    async with self._database.transaction(context) as transaction:
        organization_count = (
            await transaction.execute(_count_since(organization_id, window_start, to_user_id=None))
        ).scalar_one()
        if organization_count >= RESEND_LIMIT_PER_ORGANIZATION:
            return None
        user_count = (
            await transaction.execute(
                _count_since(organization_id, window_start, to_user_id=to_user_id)
            )
        ).scalar_one()
        if user_count >= RESEND_LIMIT_PER_USER:
            return None
        await asyncio.sleep(0)
        message_id = uuid7(self._clock)
        await transaction.execute(
            insert(EMAIL_MESSAGE).values(
                message_id=message_id,
                organization_id=organization_id,
                to_user_id=to_user_id,
                subject_es=message.subject_es,
                body_es=message.body_es,
                kind=message.kind,
                related_ids=list(message.related_ids),
                queued_at=now,
                status="queued",
            )
        )
    return _Admitted(message_id, now)


@pytest.mark.parametrize("attempt", range(REPETITIONS))
def test_without_any_lock_the_limit_is_exceeded(world: NotificationWorld, attempt: int) -> None:
    organization_id, _plant_id, _zone_id = world.new_organization()
    user_id = world.add_member(
        organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
    ).user_id
    sender = _sender(world)
    with patch.object(SesEmailSender, "_admit", _admit_without_any_lock):
        results = world.run(_ten_concurrent_sends(world, sender, organization_id, user_id))
    assert results.count("queued") > RESEND_LIMIT_PER_USER, results
