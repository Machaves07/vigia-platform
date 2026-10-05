"""``FleetQueryPort`` en proceso contra PostgreSQL 16 real (TASK-224; LC-GOB-23b; interfaces §1.3).

- Cada operación (``nodes_by_zone``, ``node``, ``assignment_at``, ``assignment_history``) es **una**
  sentencia contada, de solo lectura;
- ``nodes_by_zone`` con el estado de comunicación del expediente y las cámaras del último latido
  con su ``code``; una zona sin nodo, lista vacía; ``node`` igual que el de ``GET /fleet/nodes``;
- un reemplazo deja el hueco trazado: ``assignment_at`` en el instante del reemplazo es el nodo
  nuevo con ``replaces_node_id`` y justo antes el viejo;
- PR-GOB-12 sobre cada operación (NFR-GOB-30, H-47): otra organización igual que inexistente
  (``ResourceNotFound``); con un contexto de planta, las zonas y los nodos de la otra planta no
  existen; con uno de zona, solo esa zona y los nodos que la atienden; un contexto sin alcance (de
  sistema), nada;
- entradas inválidas: ``FleetQueryInvalid``; rango de más de 366 días: ``FleetRangeTooLong``.

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base, al milisegundo.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState, counting
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.ports import (
    FleetQueryInvalid,
    FleetQueryPort,
    FleetRangeTooLong,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ActorUnit, ScopeContext, ScopeLevel
from vigia_platform.shared.signing.keys import to_millisecond

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_query_port") as built:
        clock = built.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        yield built


@pytest.fixture
def world(stack: FleetStack) -> InventoryWorld:
    return InventoryWorld.build(stack)


def _port(world: InventoryWorld) -> FleetInventory:
    service = world.stack.services.inventory
    assert service is not None
    return service


def _context(world: InventoryWorld, level: ScopeLevel = ScopeLevel.ORGANIZATION,
             scope: uuid.UUID | None = None) -> ScopeContext:  # fmt: skip
    who = world.admin if level is ScopeLevel.ORGANIZATION else world.member(level, scope)
    return world.stack.context(who)


def _operations(
    port: FleetQueryPort, zone: uuid.UUID, node: uuid.UUID, at: datetime
) -> dict[str, Callable[[ScopeContext], Awaitable[Any]]]:
    """Las cuatro operaciones del puerto sobre la zona y el nodo dados."""
    return {
        "nodes_by_zone": lambda c: port.nodes_by_zone(c, zone),
        "node": lambda c: port.node(c, node),
        "assignment_at": lambda c: port.assignment_at(c, zone, at),
        "assignment_history": lambda c: port.assignment_history(c, zone, at - DAY, at + DAY),
    }


def test_the_port_reads_the_zone_its_nodes_and_their_cameras(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone, empty = world.zones(plant)[:2]
    node = world.declare(plant, [zone])
    now = world.next_now()
    world.write_state(node, NodeState(cameras=((20.0, 10.0), (3.0, 10.0))), now)
    cameras = world.fetch_cameras(node)
    world.add_catalog(zone, [(cameras[0], "CAM-A")])
    port, context = _port(world), _context(world)
    found = world.stack.run(port.nodes_by_zone(context, zone))
    assert [item.node_id for item in found] == [node]
    assert found[0].communication_state.value == "unknown"  # la declaración
    assert found[0].last_heartbeat_at == now - timedelta(seconds=1)
    assert {(c.camera_id, c.code) for c in found[0].cameras} == {
        (cameras[0], "CAM-A"),
        (cameras[1], None),
    }
    assert world.stack.run(port.nodes_by_zone(context, empty)) == ()
    inventory = world.stack.run(port.node(context, node))
    assert inventory.zones == (zone,)
    assert [w.value for w in inventory.warnings] == ["camera_below_min_fps"]
    listed = {n["node_id"]: n for n in world.list_all(world.admin)}
    assert [w.value for w in inventory.warnings] == listed[str(node)]["warnings"]


def test_each_operation_is_one_read_only_statement(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    node = world.declare(plant, [zone])
    port, context = _port(world), _context(world)
    for name, operation in _operations(port, zone, node, world.now()).items():
        with counting(world.stack) as log:
            world.stack.run(operation(context))
        statements = log.data_statements()
        assert len(statements) == 1, (name, statements)
        assert statements[0].lstrip().upper().startswith(("SELECT", "WITH")), name


def test_a_replacement_leaves_the_gap_traced(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    old = world.declare(plant, [zone])
    response = world.stack.declare(world.installer, plant, [], replaces_node_id=str(old))
    assert response.status_code == 201, response.text
    new = uuid.UUID(response.json()["node_id"])
    port, context = _port(world), _context(world)
    history = world.stack.run(
        port.assignment_history(context, zone, world.now() - DAY, world.now())
    )
    assert [p.node_id for p in history] == [old, new]
    assert history[0].unassigned_at == history[1].assigned_at
    assert history[1].replaces_node_id == old and history[0].replaces_node_id is None
    swap = history[1].assigned_at
    assert world.stack.run(port.assignment_at(context, zone, swap)) == history[1]
    before = world.stack.run(port.assignment_at(context, zone, swap - timedelta(microseconds=1)))
    assert before == history[0]
    # Antes de la primera asignación: un hueco sin nodo hasta ella.
    gap = world.stack.run(port.assignment_at(context, zone, history[0].assigned_at - DAY))
    assert gap.node_id is None and gap.unassigned_at == history[0].assigned_at


def test_another_organization_is_exactly_like_a_missing_resource(world: InventoryWorld) -> None:
    other = InventoryWorld.build(world.stack)
    their_zone = other.zones(other.plants[0])[0]
    their_node = other.declare(other.plants[0], [their_zone])
    port = _port(world)
    context = _context(world)
    for name, operation in _operations(port, their_zone, their_node, world.now()).items():
        with pytest.raises(ResourceNotFound):
            world.stack.run(operation(context))
        missing = _operations(port, uuid.uuid4(), uuid.uuid4(), world.now())[name]
        with pytest.raises(ResourceNotFound):
            world.stack.run(missing(context))
    # La otra organización sí lo ve (la prueba no pasa por una consulta vacía).
    theirs = _context(other)
    for operation in _operations(port, their_zone, their_node, world.now()).values():
        world.stack.run(operation(theirs))


def test_a_plant_context_never_sees_the_other_plant(world: InventoryWorld) -> None:
    first, second = world.plants
    zone, other_zone = world.zones(first)[0], world.zones(second)[0]
    mine = world.declare(first, [zone])
    theirs = world.declare(second, [other_zone])
    port = _port(world)
    context = _context(world, ScopeLevel.PLANT, first)
    for operation in _operations(port, other_zone, theirs, world.now()).values():
        with pytest.raises(ResourceNotFound):
            world.stack.run(operation(context))
    for operation in _operations(port, zone, mine, world.now()).values():
        world.stack.run(operation(context))


def test_a_zone_context_sees_only_its_zone_and_the_nodes_serving_it(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone, sibling, _ = world.zones(plant)
    serving = world.declare(plant, [zone])
    other = world.declare(plant, [sibling])
    port = _port(world)
    context = _context(world, ScopeLevel.ZONE, zone)
    for operation in _operations(port, sibling, other, world.now()).values():
        with pytest.raises(ResourceNotFound):
            world.stack.run(operation(context))
    for operation in _operations(port, zone, serving, world.now()).values():
        world.stack.run(operation(context))
    assert world.stack.run(port.node(context, serving)).zones == (zone,)


def test_a_context_without_scopes_sees_nothing(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    node = world.declare(plant, [zone])
    # El constructor real de una iteración periódica: sin asignaciones (BR-NUC-03).
    system = world.stack.authz.contexts.context_for_organization(_Task(), world.organization)
    assert system.allowed_scopes == ()
    for operation in _operations(_port(world), zone, node, world.now()).values():
        with pytest.raises(ResourceNotFound):
            world.stack.run(operation(system))


def test_invalid_inputs_and_the_range_limit(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone = world.zones(plant)[0]
    port, context = _port(world), _context(world)
    now = world.now()
    naive = now.replace(tzinfo=None)
    run = world.stack.run
    with pytest.raises(FleetQueryInvalid):
        run(port.assignment_at(context, zone, naive))
    with pytest.raises(FleetQueryInvalid):
        run(port.assignment_history(context, zone, now, now - timedelta(microseconds=1)))
    with pytest.raises(FleetQueryInvalid):
        run(port.nodes_by_zone(context, str(zone)))  # type: ignore[arg-type]
    with pytest.raises(FleetQueryInvalid):
        run(port.node(context, str(zone)))  # type: ignore[arg-type]
    assert run(port.assignment_history(context, zone, now, now)) == ()
    assert run(port.assignment_history(context, zone, now - timedelta(days=366), now)) == ()
    for too_long in (timedelta(days=366, microseconds=1), timedelta(days=367)):
        with pytest.raises(FleetRangeTooLong):
            run(port.assignment_history(context, zone, now - too_long, now))


@dataclass(frozen=True)
class _Task:
    task_name: str = "detect_mute_nodes"
    unit: ActorUnit = ActorUnit.U03
