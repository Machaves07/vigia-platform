"""PR-LAZ-39: ninguna notificación llega a un usuario cuyo alcance no contenga la zona o planta del
evento, ni a otra organización; las del proveedor no llegan al cliente salvo
``integrity_compromised`` (TASK-324; BR-LAZ-124 y 127; A-58 y A-69).

Contra PostgreSQL 16 real como ``vigia_app``, con el motor real (``NotificationWorld``): por cada
ejemplo, una organización nueva con la jerarquía y las asignaciones de rol generadas
(``hierarchies``: plantas, zonas, roles en los tres niveles, cuentas desactivadas y asignaciones
retiradas) y una secuencia de eventos del catálogo (``notification_event_sequences``: de zona, de
planta, de organización, de concesión, con responsable y solo del proveedor; parte de ellos
entregados **bajo concesión** de la planta 0, que resuelve con la consulta acotada).

La comprobación es **exacta** contra el oráculo (``expected_recipients``, escrito sin el código del
motor ni de la identidad): los destinatarios del cliente son exactamente los esperados, y por eso
la prueba cae si se **quita el filtro de planta o el de zona** de la resolución (más destinatarios
de los debidos) o si se estrecha (menos). Los del proveedor: solo en la proveedora, solo con los
roles de la fila y solo en las filas de audiencia ``provider`` o ``both``; ningún aviso del
proveedor en el cliente salvo los de ``integrity_compromised`` (y la copia de ``adoption_low``,
cuya audiencia es ``both``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from tests.properties.laz.strategies.notifications import (
    Hierarchy,
    Placed,
    event_plant,
    expected_recipients,
    hierarchies,
    notification_event_sequences,
    payload_for,
)
from vigia_platform.loop.domain.enums import NotificationAudience
from vigia_platform.notifications.catalog import CATALOG
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

EXAMPLES: Final = 30
"""Cada ejemplo siembra una organización y entrega hasta cinco eventos contra la base real."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "pr_laz_39") as built:
        yield built


def _place(world: NotificationWorld, hierarchy: Hierarchy) -> Placed:
    organization, first_plant, first_zone = world.new_organization()
    plants: list[uuid.UUID] = []
    zones: list[tuple[uuid.UUID, ...]] = []
    for index, count in enumerate(hierarchy.zones_per_plant):
        if index == 0:
            plant, zone = first_plant, first_zone
        else:
            plant, zone = world.add_plant(organization)
        plants.append(plant)
        zones.append((zone, *(world.add_zone(organization, plant) for _ in range(count - 1))))
    placed = Placed(organization, tuple(plants), tuple(zones), ())
    users = []
    for assignment in hierarchy.assignments:
        scope = {
            ScopeLevel.ORGANIZATION: organization,
            ScopeLevel.PLANT: placed.plant(assignment.place),
            ScopeLevel.ZONE: placed.zone(assignment.place),
        }[assignment.level]
        assert scope is not None
        member = world.add_member(
            organization,
            assignment.role,
            assignment.level,
            scope,
            status="active" if assignment.active else "deactivated",
            removed=assignment.removed,
        )
        users.append(member.user_id)
    return Placed(organization, placed.plants, placed.zones, tuple(users))


def _provider_staff(world: NotificationWorld) -> dict[Role, set[uuid.UUID]]:
    rows = world.fetch(
        "SELECT r.role, r.user_id FROM identity.role_assignment AS r"
        " JOIN identity.user_account AS u ON u.user_id = r.user_id"
        " WHERE r.organization_id = $1 AND r.removed_at IS NULL AND u.status = 'active'",
        world.provider_id,
    )
    staff: dict[Role, set[uuid.UUID]] = {}
    for row in rows:
        staff.setdefault(Role(row["role"]), set()).add(row["user_id"])
    return staff


def test_pr_laz_39_notifications_reach_exactly_the_users_whose_scope_contains_the_event(
    world: NotificationWorld,
) -> None:
    engine = world.engine()
    staff = _provider_staff(world)

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(st.data())
    def run(data: st.DataObject) -> None:
        hierarchy = data.draw(hierarchies(), label="jerarquía")
        events, _ = data.draw(notification_event_sequences(hierarchy), label="eventos")
        placed = _place(world, hierarchy)
        concession = None
        for draft in events:
            ids = {"node": str(uuid.uuid4()), "record": str(uuid7()), "subject": str(uuid7())}
            event = world.event(
                placed.organization_id,
                draft.event_name,
                payload_for(draft, placed, ids),
                plant_id=event_plant(draft, placed),
            )
            context = None
            if draft.under_concession:
                if concession is None:
                    concession = world.plant_concession(placed.organization_id, placed.plants[0])
                context = concession
            world.deliver(engine, event, context=context)
            rows = world.notifications(event.event_id)
            entry = CATALOG[draft.event_name]
            client = {
                r["recipient_user_id"]
                for r in rows
                if r["organization_id"] == placed.organization_id
            }
            provider = {
                r["recipient_user_id"] for r in rows if r["organization_id"] == world.provider_id
            }
            assert {r["organization_id"] for r in rows} <= {
                placed.organization_id,
                world.provider_id,
            }, draft
            expected = {placed.users[index] for index in expected_recipients(hierarchy, draft)}
            assert client == expected, draft
            if entry.audience is NotificationAudience.CLIENT:
                assert provider == set(), draft
            else:
                wanted = set().union(
                    *(staff.get(role, set()) for role in entry.recipients.provider)
                )
                assert provider == wanted, draft
            if entry.audience is NotificationAudience.PROVIDER:
                assert client == set(), draft
            # Un aviso del cliente lleva la planta (y la zona) del evento; uno del proveedor, nada.
            for row in rows:
                if row["organization_id"] == world.provider_id:
                    assert (row["plant_id"], row["zone_id"]) == (None, None)

    run()


@pytest.mark.parametrize("under_concession", [False, True], ids=["evento", "concesion"])
@pytest.mark.parametrize("level", [ScopeLevel.ZONE, ScopeLevel.PLANT], ids=["zona", "planta"])
def test_pr_laz_39_users_of_another_zone_or_plant_are_never_reached(
    world: NotificationWorld, under_concession: bool, level: ScopeLevel
) -> None:
    """El borde que la propiedad puede no generar en cada corrida: con la consulta abierta y con la
    acotada (A-69), un coordinador de **otra** zona de la misma planta y uno de **otra** planta no
    lo reciben; los de la zona, la planta y la organización del evento, sí."""
    organization, plant, zone = world.new_organization()
    sibling_zone = world.add_zone(organization, plant)
    other_plant, other_plant_zone = world.add_plant(organization)
    C = Role.COORDINATOR_SST
    wanted = {
        world.add_member(organization, C, ScopeLevel.ZONE, zone).user_id,
        world.add_member(organization, C, ScopeLevel.PLANT, plant).user_id,
        world.add_member(organization, C, ScopeLevel.ORGANIZATION, organization).user_id,
    }
    world.add_member(organization, C, ScopeLevel.ZONE, sibling_zone)
    world.add_member(organization, C, ScopeLevel.PLANT, other_plant)
    world.add_member(organization, C, ScopeLevel.ZONE, other_plant_zone)
    if level is ScopeLevel.ZONE:
        name, payload = "finding_received", {"zone_id": str(zone), "finding_id": str(uuid7())}
    else:
        # La fila de nodo es del administrador, con alcance sobre la planta.
        name, payload = (
            "node_revoked",
            {"node_id": str(uuid.uuid4()), "plant_id": str(plant), "zone_ids": [str(zone)]},
        )
        administrators = [
            world.add_member(organization, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant),
            world.add_member(
                organization, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization
            ),
        ]
        wanted = {member.user_id for member in administrators}
        world.add_member(organization, Role.ADMINISTRATOR, ScopeLevel.PLANT, other_plant)
        world.add_member(organization, Role.ADMINISTRATOR, ScopeLevel.ZONE, zone)
    event = world.event(organization, name, payload, plant_id=plant)
    context = world.plant_concession(organization, plant) if under_concession else None
    world.deliver(world.engine(), event, context=context)
    assert {row["recipient_user_id"] for row in world.notifications(event.event_id)} == wanted
