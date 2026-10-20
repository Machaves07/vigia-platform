"""``GET/POST /notifications`` y ``GET/PUT /notifications/preferences`` contra PostgreSQL 16 real
(TASK-348; SUP-U05-33 a 35; NFR-LAZ-09).

Las rutas de ``notifications/adapters/http/routes.py`` son envoltorios finos de
``NotificationReader`` y ``NotificationPreferences`` (decodifican el cursor, leen el cuerpo y
llaman al servicio); esta prueba ejerce esos servicios directamente contra la base real
(``notification_world``, reutilizado de TASK-324), que es donde viven las garantías de paginación,
filtro y archivado. El aislamiento de las tres rutas (PR-LAZ-61, cruzado entre actores) se prueba
en ``tests/isolation/test_route_isolation.py`` (los casos de ``laz_cases.py``) y en
``tests/properties/laz/test_pr_laz_61_preference_ownership.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final
from unittest.mock import Mock

import pytest

from tests.factories import uuid7 as fresh_uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.notifications.preferences import (
    NotificationPreferences,
    NotificationPreferenceUpdate,
)
from vigia_platform.notifications.reader import (
    MAX_PAGE,
    NotificationReader,
    NotificationStatusFilter,
    archive_read_notifications,
)
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

PAGINATION_ROWS: Final = 10_000
"""El criterio de aceptación (SUP-U05-33): sin pérdidas ni repeticiones sobre 10 000 filas (la
volumetría de 1 000 000 es de VIG-246)."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "notification_routes") as built:
        yield built


def _reader(world: NotificationWorld) -> NotificationReader:
    return NotificationReader(
        database=world.database,
        authorizer=Authorizer(audit=Mock(), provider_organization_id=world.provider_id),
        clock=world.clock,
        provider_organization_id=world.provider_id,
    )


def _preferences(world: NotificationWorld) -> NotificationPreferences:
    return NotificationPreferences(
        database=world.database,
        clock=world.clock,
        authorizer=Authorizer(audit=Mock(), provider_organization_id=world.provider_id),
        provider_organization_id=world.provider_id,
    )


def _member(world: NotificationWorld, organization_id: uuid.UUID, zone_id: uuid.UUID) -> Any:
    return world.add_member(organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id)


def _bulk_seed(
    world: NotificationWorld,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    recipient_user_id: uuid.UUID,
    count: int,
) -> list[uuid.UUID]:
    """Inserta ``count`` avisos directamente, por SQL en bloque (rápido): no por eso deja de ser
    la misma forma que ``seed_delivery``, solo que de una vez."""
    base = world.clock.now()
    ids = [fresh_uuid7() for _ in range(count)]
    rows = [
        (
            notification_id,
            organization_id,
            plant_id,
            zone_id,
            "finding_received",
            fresh_uuid7(),
            "normal",
            "Aviso sintético",
            "Cuerpo sintético",
            json.dumps({"kind": "zone", "id": str(zone_id)}),
            recipient_user_id,
            base + timedelta(milliseconds=index),
        )
        for index, notification_id in enumerate(ids)
    ]

    async def run() -> None:
        connection = await world.superuser()
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true)", str(organization_id)
                )
                await connection.execute("SELECT set_config('vigia.actor_kind', 'system', true)")
                await connection.executemany(
                    "INSERT INTO notifications.notification (notification_id, organization_id,"
                    " plant_id, zone_id, event_name, source_event_id, urgency, title_es, body_es,"
                    " deep_link_ref, recipient_user_id, created_at) VALUES"
                    " ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                    rows,
                )
        finally:
            await connection.close()

    world.run(run())
    return ids


def test_pagination_by_key_has_no_loss_or_repeats_over_many_rows(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    member = _member(world, organization_id, zone_id)
    ids = set(
        _bulk_seed(world, organization_id, plant_id, zone_id, member.user_id, PAGINATION_ROWS)
    )
    reader = _reader(world)
    context = world.member_context(member)
    seen: set[uuid.UUID] = set()
    cursor = None
    pages = 0
    while True:
        page = world.run(reader.list_own(context, after=cursor, limit=MAX_PAGE))
        for item in page.items:
            assert item.notification_id not in seen, "repetido"
            seen.add(item.notification_id)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages <= (PAGINATION_ROWS // MAX_PAGE) + 2, "la paginación no converge"
    assert seen == ids


def test_status_filter_separates_read_from_unread(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    member = _member(world, organization_id, zone_id)
    reader = _reader(world)
    context = world.member_context(member)
    unread_id = world.seed_delivery(
        organization_id, member.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    read_id = world.seed_delivery(
        organization_id, member.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    world.run(reader.mark_read(context, read_id))

    unread_page = world.run(reader.list_own(context, status=NotificationStatusFilter.UNREAD))
    assert {item.notification_id for item in unread_page.items} == {unread_id}

    read_page = world.run(reader.list_own(context, status=NotificationStatusFilter.READ))
    assert {item.notification_id for item in read_page.items} == {read_id}


def test_period_filter_bounds_by_created_at(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    member = _member(world, organization_id, zone_id)
    reader = _reader(world)
    context = world.member_context(member)
    old_id = world.seed_delivery(
        organization_id, member.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    world.clock.advance(int(timedelta(days=5).total_seconds()))
    cutoff = world.clock.now()
    world.clock.advance(int(timedelta(days=1).total_seconds()))
    new_id = world.seed_delivery(
        organization_id, member.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )

    before = world.run(reader.list_own(context, until=cutoff))
    assert {item.notification_id for item in before.items} == {old_id}

    after = world.run(reader.list_own(context, since=cutoff))
    assert {item.notification_id for item in after.items} == {new_id}


def test_read_notifications_are_archived_after_sixty_days_and_stay_queryable(
    world: NotificationWorld,
) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    member = _member(world, organization_id, zone_id)
    reader = _reader(world)
    context = world.member_context(member)
    notification_id = world.seed_delivery(
        organization_id, member.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    world.run(reader.mark_read(context, notification_id))
    world.clock.advance(int(timedelta(days=61).total_seconds()))

    async def archive() -> None:
        async with world.database.transaction(context) as transaction:
            await archive_read_notifications(transaction, world.clock)

    world.run(archive())

    default_page = world.run(reader.list_own(context))
    assert notification_id not in {item.notification_id for item in default_page.items}

    with_archived = world.run(reader.list_own(context, include_archived=True))
    found = next(item for item in with_archived.items if item.notification_id == notification_id)
    assert found.archived_at is not None
    # Nada se borra: sigue existiendo la fila.
    assert world.fetch(
        "SELECT 1 FROM notifications.notification WHERE notification_id = $1", notification_id
    )


def test_preferences_have_defaults_and_persist_updates(world: NotificationWorld) -> None:
    organization_id, _plant_id, zone_id = world.new_organization()
    member = _member(world, organization_id, zone_id)
    preferences = _preferences(world)
    context = world.member_context(member)

    defaults = world.run(preferences.read_own(context))
    assert defaults.email_enabled_normal is True
    assert defaults.email_enabled_digest is True
    assert 1 <= defaults.weekly_digest_weekday <= 7

    updated = world.run(
        preferences.update_own(
            context,
            NotificationPreferenceUpdate(
                email_enabled_normal=False, email_enabled_digest=True, weekly_digest_weekday=3
            ),
        )
    )
    assert updated.email_enabled_normal is False
    assert updated.weekly_digest_weekday == 3

    reread = world.run(preferences.read_own(context))
    assert reread.email_enabled_normal is False
    assert reread.weekly_digest_weekday == 3
