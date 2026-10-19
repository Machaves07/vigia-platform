"""FS-LAZ-02: con ``FakeEmailSender`` caído, el motor de notificaciones real deja todo
``degraded_app_only`` (visible, nada perdido) salvo ``integrity_compromised``, que sale igual
(TASK-325; PAT-LAZ-RES-04; BR-LAZ-127, 130).

Contra PostgreSQL 16 real (``NotificationWorld``, reutilizado de TASK-324), con un
``FakeEmailSender`` de verdad (no ``StubSender``) registrado en el motor: cinco minutos de caída
simulada (``fail_for``, el mismo reloj que ``world.clock``) abren su circuito; un evento
``immediate`` con correo normal queda ``degraded_app_only`` con ``email_circuit_open``, visible en
la aplicación; ``integrity_compromised`` (``bypasses_email_circuit``) se encola igual, con el
circuito todavía abierto.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, Senders, notification_world
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.notifications.adapters.ses.fake import FakeEmailSender
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "fs_laz_02") as built:
        yield built


def _probe_message(organization_id: uuid.UUID) -> InvitationEmail:
    return InvitationEmail(
        organization_id=organization_id,
        to_user_id=uuid.uuid4(),
        to_email="persona@example.test",
        subject_es="Sonda",
        body_es="Sonda sintética para abrir el circuito",
        related_ids=(uuid.uuid4(),),
    )


def test_degraded_app_only_and_integrity_compromised_bypasses_the_open_circuit(
    world: NotificationWorld,
) -> None:
    organization_id, _plant_id, zone_id = world.new_organization()
    world.add_member(organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id)
    world.add_member(organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id)
    sender = FakeEmailSender(clock=world.clock)
    engine = world.engine(senders=Senders(sender))  # type: ignore[arg-type]
    assert sender.circuit_state() == "closed"

    sender.fail_for(timedelta(minutes=5))
    # El circuito abre en el primer intento fallido, no antes (PAT-LAZ-RES-04): un envío
    # cualquiera lo dispara (aquí, la misma invitación que ``EmailSenderPort`` sirve a U-02).
    world.run(sender.send(object(), _probe_message(organization_id)))  # type: ignore[arg-type]
    assert sender.circuit_state() == "open"

    # Un immediate+correo normal (no bypasses_email_circuit): review_overdue, "phase": "first"
    # (el único nivel immediate+correo del catálogo que no es integrity_compromised, U04-H-07).
    overdue_event = world.event(
        organization_id,
        "review_overdue",
        {
            "zone_id": str(zone_id),
            "detection_id": str(uuid7()),
            "reminders_sent": 1,
            "phase": "first",
        },
        plant_id=None,
    )
    assert world.deliver(engine, overdue_event) == 1
    deliveries = world.deliveries(overdue_event.event_id)
    email_deliveries = [row for row in deliveries if row["channel"] == "email"]
    assert len(email_deliveries) == 1
    assert email_deliveries[0]["status"] == "degraded_app_only"
    assert email_deliveries[0]["failure_code"] == "email_circuit_open"
    # Nada perdido: el aviso sigue visible en la aplicación.
    in_app = [row for row in deliveries if row["channel"] == "in_app"]
    assert len(in_app) == 1 and in_app[0]["status"] == "sent"
    assert world.emails(overdue_event.event_id) == []  # degraded: ningún EmailMessage se crea.

    # integrity_compromised (bypasses_email_circuit): sale igual, con el circuito todavía abierto.
    assert sender.circuit_state() == "open"
    integrity_event = world.event(
        organization_id,
        "integrity_compromised",
        {"chain_kind": "ledger", "first_failed_sequence": 9, "verification_mode": "full"},
    )
    world.deliver(engine, integrity_event)
    integrity_deliveries = world.deliveries(integrity_event.event_id)
    client_email = [
        row
        for row in integrity_deliveries
        if row["channel"] == "email" and row["organization_id"] == organization_id
    ]
    assert len(client_email) == 1
    assert client_email[0]["status"] == "pending"  # QUEUED: no degradado, el circuito no cuenta.
    assert len(world.emails(integrity_event.event_id)) >= 1
