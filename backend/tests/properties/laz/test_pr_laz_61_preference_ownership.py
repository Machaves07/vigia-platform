"""PR-LAZ-61: dentro de una misma organización, ninguna operación de notificaciones ni de
preferencias devuelve o modifica una fila cuyo ``recipient_user_id`` o ``user_id`` no sea el del
actor (TASK-348; PAT-LAZ-SEG-03).

Contra PostgreSQL 16 real (``notification_world``, reutilizado de TASK-324), con el contexto real
de sesión de cada miembro (``NotificationWorld.member_context``): ``POST
/notifications/{notification_id}/read`` sobre un aviso de otro usuario (o inexistente) responde
igual (``not_found``) y no deja efecto; ``PUT``/``GET /notifications/preferences`` nunca leen ni
escriben la fila de otro usuario, porque la operación solo recibe ``context.actor.id``, nunca un
``user_id`` que llegue de otra parte. ``NotificationPreferenceNotOwned`` (la comprobación
defensiva, ``ensure_owned``) se prueba directamente, porque ninguna ruta HTTP tiene hoy un vector
para nombrar a otro usuario.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import Mock

import pytest

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import Member, NotificationWorld, notification_world
from vigia_platform.identity.authz.authorize import Authorizer, ResourceNotFound
from vigia_platform.notifications.preferences import (
    NotificationPreferenceNotOwned,
    NotificationPreferences,
    NotificationPreferenceUpdate,
    ensure_owned,
)
from vigia_platform.notifications.reader import NotificationReader
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "pr_laz_61") as built:
        yield built


def _authorizer(world: NotificationWorld) -> Authorizer:
    """Ningún caso de esta prueba entra bajo concesión: ``deny`` nunca se llama, así que la
    auditoría es un doble inerte."""
    return Authorizer(audit=Mock(), provider_organization_id=world.provider_id)


def _reader(world: NotificationWorld) -> NotificationReader:
    return NotificationReader(
        database=world.database,
        authorizer=_authorizer(world),
        clock=world.clock,
        provider_organization_id=world.provider_id,
    )


def _preferences(world: NotificationWorld) -> NotificationPreferences:
    return NotificationPreferences(
        database=world.database,
        clock=world.clock,
        authorizer=_authorizer(world),
        provider_organization_id=world.provider_id,
    )


def _coordinator(
    world: NotificationWorld, organization_id: uuid.UUID, zone_id: uuid.UUID
) -> Member:
    return world.add_member(organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id)


def test_ensure_owned_rejects_any_user_other_than_the_actors() -> None:
    other = uuid7()
    context_actor = uuid7()

    class _Fake:
        actor = type("A", (), {"id": context_actor})()

    with pytest.raises(NotificationPreferenceNotOwned):
        ensure_owned(_Fake(), other)  # type: ignore[arg-type]
    ensure_owned(_Fake(), context_actor)  # type: ignore[arg-type]  # no levanta nada


def test_mark_read_on_another_users_notification_responds_not_found_without_effect(
    world: NotificationWorld,
) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    owner = _coordinator(world, organization_id, zone_id)
    intruder = _coordinator(world, organization_id, zone_id)
    notification_id = world.seed_delivery(
        organization_id, owner.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    reader = _reader(world)
    with pytest.raises(ResourceNotFound):
        world.run(reader.mark_read(world.member_context(intruder), notification_id))
    rows = world.fetch(
        "SELECT read_at FROM notifications.notification WHERE notification_id = $1", notification_id
    )
    assert rows[0]["read_at"] is None


def test_mark_read_on_a_nonexistent_notification_responds_the_same_way(
    world: NotificationWorld,
) -> None:
    organization_id, _plant_id, zone_id = world.new_organization()
    member = _coordinator(world, organization_id, zone_id)
    reader = _reader(world)
    with pytest.raises(ResourceNotFound):
        world.run(reader.mark_read(world.member_context(member), uuid7()))


def test_mark_read_on_the_owners_own_notification_succeeds(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    owner = _coordinator(world, organization_id, zone_id)
    notification_id = world.seed_delivery(
        organization_id, owner.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    reader = _reader(world)
    world.run(reader.mark_read(world.member_context(owner), notification_id))
    rows = world.fetch(
        "SELECT read_at FROM notifications.notification WHERE notification_id = $1", notification_id
    )
    assert rows[0]["read_at"] is not None


def test_preferences_update_never_touches_another_users_row(world: NotificationWorld) -> None:
    organization_id, _plant_id, zone_id = world.new_organization()
    actor = _coordinator(world, organization_id, zone_id)
    other = _coordinator(world, organization_id, zone_id)
    preferences = _preferences(world)
    # El otro usuario ya tiene su fila (valores por defecto distintos de lo que el actor fija).
    world.run(preferences.read_own(world.member_context(other)))
    updated = world.run(
        preferences.update_own(
            world.member_context(actor),
            NotificationPreferenceUpdate(
                email_enabled_normal=False, email_enabled_digest=False, weekly_digest_weekday=5
            ),
        )
    )
    assert updated.weekly_digest_weekday == 5
    other_row = world.fetch(
        "SELECT * FROM notifications.user_notification_preference WHERE user_id = $1",
        other.user_id,
    )
    assert other_row[0]["weekly_digest_weekday"] != 5
    assert other_row[0]["email_enabled_normal"] is True


def test_list_own_never_returns_another_users_notification(world: NotificationWorld) -> None:
    organization_id, plant_id, zone_id = world.new_organization()
    actor = _coordinator(world, organization_id, zone_id)
    other = _coordinator(world, organization_id, zone_id)
    world.seed_delivery(
        organization_id, other.user_id, urgency="normal", plant_id=plant_id, zone_id=zone_id
    )
    reader = _reader(world)
    page = world.run(reader.list_own(world.member_context(actor)))
    assert page.items == ()
