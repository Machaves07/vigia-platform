"""``email_grouping`` y ``digest_weekly`` contra PostgreSQL 16 real (TASK-348; BR-LAZ-125, 126,
130 y 131; PR-LAZ-42; NFR-LAZ-13).

Contra ``notification_world`` (reutilizado de TASK-324): entregas de correo sembradas
directamente (``NotificationWorld.seed_delivery``), para probar la agrupación genérica sobre
cualquier urgencia, también ``digest`` (que el catálogo de hoy no combina todavía con el canal de
correo; ver la nota de ``notifications.digest``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, Senders, StubSender, notification_world
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import NotificationDeliveryStatus, NotificationUrgency
from vigia_platform.notifications.adapters.postgres.tables import (
    EMAIL_MESSAGE,
    USER_NOTIFICATION_PREFERENCE,
)
from vigia_platform.notifications.delivery import EMAIL_CIRCUIT_OPEN
from vigia_platform.notifications.digest import DigestWeeklyTask
from vigia_platform.notifications.grouping import (
    DIGEST_EMAIL_KIND,
    EMAIL_GROUPED_KIND,
    ORGANIZATION_EMAIL_CAP_PER_HOUR,
    EmailGroupingTask,
    candidates_statement,
)
from vigia_platform.notifications.preferences import preference_for
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

_DEFAULT_GROUPING_HOURS = 4


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "grouping_tasks") as built:
        yield built


def _grouping_task(world: NotificationWorld, *, circuit: str = "closed") -> EmailGroupingTask:
    return EmailGroupingTask(
        configuration=PostgresLoopConfigurationRepository(world.database),
        senders=Senders(StubSender(circuit)),  # type: ignore[arg-type]
        clock=world.clock,
    )


def _digest_task(world: NotificationWorld, *, circuit: str = "closed") -> DigestWeeklyTask:
    return DigestWeeklyTask(senders=Senders(StubSender(circuit)), clock=world.clock)  # type: ignore[arg-type]


def _coordinator(
    world: NotificationWorld, organization_id: uuid.UUID, zone_id: uuid.UUID
) -> uuid.UUID:
    return world.add_member(organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id).user_id


def _digest_batches(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT * FROM notifications.digest_batch WHERE organization_id = $1", organization_id
    )


def _email_messages(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT * FROM notifications.email_message WHERE organization_id = $1"
        " AND kind = 'digest' ORDER BY queued_at",
        organization_id,
    )


def _delivery_for(world: NotificationWorld, notification_id: uuid.UUID) -> dict[str, Any]:
    rows = world.fetch(
        "SELECT * FROM notifications.notification_delivery WHERE notification_id = $1"
        " AND channel = 'email'",
        notification_id,
    )
    assert len(rows) == 1
    return rows[0]


def _run(
    world: NotificationWorld, task: EmailGroupingTask | DigestWeeklyTask, context: Any
) -> None:
    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            await task.run(transaction)

    world.run(run())


def _seed_sent_messages(
    world: NotificationWorld,
    context: Any,
    organization_id: uuid.UUID,
    filler_recipient: uuid.UUID,
    count: int,
) -> None:
    """``count`` ``EmailMessage`` de ``kind = digest`` ya "enviados" esta hora, para dejar el
    presupuesto de la organización justo en el borde del tope (NFR-LAZ-13) sin tener que cerrar
    ``count`` ventanas reales."""
    now = world.clock.now()

    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            for _ in range(count):
                await transaction.execute(
                    EMAIL_MESSAGE.insert().values(
                        message_id=uuid.uuid4(),
                        organization_id=organization_id,
                        to_user_id=filler_recipient,
                        subject_es="Relleno",
                        body_es="Relleno",
                        kind=DIGEST_EMAIL_KIND,
                        related_ids=[],
                        queued_at=now,
                        status="queued",
                    )
                )

    world.run(run())


def test_email_grouping_groups_pending_normal_deliveries_after_the_configured_hours(
    world: NotificationWorld,
) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    ids = [
        world.seed_delivery(
            organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
        )
        for _ in range(3)
    ]
    task = _grouping_task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    _run(world, task, context)
    # Dentro de la misma hora, todavía no toca cerrar la ventana (email_grouping_hours = 4).
    assert _digest_batches(world, organization_id) == []
    world.clock.advance(int(timedelta(hours=_DEFAULT_GROUPING_HOURS, minutes=1).total_seconds()))
    _run(world, task, context)
    batches = _digest_batches(world, organization_id)
    assert len(batches) == 1
    assert batches[0]["kind"] == EMAIL_GROUPED_KIND
    assert batches[0]["item_count"] == 3
    assert sorted(batches[0]["notification_ids"]) == sorted(ids)
    assert batches[0]["sent_at"] is not None
    messages = _email_messages(world, organization_id)
    assert len(messages) == 1
    assert messages[0]["kind"] == DIGEST_EMAIL_KIND
    for notification_id in ids:
        delivery = _delivery_for(world, notification_id)
        assert delivery["digest_batch_id"] == batches[0]["digest_batch_id"]


def test_immediate_urgency_is_never_grouped(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    immediate_id = world.seed_delivery(
        organization_id, recipient, urgency="immediate", plant_id=plant_id, zone_id=zone_id
    )
    task = _grouping_task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    world.clock.advance(int(timedelta(days=2).total_seconds()))
    _run(world, task, context)
    assert _digest_batches(world, organization_id) == []
    delivery = _delivery_for(world, immediate_id)
    assert delivery["digest_batch_id"] is None
    assert delivery["status"] == NotificationDeliveryStatus.PENDING.value


def test_silenced_preference_suppresses_without_a_batch(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    context = world.event_context(world.event(organization_id, "finding_received", {}))

    async def silence() -> None:
        async with world.database.transaction(context) as transaction:
            await preference_for(transaction, recipient, world.clock)
            pref = USER_NOTIFICATION_PREFERENCE
            await transaction.execute(
                update(pref).where(pref.c.user_id == recipient).values(email_enabled_normal=False)
            )

    world.run(silence())
    notification_id = world.seed_delivery(
        organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    world.clock.advance(int(timedelta(days=2).total_seconds()))
    task = _grouping_task(world)
    _run(world, task, context)
    assert _digest_batches(world, organization_id) == []
    delivery = _delivery_for(world, notification_id)
    assert delivery["status"] == NotificationDeliveryStatus.SUPPRESSED_BY_PREFERENCE.value
    # El aviso en la aplicación nunca se toca (BR-LAZ-125): sigue en notification.notification.
    assert world.fetch(
        "SELECT 1 FROM notifications.notification WHERE notification_id = $1", notification_id
    )


def test_email_circuit_open_degrades_without_losing_the_notification(
    world: NotificationWorld,
) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    notification_id = world.seed_delivery(
        organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    world.clock.advance(int(timedelta(days=2).total_seconds()))
    task = _grouping_task(world, circuit="open")
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    _run(world, task, context)
    batches = _digest_batches(world, organization_id)
    assert len(batches) == 1
    assert batches[0]["status"] == "degraded"
    assert batches[0]["sent_at"] is None
    assert _email_messages(world, organization_id) == []
    delivery = _delivery_for(world, notification_id)
    assert delivery["status"] == NotificationDeliveryStatus.DEGRADED_APP_ONLY.value
    assert delivery["failure_code"] == EMAIL_CIRCUIT_OPEN
    # Nada se pierde: el aviso sigue en la aplicación.
    assert world.fetch(
        "SELECT 1 FROM notifications.notification WHERE notification_id = $1", notification_id
    )


def test_six_daily_groupings_never_exceed_six_emails_per_user_per_day(
    world: NotificationWorld,
) -> None:
    """``email_grouping`` sondea cada hora (su propio horario) pero solo cierra la ventana cada
    ``email_grouping_hours`` (4h por defecto): en 24h de sondeos horarios con avisos nuevos cada
    hora, nunca más de 24 / 4 = 6 correos agrupados para el mismo destinatario (BR-LAZ-125)."""
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    task = _grouping_task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    for _ in range(24):  # una pasada horaria, como el horario real de la tarea.
        world.seed_delivery(
            organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
        )
        _run(world, task, context)
        world.clock.advance(int(timedelta(hours=1).total_seconds()))
    messages = _email_messages(world, organization_id)
    assert len(messages) <= 6, messages


def test_digest_weekly_groups_digest_urgency_after_a_week(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = _coordinator(world, organization_id, zone_id)
    notification_id = world.seed_delivery(
        organization_id, recipient, urgency="digest", plant_id=plant_id, zone_id=zone_id
    )
    task = _digest_task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    _run(world, task, context)
    assert _digest_batches(world, organization_id) == []  # todavía no es el día configurado
    world.clock.advance(int(timedelta(days=8).total_seconds()))
    _run(world, task, context)
    batches = [
        row for row in _digest_batches(world, organization_id) if row["kind"] == "weekly_manager"
    ]
    assert len(batches) == 1
    assert batches[0]["notification_ids"] == [notification_id]
    delivery = _delivery_for(world, notification_id)
    assert delivery["digest_batch_id"] == batches[0]["digest_batch_id"]


def test_organization_cap_is_never_exceeded_with_concurrent_recipients(
    world: NotificationWorld,
) -> None:
    """Garantía de tope: 200 ``EmailMessage`` de ``kind = digest`` por hora y organización, con
    el presupuesto justo en el borde (faltan 5 para el tope) y 12 destinatarios cerrando su
    ventana a la vez (NFR-LAZ-13): como mucho 5 de los 12 entran; el resto queda pendiente, sin
    reclamar y sin lote enviado (PR-LAZ-42, nada se pierde), y una ejecución posterior tras
    liberarse el presupuesto los agrupa."""
    organization_id, plant_id, zone_id = world.new_organization()
    recipients = [
        world.add_member(organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id).user_id
        for _ in range(12)
    ]
    notification_by_recipient = {
        recipient: world.seed_delivery(
            organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
        )
        for recipient in recipients
    }
    world.clock.advance(int(timedelta(hours=_DEFAULT_GROUPING_HOURS, minutes=1).total_seconds()))
    remaining_budget = 5
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    _seed_sent_messages(
        world,
        context,
        organization_id,
        world.owners[organization_id],
        ORGANIZATION_EMAIL_CAP_PER_HOUR - remaining_budget,
    )
    task = _grouping_task(world)

    async def run_one(recipient: uuid.UUID) -> None:
        async with world.database.transaction(context) as transaction:
            all_rows = (
                await transaction.execute(
                    candidates_statement(organization_id, NotificationUrgency.NORMAL)
                )
            ).all()
            own_rows = [row for row in all_rows if row.recipient_user_id == recipient]
            await task._process_recipient(transaction, recipient, own_rows)

    async def scenario() -> None:
        await asyncio.gather(*(run_one(recipient) for recipient in recipients))

    world.run(scenario())
    messages = _email_messages(world, organization_id)
    assert len(messages) <= ORGANIZATION_EMAIL_CAP_PER_HOUR
    sent_recipients = {message["to_user_id"] for message in messages} & set(recipients)
    # El tope deja pasar exactamente lo que queda de presupuesto, nunca más.
    assert len(sent_recipients) == remaining_budget
    stuck = set(recipients) - sent_recipients
    assert len(stuck) == len(recipients) - remaining_budget
    for recipient in stuck:
        # Quien no cupo en el tope queda pendiente, sin lote y sin reclamar: nada se pierde
        # (PR-LAZ-42) y no se duplica en la siguiente ejecución.
        delivery = _delivery_for(world, notification_by_recipient[recipient])
        assert delivery["status"] == NotificationDeliveryStatus.PENDING.value
        assert delivery["digest_batch_id"] is None
    # Pasada la hora del tope, el presupuesto se libera y la siguiente ejecución agrupa lo que
    # había quedado pendiente: no se pierde ni se duplica.
    world.clock.advance(int(timedelta(hours=1, minutes=1).total_seconds()))
    _run(world, task, context)
    messages_after = _email_messages(world, organization_id)
    sent_recipients_after = {message["to_user_id"] for message in messages_after} & set(recipients)
    assert sent_recipients_after == set(recipients)
    for recipient in recipients:
        delivery = _delivery_for(world, notification_by_recipient[recipient])
        assert delivery["digest_batch_id"] is not None
