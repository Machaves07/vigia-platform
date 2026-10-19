"""PR-LAZ-40: el mismo evento entregado dos veces produce una sola notificación y un solo
``EmailMessage`` por destinatario, también con caídas entre el efecto y la confirmación y con
entregas simultáneas (TASK-324; BR-LAZ-129; PAT-LAZ-RES-05).

Contra PostgreSQL 16 real como ``vigia_app``, con el motor real (``NotificationWorld``):

- **Propiedad con estado**: para jerarquías y secuencias generadas con repeticiones, entregas que
  se caen antes de confirmar (la transacción se revierte) y el reproceso final de toda la cola
  (``notification_event_sequences``), el conjunto final de avisos es el de una sola entrega por
  evento: un aviso por (evento, destinatario), una entrega ``in_app`` y a lo sumo una de correo por
  aviso, y un ``EmailMessage`` por aviso ``immediate``.
- **Caída entre el efecto y la confirmación**: los avisos del proveedor se confirman en su propia
  transacción y la del cliente falla después; el reintento crea los del cliente y no repite los del
  proveedor.
- **Concurrencia** (``asyncio.gather`` sobre dos conexiones): dos entregas del mismo evento a la
  vez; la segunda espera a la primera en ``notification_source_key`` y, cuando la primera
  confirma, choca con ``unique_violation`` (``ConcurrentDelivery``) y se revierte entera: un solo
  aviso y un solo correo por destinatario, y su reintento termina sin efecto. Se repite tres veces.
  ``test_without_the_source_key_the_concurrent_delivery_duplicates`` es la mutación: en una base
  aparte, sin la clave primaria de ``notification_source_key``, la misma prueba produce avisos y
  correos duplicados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from tests.properties.laz.strategies.notifications import (
    Placed,
    event_plant,
    hierarchies,
    notification_event_sequences,
    payload_for,
)
from tests.properties.laz.test_pr_laz_39_recipients_scope import _place
from vigia_platform.notifications.engine import ConcurrentDelivery, NotificationEngine
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.outbox.publish import OutboxEvent

pytestmark = pytest.mark.integration

EXAMPLES: Final = 30
WAIT_SECONDS: Final = 60.0
"""Tope para ver a la segunda entrega esperando o terminada (no decide el resultado)."""
REPETITIONS: Final = 3


class Crash(Exception):
    """El proceso cae después del efecto y antes de confirmar."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "pr_laz_40") as built:
        yield built


def _crash(world: NotificationWorld, engine: NotificationEngine, event: OutboxEvent) -> None:
    async def run() -> None:
        async with world.database.transaction(world.event_context(event)) as tx:
            await engine.process(event, tx)
            raise Crash

    with pytest.raises(Crash):
        world.run(run())


def _check_once(world: NotificationWorld, event: OutboxEvent) -> list[dict[str, Any]]:
    """Un aviso por destinatario, sus entregas y, si es immediate, un correo por aviso."""
    rows = world.notifications(event.event_id)
    keys = Counter((row["organization_id"], row["recipient_user_id"]) for row in rows)
    assert all(count == 1 for count in keys.values()), keys
    deliveries = Counter(
        (row["notification_id"], row["channel"]) for row in world.deliveries(event.event_id)
    )
    assert all(count == 1 for count in deliveries.values()), deliveries
    assert {notification for notification, channel in deliveries if channel == "in_app"} == {
        row["notification_id"] for row in rows
    }
    emails = world.emails(event.event_id)
    related = Counter(email["related_ids"][0] for email in emails)
    assert all(count == 1 for count in related.values()), related
    immediate = {row["notification_id"] for row in rows if row["urgency"] == "immediate"}
    assert set(related) <= immediate
    return rows


def test_pr_laz_40_repetitions_and_crashes_leave_one_notification_per_recipient(
    world: NotificationWorld,
) -> None:
    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(st.data())
    def run(data: st.DataObject) -> None:
        hierarchy = data.draw(hierarchies(), label="jerarquía")
        drafts, deliveries = data.draw(notification_event_sequences(hierarchy), label="entregas")
        placed: Placed = _place(world, hierarchy)
        engine = world.engine()
        events = [
            world.event(
                placed.organization_id,
                draft.event_name,
                payload_for(
                    draft,
                    placed,
                    {"node": str(uuid.uuid4()), "record": str(uuid7()), "subject": str(uuid7())},
                ),
                plant_id=event_plant(draft, placed),
            )
            for draft in drafts
        ]
        # La referencia es la primera entrega confirmada de cada evento.
        first: dict[int, set[tuple[uuid.UUID, uuid.UUID]]] = {}
        for delivery in deliveries:
            event = events[delivery.index]
            if delivery.outcome == "crash":
                _crash(world, engine, event)
                if delivery.index not in first:
                    # Lo del cliente se revirtió con la transacción del despachador; lo de la
                    # proveedora, en su propia transacción, puede haberse confirmado ya.
                    assert {
                        row["organization_id"] for row in world.notifications(event.event_id)
                    } <= {world.provider_id}
                _check_once(world, event)
                continue
            world.deliver(engine, event)
            rows = _check_once(world, event)
            seen = {(row["organization_id"], row["recipient_user_id"]) for row in rows}
            # La primera entrega confirmada fija el conjunto; las siguientes no lo cambian.
            assert first.setdefault(delivery.index, seen) == seen
        for index, event in enumerate(events):
            assert {
                (row["organization_id"], row["recipient_user_id"])
                for row in _check_once(world, event)
            } == first[index]

    run()


# --- caída entre el efecto y la confirmación ---


class _FailOnClientInsert:
    """La transacción del despachador, que se cae al primer aviso del cliente."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def context(self) -> Any:
        return self._inner.context

    async def execute(self, statement: Any, parameters: Any = None) -> Any:
        if getattr(statement, "table", None) is not None and statement.table.name == "notification":
            raise Crash
        return await self._inner.execute(statement, parameters)


def test_the_provider_side_is_not_repeated_after_a_crash_on_the_client_side(
    world: NotificationWorld,
) -> None:
    organization, _, _ = world.new_organization()
    administrator = world.add_member(
        organization, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization
    )
    event = world.event(
        organization,
        "integrity_compromised",
        {"chain_kind": "ledger", "first_failed_sequence": 9, "verification_mode": "full"},
    )
    engine = world.engine()

    async def crashing() -> None:
        async with world.database.transaction(world.event_context(event)) as tx:
            await engine.process(event, _FailOnClientInsert(tx))  # type: ignore[arg-type]

    with pytest.raises(Crash):
        world.run(crashing())
    rows = world.notifications(event.event_id)
    provider = {r["recipient_user_id"] for r in rows if r["organization_id"] == world.provider_id}
    assert provider and {r["organization_id"] for r in rows} == {world.provider_id}
    provider_emails = len(world.emails(event.event_id))
    assert world.deliver(engine, event) == 1  # solo el del cliente
    rows = _check_once(world, event)
    assert {r["recipient_user_id"] for r in rows if r["organization_id"] == organization} == {
        administrator.user_id
    }
    assert {r["recipient_user_id"] for r in rows if r["organization_id"] == world.provider_id} == (
        provider
    )
    assert len(world.emails(event.event_id)) == provider_emails + 1
    assert world.deliver(engine, event) == 0


# --- concurrencia ---


async def _blocked(world: NotificationWorld) -> bool:
    connection = await world.superuser()
    try:
        waiting = await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = $1"
            " AND cardinality(pg_blocking_pids(pid)) > 0",
            world.migrated.database,
        )
    finally:
        await connection.close()
    return bool(waiting)


async def _two_at_once(
    world: NotificationWorld, engine: NotificationEngine, event: OutboxEvent
) -> list[Any]:
    """La primera entrega escribe y espera sin confirmar; la segunda arranca, choca con ella en la
    clave (o termina, sin la clave) y solo entonces la primera confirma."""
    written, release = asyncio.Event(), asyncio.Event()

    async def first() -> int:
        async with world.database.transaction(world.event_context(event)) as tx:
            created = await engine.process(event, tx)
            written.set()
            await release.wait()
            return created

    async def second() -> int:
        await written.wait()
        async with world.database.transaction(world.event_context(event)) as tx:
            return await engine.process(event, tx)

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await written.wait()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while not tasks[1].done() and not await _blocked(world):
        assert loop.time() < deadline, "la segunda entrega no llegó a la clave"
        await asyncio.sleep(0.05)
    release.set()
    return list(await asyncio.gather(*tasks, return_exceptions=True))


def _concurrent_case(world: NotificationWorld) -> tuple[OutboxEvent, set[uuid.UUID], list[Any]]:
    organization, plant, zone = world.new_organization()
    recipients = {
        world.add_member(organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone).user_id
        for _ in range(3)
    }
    event = world.event(
        organization,
        "review_overdue",
        # "phase": "first" (U04-H-07, VIG-213): el único nivel immediate+correo del catálogo.
        {
            "zone_id": str(zone),
            "detection_id": str(uuid7()),
            "reminders_sent": 1,
            "phase": "first",
        },
        plant_id=plant,
    )
    engine = world.engine()
    results = world.run(_two_at_once(world, engine, event))
    return event, recipients, results


@pytest.mark.parametrize("attempt", range(REPETITIONS))
def test_two_simultaneous_deliveries_leave_one_notification_and_one_email(
    world: NotificationWorld, attempt: int
) -> None:
    event, recipients, results = _concurrent_case(world)
    assert results[0] == len(recipients)
    assert isinstance(results[1], ConcurrentDelivery), results
    rows = _check_once(world, event)
    assert {row["recipient_user_id"] for row in rows} == recipients
    assert len(world.emails(event.event_id)) == len(recipients)
    # El reintento del perdedor termina sin efecto.
    assert world.deliver(world.engine(), event) == 0
    assert len(world.notifications(event.event_id)) == len(recipients)


@pytest.fixture(scope="module")
def keyless_world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    """Una base aparte sin la clave de idempotencia de los avisos (la mutación)."""
    with notification_world(postgres_endpoint, "pr_laz_40_keyless") as built:
        built.fetch(
            "ALTER TABLE notifications.notification_source_key"
            " DROP CONSTRAINT notification_source_key_pkey"
        )
        yield built


@pytest.mark.parametrize("attempt", range(REPETITIONS))
def test_without_the_source_key_the_concurrent_delivery_duplicates(
    keyless_world: NotificationWorld, attempt: int
) -> None:
    event, recipients, results = _concurrent_case(keyless_world)
    assert results == [len(recipients), len(recipients)]
    rows = keyless_world.notifications(event.event_id)
    assert len(rows) == 2 * len(recipients)
    assert len(keyless_world.emails(event.event_id)) == 2 * len(recipients)
