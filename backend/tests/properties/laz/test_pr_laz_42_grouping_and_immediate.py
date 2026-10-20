"""PR-LAZ-42: lo agrupado por ``email_grouping`` es exactamente lo ``normal`` no silenciado del
periodo; nada ``immediate`` se agrupa ni se silencia; con el correo caído, todo queda en la
aplicación con su estado declarado y nada se pierde (TASK-348; BR-LAZ-125, 126 y 130).

Contra PostgreSQL 16 real (``notification_world``, reutilizado de TASK-324). Por cada ejemplo: una
secuencia de avisos de urgencia ``normal`` o ``immediate`` para un mismo destinatario (los
``normal`` sembrados directamente como entregas pendientes, los ``immediate`` entregados por el
motor real sobre el evento ``finding_overdue``, que el catálogo fija como ``immediate`` siempre) y
una preferencia (``preference_sets``) que puede silenciar el correo ``normal``. Tras correr
``email_grouping`` tras su ventana:

- el conjunto agrupado (``DigestBatch.notification_ids``) es exactamente el de avisos ``normal``
  **no** silenciados;
- los silenciados quedan ``suppressed_by_preference``, nunca en un lote;
- ningún aviso ``immediate`` aparece jamás en ``digest_batch`` ni en ``suppressed_by_preference``
  (su entrega, inmediata, no la toca esta tarea en absoluto);
- con el ``EmailSenderPort`` caído (circuito abierto), el lote se escribe igual pero sin
  ``EmailMessage``, y todo aviso (normal o inmediato) sigue existiendo en
  ``notifications.notification``: nada se pierde.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import update

from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, Senders, StubSender, notification_world
from tests.properties.laz.strategies.notifications import preference_sets
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.notifications.adapters.postgres.tables import USER_NOTIFICATION_PREFERENCE
from vigia_platform.notifications.grouping import EmailGroupingTask
from vigia_platform.notifications.preferences import preference_for
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

EXAMPLES = 20


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "pr_laz_42") as built:
        yield built


def _task(world: NotificationWorld, *, circuit: str = "closed") -> EmailGroupingTask:
    return EmailGroupingTask(
        configuration=PostgresLoopConfigurationRepository(world.database),
        senders=Senders(StubSender(circuit)),  # type: ignore[arg-type]
        clock=world.clock,
    )


def _digest_batches(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT * FROM notifications.digest_batch WHERE organization_id = $1", organization_id
    )


def _deliveries(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT nd.*, n.urgency FROM notifications.notification_delivery AS nd"
        " JOIN notifications.notification AS n"
        " ON n.notification_id = nd.notification_id AND n.created_at = nd.notification_created_at"
        " WHERE nd.organization_id = $1 AND nd.channel = 'email'",
        organization_id,
    )


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    urgencies=st.lists(st.sampled_from(("normal", "immediate")), min_size=1, max_size=8),
    preferences=preference_sets(min_size=1, max_size=1),
    circuit=st.sampled_from(("closed", "open")),
)
def test_grouping_is_exactly_normal_unsilenced_and_never_touches_immediate(
    world: NotificationWorld,
    urgencies: list[str],
    preferences: tuple[Any, ...],
    circuit: str,
) -> None:
    preference = preferences[0]
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = world.add_member(
        organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
    ).user_id
    context = world.event_context(world.event(organization_id, "finding_received", {}))

    async def set_preference() -> None:
        async with world.database.transaction(context) as transaction:
            await preference_for(transaction, recipient, world.clock)
            await transaction.execute(
                update(USER_NOTIFICATION_PREFERENCE)
                .where(USER_NOTIFICATION_PREFERENCE.c.user_id == recipient)
                .values(email_enabled_normal=preference.email_enabled_normal)
            )

    world.run(set_preference())

    engine = world.engine(senders=Senders(StubSender()))  # type: ignore[arg-type]
    normal_ids = []
    for urgency in urgencies:
        if urgency == "normal":
            normal_ids.append(
                world.seed_delivery(
                    organization_id,
                    recipient,
                    urgency="normal",
                    plant_id=plant_id,
                    zone_id=zone_id,
                )
            )
        else:
            event = world.event(
                organization_id,
                "finding_overdue",
                {
                    "zone_id": str(zone_id),
                    "subject_kind": "finding",
                    "subject_id": str(uuid.uuid4()),
                    "record_id": str(uuid.uuid4()),
                    "days_overdue": 3,
                    "silent_days": 14,
                },
                plant_id=plant_id,
            )
            world.deliver(engine, event, context=context)

    world.clock.advance(int(timedelta(hours=5).total_seconds()))
    task = _task(world, circuit=circuit)

    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            await task.run(transaction)

    world.run(run())

    batches = _digest_batches(world, organization_id)
    grouped_ids: set[uuid.UUID] = set()
    for batch in batches:
        grouped_ids.update(batch["notification_ids"])

    all_rows = _deliveries(world, organization_id)
    immediate_rows = [row for row in all_rows if row["urgency"] == "immediate"]
    normal_rows = [row for row in all_rows if row["urgency"] == "normal"]

    # Nada immediate se agrupa ni se silencia.
    for row in immediate_rows:
        assert row["notification_id"] not in grouped_ids
        assert row["status"] != "suppressed_by_preference"

    if preference.email_enabled_normal:
        assert grouped_ids == set(normal_ids), (grouped_ids, normal_ids)
        for row in normal_rows:
            assert row["status"] != "suppressed_by_preference"
    else:
        assert grouped_ids == set()
        for row in normal_rows:
            assert row["status"] == "suppressed_by_preference"

    if circuit == "open" and preference.email_enabled_normal and normal_ids:
        for batch in batches:
            assert batch["sent_at"] is None
        messages = world.fetch(
            "SELECT 1 FROM notifications.email_message WHERE organization_id = $1"
            " AND kind = 'digest'",
            organization_id,
        )
        assert messages == []

    # Nada se pierde: todo aviso sigue existiendo en la aplicación.
    for notification_id in [*normal_ids, *(row["notification_id"] for row in immediate_rows)]:
        assert world.fetch(
            "SELECT 1 FROM notifications.notification WHERE notification_id = $1", notification_id
        )
