"""PR-GOB-30 (TASK-224; LC-GOB-23b; PAT-GOB-REN-04) sobre ``FleetQueryPort``, contra PostgreSQL 16.

Oráculo (perfil ``ci``, semilla fija y semilla de la sesión registradas por ``tests/conftest.py``):
para una historia generada de asignaciones de una zona (``identity.zone_node_assignment`` de U-02,
con huecos, asignaciones contiguas, nodos que se repiten, reemplazos y la última quizá vigente) y
un rango generado de hasta 366 días:

- ``assignment_history`` devuelve **exactamente** las asignaciones del modelo que se solapan con
  ``[from, to]``, en orden de ``assigned_at``, con su ``replaces_node_id``;
- es lo mismo, en el mismo orden, que repetir ``assignment_at`` en los bordes de cada asignación
  (``assigned_at`` y el último microsegundo antes de ``unassigned_at``);
- ``assignment_at`` en un instante sin nodo devuelve el hueco acotado por la retirada anterior y la
  asignación siguiente;
- un rango de 367 días (o de 366 días y un microsegundo) es ``FleetRangeTooLong``, nunca un
  resultado truncado; uno de 366 días exactos devuelve todo.

Cada ejemplo usa una zona nueva (las asignaciones son de solo anexar). Solo datos generados
(NFR-CTR-43); las marcas parten de un instante fijo derivado del reloj simulado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fleet_http_support import fleet_stack
from tests.fleet_inventory_support import InventoryWorld
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.ports import FleetRangeTooLong, ZoneAssignmentPeriod
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing.keys import to_millisecond

pytestmark = pytest.mark.integration

US: Final = timedelta(microseconds=1)
DAY: Final = timedelta(days=1)
LIMIT: Final = timedelta(days=366)
NODES: Final = 5


@dataclass(frozen=True)
class Interval:
    node: int
    start: timedelta
    end: timedelta | None


@st.composite
def histories(draw: st.DrawFn) -> list[Interval]:
    """Intervalos sin solape (la exclusión GiST de A-12/A-32), contiguos o con hueco."""
    count = draw(st.integers(0, 6))
    cursor = timedelta(milliseconds=draw(st.integers(0, 30 * 86_400_000)))
    intervals: list[Interval] = []
    for index in range(count):
        length = timedelta(
            milliseconds=draw(st.one_of(st.integers(1, 1_000), st.integers(1, 200 * 86_400_000)))
        )
        last = index == count - 1
        end = None if last and draw(st.booleans()) else cursor + length
        intervals.append(Interval(draw(st.integers(0, NODES - 1)), cursor, end))
        if end is None:
            break
        gap = draw(st.sampled_from((0, 0, 1, 3_600_000, 90 * 86_400_000)))
        cursor = end + timedelta(milliseconds=gap)
    return intervals


@dataclass
class World:
    inventory: InventoryWorld
    service: FleetInventory
    context: ScopeContext
    plant: uuid.UUID
    nodes: list[uuid.UUID]
    replaces: dict[uuid.UUID, uuid.UUID | None]
    origin: datetime


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with fleet_stack(postgres_endpoint, "pr_gob_30") as stack:
        clock = stack.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        inventory = InventoryWorld.build(stack)
        plant = inventory.plants[0]
        nodes = [inventory.add_node(plant) for _ in range(NODES)]
        # Dos reemplazos: el nodo 1 reemplaza al 0 y el 3 al 2 (replaces_node_id de la ficha).
        replaces: dict[uuid.UUID, uuid.UUID | None] = dict.fromkeys(nodes)
        for new, old in ((1, 0), (3, 2)):
            stack.execute(
                "UPDATE fleet.node_fleet_record SET replaces_node_id = $2 WHERE node_id = $1",
                nodes[new],
                nodes[old],
            )
            replaces[nodes[new]] = nodes[old]
        assert stack.services.inventory is not None
        yield World(
            inventory=inventory,
            service=stack.services.inventory,
            context=stack.context(inventory.admin),
            plant=plant,
            nodes=nodes,
            replaces=replaces,
            origin=inventory.now() - timedelta(days=800),
        )


def _zone_with(world: World, intervals: list[Interval]) -> uuid.UUID:
    inventory = world.inventory
    zone = uuid.uuid4()
    inventory.stack.authz.add_zone(inventory.organization, world.plant, zone)
    for interval in intervals:
        inventory.stack.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            uuid.uuid4(),
            inventory.organization,
            world.plant,
            zone,
            world.nodes[interval.node],
            world.origin + interval.start,
            None if interval.end is None else world.origin + interval.end,
            inventory.stack.authz.operator_id,
        )
    return zone


def _period(world: World, interval: Interval) -> ZoneAssignmentPeriod:
    node = world.nodes[interval.node]
    return ZoneAssignmentPeriod(
        node_id=node,
        assigned_at=world.origin + interval.start,
        unassigned_at=None if interval.end is None else world.origin + interval.end,
        replaces_node_id=world.replaces[node],
    )


def _overlaps(world: World, interval: Interval, start: datetime, end: datetime) -> bool:
    assigned = world.origin + interval.start
    unassigned = None if interval.end is None else world.origin + interval.end
    return assigned <= end and (unassigned is None or unassigned > start)


def _borders(intervals: list[Interval]) -> list[int]:
    """Los bordes de las asignaciones en milisegundos desde el origen (y sus vecinos)."""
    edges = [i.start for i in intervals] + [i.end for i in intervals if i.end is not None]
    ms = [edge // timedelta(milliseconds=1) for edge in edges]
    return sorted({m + d for m in ms for d in (-1, 0, 1) if m + d >= 0})


def _instant(data: st.DataObject, intervals: list[Interval], label: str) -> int:
    """Un instante cualquiera o, a veces, un borde exacto de una asignación (o su vecino)."""
    borders = _borders(intervals)
    if borders and data.draw(st.booleans(), label=f"{label} en un borde"):
        return data.draw(st.sampled_from(borders), label=label)
    return data.draw(st.integers(0, 500 * 86_400_000), label=label)


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(intervals=histories(), data=st.data())
def test_pr_gob_30_history_equals_assignment_at_on_the_borders(
    world: World, intervals: list[Interval], data: st.DataObject
) -> None:
    run = world.inventory.stack.run
    zone = _zone_with(world, intervals)
    offset = _instant(data, intervals, "from")
    limit_ms = LIMIT // timedelta(milliseconds=1)
    length = data.draw(
        st.one_of(
            st.just(limit_ms),
            st.just(0),
            st.integers(0, limit_ms),
            st.sampled_from(
                [b - offset for b in _borders(intervals) if 0 <= b - offset <= limit_ms]
            )
            if any(0 <= b - offset <= limit_ms for b in _borders(intervals))
            else st.just(limit_ms),
        ),
        label="to - from",
    )
    probe = _instant(data, intervals, "instante")
    start = world.origin + timedelta(milliseconds=offset)
    end = start + timedelta(milliseconds=length)
    history = list(run(world.service.assignment_history(world.context, zone, start, end)))
    expected = [_period(world, i) for i in intervals if _overlaps(world, i, start, end)]
    assert history == expected
    # Lo mismo, en el mismo orden, que assignment_at en los bordes de cada asignación.
    at_borders: list[ZoneAssignmentPeriod] = []
    for period in history:
        found = run(world.service.assignment_at(world.context, zone, period.assigned_at))
        assert found == period
        if period.unassigned_at is not None:
            assert (
                run(world.service.assignment_at(world.context, zone, period.unassigned_at - US))
                == period
            )
        at_borders.append(found)
    assert at_borders == history
    # Un instante cualquiera: la asignación del modelo o el hueco entre la anterior y la siguiente.
    at = world.origin + timedelta(milliseconds=probe)
    found = run(world.service.assignment_at(world.context, zone, at))
    covering = [
        i
        for i in intervals
        if world.origin + i.start <= at and (i.end is None or world.origin + i.end > at)
    ]
    if covering:
        assert found == _period(world, covering[0])
    else:
        before = [
            world.origin + i.end
            for i in intervals
            if i.end is not None and world.origin + i.end <= at
        ]
        after = [world.origin + i.start for i in intervals if world.origin + i.start > at]
        assert found.node_id is None and found.replaces_node_id is None
        assert found.unassigned_at == (min(after) if after else None)
        if before:
            assert found.assigned_at == max(before)
        else:
            assert found.assigned_at <= at
    # El tope: 366 días y un microsegundo (o 367 días) es un error, nunca un resultado truncado.
    for too_long in (LIMIT + US, LIMIT + DAY):
        with pytest.raises(FleetRangeTooLong) as raised:
            run(world.service.assignment_history(world.context, zone, start, start + too_long))
        assert raised.value.detail_code is FleetDetailCode.ASSIGNMENT_RANGE_TOO_LONG
