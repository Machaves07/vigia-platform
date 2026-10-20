"""PR-LAZ-48 (parte de agrupación): tras una caída, ``email_grouping`` y ``digest_weekly`` emiten
**un solo** envío con lo acumulado, nunca una ráfaga; dos ejecuciones concurrentes (arrendamiento
vencido artificialmente) no duplican el lote (TASK-348).

Contra PostgreSQL 16 real (``notification_world``, reutilizado de TASK-324): la garantía de «una
sola vez» no es una restricción única del lote (``DigestBatch`` se inserta sin condición: con dos
ejecuciones a la vez puede haber, de hecho, dos filas para la misma ventana), sino **reclamar**
las entregas: ``UPDATE notification_delivery ... WHERE digest_batch_id IS NULL`` toma el bloqueo
de fila de cada entrega; la ejecución que llega después, al evaluar la misma condición ya
confirmada la primera, no encuentra ninguna entrega libre, así que su lote se marca
``superseded`` y no envía nada (``vigia_app`` no puede borrarlo ni corregir ``notification_ids``:
solo ``sent_at`` y ``status`` son columnas actualizables, P4). La mutación
(``_close_window_without_the_claim_guard``) quita esa condición del ``UPDATE``: cae en rojo
(correo duplicado), repetida varias veces.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import update

from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, Senders, StubSender, notification_world
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.notifications.adapters.postgres.tables import (
    DIGEST_BATCH,
    NOTIFICATION_DELIVERY,
)
from vigia_platform.notifications.digest import WEEKLY_MANAGER_KIND, DigestWeeklyTask
from vigia_platform.notifications.grouping import (
    EMAIL_GROUPED_KIND,
    Candidate,
    DigestGrouperBase,
    EmailGroupingTask,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.ids import uuid7

pytestmark = pytest.mark.integration

REPETITIONS = 3


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "grouping_catch_up") as built:
        yield built


def _task(world: NotificationWorld) -> EmailGroupingTask:
    return EmailGroupingTask(
        configuration=PostgresLoopConfigurationRepository(world.database),
        senders=Senders(StubSender()),  # type: ignore[arg-type]
        clock=world.clock,
    )


def _digest_task(world: NotificationWorld) -> DigestWeeklyTask:
    return DigestWeeklyTask(senders=Senders(StubSender()), clock=world.clock)  # type: ignore[arg-type]


def _digest_batches(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT * FROM notifications.digest_batch WHERE organization_id = $1", organization_id
    )


def _email_messages(world: NotificationWorld, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    return world.fetch(
        "SELECT * FROM notifications.email_message WHERE organization_id = $1 AND kind = 'digest'",
        organization_id,
    )


def test_a_prolonged_outage_produces_exactly_one_batch_with_everything_accumulated(
    world: NotificationWorld,
) -> None:
    """48 h caído (reloj inyectado, no la pared): al volver, un solo lote con todo lo acumulado,
    nunca una ráfaga de varios lotes por el tiempo transcurrido."""
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = world.add_member(
        organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
    ).user_id
    task = _task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    ids = []
    for _ in range(5):
        ids.append(
            world.seed_delivery(
                organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
            )
        )
        world.clock.advance(int(timedelta(hours=10).total_seconds()))  # la tarea no corrió: caída
    # El reloj ya avanzó 48h+ desde el primer aviso pendiente: una sola pasada agrupa todo.

    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            await task.run(transaction)

    world.run(run())
    batches = _digest_batches(world, organization_id)
    assert len(batches) == 1, batches
    assert batches[0]["item_count"] == 5
    assert sorted(batches[0]["notification_ids"]) == sorted(ids)
    messages = _email_messages(world, organization_id)
    assert len(messages) == 1, messages


def test_a_prolonged_outage_produces_exactly_one_digest_weekly_batch_with_everything_accumulated(
    world: NotificationWorld,
) -> None:
    """Lo mismo que la prueba anterior, pero para ``digest_weekly`` (PR-LAZ-48 nombra las dos
    tareas): varias semanas caído acumulan avisos ``digest`` sin que ``digest_weekly`` corra; al
    volver, un solo lote con todo, nunca una ráfaga de varios lotes por el tiempo transcurrido."""
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = world.add_member(
        organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
    ).user_id
    task = _digest_task(world)
    context = world.event_context(world.event(organization_id, "finding_received", {}))
    ids = []
    for _ in range(5):
        ids.append(
            world.seed_delivery(
                organization_id, recipient, urgency="digest", plant_id=plant_id, zone_id=zone_id
            )
        )
        world.clock.advance(int(timedelta(days=8).total_seconds()))  # la tarea no corrió: caída
    # El reloj ya avanzó varias semanas desde el primer aviso pendiente: una sola pasada agrupa
    # todo (la sonda diaria de ``digest_weekly`` alcanza el día que tocaba tras la caída).

    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            await task.run(transaction)

    world.run(run())
    batches = [
        row for row in _digest_batches(world, organization_id) if row["kind"] == "weekly_manager"
    ]
    assert len(batches) == 1, batches
    assert batches[0]["item_count"] == 5
    assert sorted(batches[0]["notification_ids"]) == sorted(ids)
    messages = _email_messages(world, organization_id)
    assert len(messages) == 1, messages


def _window(world: NotificationWorld) -> tuple[Any, Any]:
    now = world.clock.now()
    return now - timedelta(hours=1), now


async def _close_window_without_the_claim_guard(
    self: DigestGrouperBase,
    transaction: Any,
    *,
    recipient_user_id: uuid.UUID,
    kind: str,
    subject_prefix: str,
    window_from: Any,
    window_to: Any,
    candidates: list[Candidate],
) -> None:
    """Copia de ``DigestGrouperBase._close_window`` cuyo ``UPDATE`` de reclamo no exige
    ``digest_batch_id IS NULL`` (la mutación observable: la condición es la guarda de verdad).
    Sin ella, dos ejecuciones concurrentes reclaman las mismas entregas cada una para su propio
    lote y ambas terminan enviando el correo (duplicado)."""
    organization_id = transaction.context.organization_id
    notification_ids = sorted({c.notification_id for c in candidates})
    digest_batch_id = uuid7(self._clock)
    await transaction.execute(
        DIGEST_BATCH.insert().values(
            digest_batch_id=digest_batch_id,
            organization_id=organization_id,
            recipient_user_id=recipient_user_id,
            kind=kind,
            window_from=window_from,
            window_to=window_to,
            notification_ids=notification_ids,
            item_count=len(notification_ids),
            sent_at=None,
            status="queued",
        )
    )
    await asyncio.sleep(0)  # cede el control para que la otra ejecución concurrente reclame igual
    await transaction.execute(
        update(NOTIFICATION_DELIVERY)
        .where(
            NOTIFICATION_DELIVERY.c.organization_id == organization_id,
            NOTIFICATION_DELIVERY.c.notification_id.in_(notification_ids),
        )
        .values(digest_batch_id=digest_batch_id)
    )
    await self._send(
        transaction,
        digest_batch_id=digest_batch_id,
        recipient_user_id=recipient_user_id,
        subject_prefix=subject_prefix,
        candidates=candidates,
        failure_code=None,
    )


def _seed_recipient_with_pending(
    world: NotificationWorld, *, count: int = 2
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    organization_id, plant_id, zone_id = world.new_organization()
    recipient = world.add_member(
        organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
    ).user_id
    ids = [
        world.seed_delivery(
            organization_id, recipient, urgency="normal", plant_id=plant_id, zone_id=zone_id
        )
        for _ in range(count)
    ]
    return organization_id, recipient, ids


async def _two_concurrent_closes(
    world: NotificationWorld,
    task: DigestGrouperBase,
    organization_id: uuid.UUID,
    recipient: uuid.UUID,
    notification_ids: list[uuid.UUID],
    *,
    kind: str = EMAIL_GROUPED_KIND,
) -> None:
    window_from, window_to = _window(world)
    candidates = [
        Candidate(
            notification_id=nid,
            created_at=window_from,
            plant_id=None,
            title_es="t",
            body_es="b",
        )
        for nid in notification_ids
    ]

    async def close() -> None:
        context = world.event_context(world.event(organization_id, "finding_received", {}))
        async with world.database.transaction(context) as transaction:
            await task._close_window(
                transaction,
                recipient_user_id=recipient,
                kind=kind,
                subject_prefix="Resumen de notificaciones",
                window_from=window_from,
                window_to=window_to,
                candidates=candidates,
            )

    await asyncio.gather(close(), close())


@pytest.mark.parametrize("attempt", range(REPETITIONS))
@pytest.mark.parametrize("task_kind", ["email_grouping", "digest_weekly"])
def test_two_concurrent_closes_of_the_same_window_send_only_once(
    world: NotificationWorld, task_kind: str, attempt: int
) -> None:
    """Simula un arrendamiento vencido artificialmente: dos ejecuciones calculan la misma ventana
    a la vez, para las dos tareas que comparten ``DigestGrouperBase._close_window`` (PR-LAZ-48
    nombra ``email_grouping`` y ``digest_weekly``). Puede quedar más de una fila de ``DigestBatch``
    (la que pierde la carrera de ``notification_delivery`` no se borra, PAT-LAZ-RES-05: queda
    ``superseded``), pero solo una reclama las entregas y envía: un solo correo, nunca dos."""
    organization_id, recipient, ids = _seed_recipient_with_pending(world)
    task: DigestGrouperBase
    kind: str
    if task_kind == "email_grouping":
        task, kind = _task(world), EMAIL_GROUPED_KIND
    else:
        task, kind = _digest_task(world), WEEKLY_MANAGER_KIND
    world.run(_two_concurrent_closes(world, task, organization_id, recipient, ids, kind=kind))
    batches = _digest_batches(world, organization_id)
    assert len(batches) in (1, 2), batches
    assert sum(1 for b in batches if b["status"] == "sent") == 1
    assert sum(1 for b in batches if b["status"] == "superseded") == len(batches) - 1
    assert len(_email_messages(world, organization_id)) == 1


@pytest.mark.parametrize("attempt", range(REPETITIONS))
def test_without_the_claim_guard_the_email_is_sent_twice(
    world: NotificationWorld, attempt: int
) -> None:
    """Mutación: quita ``digest_batch_id IS NULL`` del ``UPDATE`` que reclama las entregas. Sin
    esa condición, las dos ejecuciones concurrentes de la misma ventana reclaman las mismas
    entregas cada una para su propio lote y las dos envían: el correo se duplica."""
    organization_id, recipient, ids = _seed_recipient_with_pending(world)
    task = _task(world)
    with patch.object(DigestGrouperBase, "_close_window", _close_window_without_the_claim_guard):
        world.run(_two_concurrent_closes(world, task, organization_id, recipient, ids))
    messages = _email_messages(world, organization_id)
    assert len(messages) > 1, messages
