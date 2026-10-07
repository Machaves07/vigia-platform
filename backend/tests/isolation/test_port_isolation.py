"""PR-GOB-12 sobre las 19 operaciones de los tres puertos de U-03 (TASK-228; NFR-GOB-30, 61).

``CatalogQueryPort`` (9), ``GateQueryPort`` (6) y ``FleetQueryPort`` (4) de interfaces §1 y
«Versión 1.5», en proceso y contra PostgreSQL 16 real como ``vigia_app``.

**Catálogo de casos** (``PORT_CASES``): una entrada por método público de cada protocolo, con su
forma (``SINGLE``, ``PLANT``, ``BATCH`` o ``RANGE``) y la llamada sobre un ``Target``.
``uncovered_operations`` compara los métodos de los protocolos con el catálogo y nombra cada
operación sin caso; ``test_a_port_operation_without_case_is_named`` lo demuestra con un protocolo
sonda. Corren sin base: la canalización las ejecuta también en el trabajo sin integración.

Con la base (el mundo de ``gob_world``: A y B con dos plantas, dos zonas por planta y un nodo por
zona; cada zona con lo que leen las quince operaciones del catálogo y las compuertas):

- con el contexto de A, el ``Target`` conocido de B responde **exactamente** como uno inexistente
  (``ResourceNotFound`` con los mismos argumentos y ningún dato); B ve lo suyo y A lo suyo, así la
  prueba no pasa por una consulta vacía;
- las cuatro formas por lote y rango (``single_occupancy_many``, ``standards_at_many``,
  ``gate_history``, ``assignment_history``) con una mezcla de A y B: igual que con un inexistente,
  nunca un resultado parcial;
- un contexto de una planta no ve la otra planta de su organización, y uno de una zona no ve la
  zona hermana de su misma planta (ni el nodo que la atiende);
- ninguna operación escribe: la huella de B no cambia con las lecturas de A.

**Seguimiento de VIG-159** (comentario de la sesión de control en VIG-165): con un contexto de zona,
``GET /fleet/nodes`` y ``nodes_by_zone`` solo muestran las cámaras que declara el catálogo vigente
de una zona visible, con su ``code``, y los avisos que dependen de ellas
(``camera_below_min_fps``, ``simulated_adapter_in_productive``) se calculan sobre lo visible.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import enum
import inspect
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

import pytest

from tests.catalog_ports_support import PortsWorld, ZoneData
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState
from tests.integration.conftest import PostgresEndpoint
from tests.isolation.gob_world import (
    GobOrganization,
    GobZone,
    changed_tables,
    fingerprint,
    two_organizations,
)
from tests.node_api_support import TestAuthority
from vigia_platform.catalog.adapters.postgres.query_ports import (
    CatalogQueryPorts,
    catalog_query_ports,
)
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import CatalogQueryPort, GateQueryPort, StandardRef
from vigia_platform.fleet.ports import FleetQueryPort
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ScopeContext, ScopeLevel
from vigia_platform.shared.signing.keys import to_millisecond

DAY: Final = timedelta(days=1)

PORT_PROTOCOLS: Final[tuple[type[Any], ...]] = (CatalogQueryPort, GateQueryPort, FleetQueryPort)
"""Los tres puertos de lectura de U-03 (interfaces §1.1 a §1.3)."""


# --- Catálogo de casos --------------------------------------------------------------------------


class Shape(enum.Enum):
    SINGLE = "single"
    """Un identificador de zona, de estándar o de nodo."""
    PLANT = "plant"
    """Una planta (``states_by_plant``, ``plant_policy``)."""
    BATCH = "batch"
    """Una lista: con un solo elemento ajeno, toda la llamada es ``ResourceNotFound``."""
    RANGE = "range"
    """Un rango de tiempo sobre una zona."""


@dataclass(frozen=True)
class Target:
    """Los identificadores de una zona sobre los que actúa una operación."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standard_id: uuid.UUID
    node_id: uuid.UUID
    t0: datetime

    @property
    def at(self) -> datetime:
        """Un instante en que rigen el catálogo 2 y las dos compuertas (``ZoneData.at``)."""
        return self.t0 + DAY + timedelta(hours=1)

    def missing(self) -> Target:
        """La misma forma con identificadores que no existen en ninguna organización."""
        return Target(
            self.organization_id, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), self.t0
        )


@dataclass(frozen=True)
class Ports:
    catalog: CatalogQueryPort
    gates: GateQueryPort
    fleet: FleetQueryPort


Call = Callable[[Ports, ScopeContext, Sequence[Target]], Awaitable[Any]]
"""La operación sobre los ``Target`` dados (por lote, todos; las demás, el último)."""


@dataclass(frozen=True)
class PortCase:
    shape: Shape
    call: Call


def _ref(target: Target) -> StandardRef:
    return StandardRef(target.zone_id, target.standard_id, target.at)


PORT_CASES: Final[dict[str, PortCase]] = {
    # --- CatalogQueryPort (9) ---
    "CatalogQueryPort.current_catalog": PortCase(
        Shape.SINGLE, lambda p, c, t: p.catalog.current_catalog(c, t[-1].zone_id)
    ),
    "CatalogQueryPort.catalog_at": PortCase(
        Shape.SINGLE, lambda p, c, t: p.catalog.catalog_at(c, t[-1].zone_id, t[-1].at)
    ),
    "CatalogQueryPort.standard_version": PortCase(
        Shape.SINGLE,
        lambda p, c, t: p.catalog.standard_version(c, t[-1].zone_id, t[-1].standard_id, 2),
    ),
    "CatalogQueryPort.standard_at": PortCase(
        Shape.SINGLE,
        lambda p, c, t: p.catalog.standard_at(c, t[-1].zone_id, t[-1].standard_id, t[-1].at),
    ),
    "CatalogQueryPort.catalog_history": PortCase(
        Shape.SINGLE, lambda p, c, t: p.catalog.catalog_history(c, t[-1].zone_id)
    ),
    "CatalogQueryPort.single_occupancy": PortCase(
        Shape.SINGLE, lambda p, c, t: p.catalog.single_occupancy(c, t[-1].zone_id, t[-1].at)
    ),
    "CatalogQueryPort.single_occupancy_many": PortCase(
        Shape.BATCH,
        lambda p, c, t: p.catalog.single_occupancy_many(c, [x.zone_id for x in t], t[-1].at),
    ),
    "CatalogQueryPort.standards_at_many": PortCase(
        Shape.BATCH, lambda p, c, t: p.catalog.standards_at_many(c, [_ref(x) for x in t])
    ),
    "CatalogQueryPort.regression_state": PortCase(
        Shape.SINGLE, lambda p, c, t: p.catalog.regression_state(c, t[-1].zone_id)
    ),
    # --- GateQueryPort (6) ---
    "GateQueryPort.state": PortCase(Shape.SINGLE, lambda p, c, t: p.gates.state(c, t[-1].zone_id)),
    "GateQueryPort.states_by_plant": PortCase(
        Shape.PLANT, lambda p, c, t: p.gates.states_by_plant(c, t[-1].plant_id)
    ),
    "GateQueryPort.state_at": PortCase(
        Shape.SINGLE, lambda p, c, t: p.gates.state_at(c, t[-1].zone_id, GateKind.USAGE, t[-1].at)
    ),
    "GateQueryPort.gate_history": PortCase(
        Shape.RANGE,
        lambda p, c, t: p.gates.gate_history(c, t[-1].zone_id, t[-1].t0, t[-1].t0 + 30 * DAY),
    ),
    "GateQueryPort.plant_policy": PortCase(
        Shape.PLANT, lambda p, c, t: p.gates.plant_policy(c, t[-1].plant_id)
    ),
    "GateQueryPort.current_agreement": PortCase(
        Shape.SINGLE, lambda p, c, t: p.gates.current_agreement(c, t[-1].zone_id)
    ),
    # --- FleetQueryPort (4) ---
    "FleetQueryPort.nodes_by_zone": PortCase(
        Shape.SINGLE, lambda p, c, t: p.fleet.nodes_by_zone(c, t[-1].zone_id)
    ),
    "FleetQueryPort.node": PortCase(Shape.SINGLE, lambda p, c, t: p.fleet.node(c, t[-1].node_id)),
    "FleetQueryPort.assignment_at": PortCase(
        Shape.SINGLE, lambda p, c, t: p.fleet.assignment_at(c, t[-1].zone_id, t[-1].at)
    ),
    "FleetQueryPort.assignment_history": PortCase(
        Shape.RANGE,
        lambda p, c, t: p.fleet.assignment_history(
            c, t[-1].zone_id, t[-1].t0 - 60 * DAY, t[-1].t0 + 30 * DAY
        ),
    ),
}
"""Un caso por operación pública de los tres protocolos (``uncovered_operations`` lo exige)."""


def protocol_operations(protocols: Sequence[type[Any]]) -> set[str]:
    """``Protocolo.método`` de cada método público **declarado** en cada protocolo."""
    return {
        f"{protocol.__name__}.{name}"
        for protocol in protocols
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def uncovered_operations(protocols: Sequence[type[Any]] = PORT_PROTOCOLS) -> list[str]:
    """Las operaciones de ``protocols`` sin caso de aislamiento en ``PORT_CASES``."""
    return sorted(protocol_operations(protocols) - set(PORT_CASES))


# --- Cobertura del catálogo (sin base) -----------------------------------------------------------


def test_every_port_operation_has_its_isolation_case() -> None:
    missing = uncovered_operations()
    assert not missing, f"operaciones de puerto sin caso de aislamiento: {missing}"
    assert set(PORT_CASES) == protocol_operations(PORT_PROTOCOLS)
    assert len(PORT_CASES) == 19
    shapes = {name for name, case in PORT_CASES.items() if case.shape in (Shape.BATCH, Shape.RANGE)}
    assert shapes == {
        "CatalogQueryPort.single_occupancy_many",
        "CatalogQueryPort.standards_at_many",
        "GateQueryPort.gate_history",
        "FleetQueryPort.assignment_history",
    }


class ProbePort(Protocol):
    """Un puerto sonda con una operación nueva y sin caso (NFR-GOB-61)."""

    async def probe_without_case(self, context: ScopeContext, zone_id: uuid.UUID) -> None: ...


class ProbeCatalogPort(CatalogQueryPort, Protocol):
    """Una operación nueva añadida al protocolo existente."""

    async def zones_by_standard(self, context: ScopeContext, standard_id: uuid.UUID) -> None: ...


def test_a_port_operation_without_case_is_named() -> None:
    assert uncovered_operations((*PORT_PROTOCOLS, ProbePort)) == ["ProbePort.probe_without_case"]
    assert uncovered_operations((*PORT_PROTOCOLS, ProbeCatalogPort)) == [
        "ProbeCatalogPort.zones_by_standard"
    ]
    with pytest.raises(AssertionError, match=r"ProbePort\.probe_without_case"):
        missing = uncovered_operations((*PORT_PROTOCOLS, ProbePort))
        assert not missing, f"operaciones de puerto sin caso de aislamiento: {missing}"


# --- El mundo ------------------------------------------------------------------------------------


@dataclass
class PortWorld:
    stack: FleetStack
    data: PortsWorld
    ports: Ports
    a: GobOrganization
    b: GobOrganization
    zones: dict[uuid.UUID, ZoneData]

    def run(self, awaitable: Awaitable[Any]) -> Any:
        return self.stack.run(awaitable)

    def target(self, organization: GobOrganization, plant: int = 0, index: int = 0) -> Target:
        zone = organization.zone(plant, index)
        data = self.zones[zone.zone_id]
        node = organization.node(plant, index)
        return Target(
            zone.organization_id, zone.plant_id, zone.zone_id, data.standard_id, node.node_id,
            data.t0,
        )  # fmt: skip

    def context(self, organization: GobOrganization, *scopes: tuple[ScopeLevel, uuid.UUID]) -> Any:
        return self.data.context(organization.organization_id, *scopes)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[PortWorld]:
    with fleet_stack(postgres_endpoint, "port_isolation") as stack:
        clock = stack.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        catalog: CatalogQueryPorts = catalog_query_ports(stack.database)
        data = PortsWorld(stack.authz, catalog)
        zones: dict[uuid.UUID, ZoneData] = {}

        def seed(zone: GobZone) -> None:
            zones[zone.zone_id] = data.populate(zone.organization_id, zone.plant_id, zone.zone_id)

        a, b = two_organizations(stack, TestAuthority(), stack.authz.now(), seed)
        fleet = stack.services.inventory
        assert fleet is not None
        ports = Ports(catalog.catalog, catalog.gates, fleet)
        yield PortWorld(stack, data, ports, a, b, zones)


def _outcome(world: PortWorld, awaitable: Awaitable[Any]) -> tuple[type[BaseException], Any]:
    """La excepción y sus argumentos; un resultado es un fallo (se vio un dato)."""
    try:
        result = world.run(awaitable)
    except ResourceNotFound as error:
        return type(error), error.args
    pytest.fail(f"respondió con datos: {result!r}")


# --- PR-GOB-12: el contexto de A contra los identificadores de B ---------------------------------


@pytest.mark.integration
def test_pr_gob_12_each_operation_answers_a_foreign_identifier_like_a_missing_one(
    world: PortWorld,
) -> None:
    mine, theirs = world.target(world.a), world.target(world.b)
    context = world.context(world.a)
    their_context = world.context(world.b)
    before = fingerprint(world.stack.fetch, world.b.organization_id)
    failures: list[str] = []
    for name, case in PORT_CASES.items():
        absent = _outcome(world, case.call(world.ports, context, [theirs.missing()]))
        foreign = _outcome(world, case.call(world.ports, context, [theirs]))
        if foreign != absent or absent[0] is not ResourceNotFound:
            failures.append(f"{name}: {foreign} != {absent}")
        # La prueba no pasa por una consulta vacía: A ve lo suyo y B ve lo de B.
        world.run(case.call(world.ports, context, [mine]))
        world.run(case.call(world.ports, their_context, [theirs]))
    assert not failures, "\n".join(failures)
    assert changed_tables(before, fingerprint(world.stack.fetch, world.b.organization_id)) == []


@pytest.mark.integration
def test_pr_gob_12_batch_and_range_forms_with_a_mix_of_a_and_b(world: PortWorld) -> None:
    mine, theirs = world.target(world.a), world.target(world.b)
    sibling = world.target(world.a, 0, 1)
    context = world.context(world.a)
    batched = [n for n, c in PORT_CASES.items() if c.shape in (Shape.BATCH, Shape.RANGE)]
    assert len(batched) == 4
    for name in batched:
        case = PORT_CASES[name]
        if case.shape is Shape.BATCH:
            # Un elemento de B entre los de A, en cualquier posición: igual que un inexistente.
            for mixed, absent in (
                ([mine, theirs], [mine, theirs.missing()]),
                ([theirs, mine, sibling], [theirs.missing(), mine, sibling]),
                ([mine, sibling, theirs], [mine, sibling, theirs.missing()]),
            ):
                found = _outcome(world, case.call(world.ports, context, mixed))
                assert found == _outcome(world, case.call(world.ports, context, absent)), name
            # Sin el elemento de B, la lista responde entera y en orden.
            result = world.run(case.call(world.ports, context, [mine, sibling, mine]))
            assert len(result) == 3, name
        else:
            # El rango sobre la zona de B: igual que sobre una zona inexistente.
            found = _outcome(world, case.call(world.ports, context, [theirs]))
            assert found == _outcome(world, case.call(world.ports, context, [theirs.missing()]))
            world.run(case.call(world.ports, context, [mine]))


@pytest.mark.integration
def test_a_plant_or_zone_context_never_sees_the_rest_of_its_organization(
    world: PortWorld,
) -> None:
    a = world.a
    mine = world.target(a, 0, 0)
    sibling = world.target(a, 0, 1)
    elsewhere = world.target(a, 1, 0)
    plant_context = world.context(a, (ScopeLevel.PLANT, mine.plant_id))
    zone_context = world.context(a, (ScopeLevel.ZONE, mine.zone_id))
    failures: list[str] = []
    for context, label, hidden in (
        (plant_context, "planta", [elsewhere]),
        (zone_context, "zona", [sibling, elsewhere]),
    ):
        for name, case in PORT_CASES.items():
            for target in hidden:
                if (
                    context is zone_context
                    and case.shape is Shape.PLANT
                    and target.plant_id == mine.plant_id
                ):
                    continue  # la planta de un contexto de zona es la suya: abajo
                absent = _outcome(world, case.call(world.ports, context, [target.missing()]))
                seen = _outcome(world, case.call(world.ports, context, [target]))
                if seen != absent:
                    failures.append(f"{name} ({label}): ve {target.zone_id}")
            world.run(case.call(world.ports, context, [mine]))
    assert not failures, "\n".join(failures)
    # La planta de un contexto de zona, solo con su zona.
    states = world.run(world.ports.gates.states_by_plant(zone_context, mine.plant_id))
    assert [state.zone_id for state in states] == [mine.zone_id]
    plant_states = world.run(world.ports.gates.states_by_plant(plant_context, mine.plant_id))
    assert {state.zone_id for state in plant_states} == {mine.zone_id, sibling.zone_id}
    # Un lote con la zona hermana o de la otra planta: entero not_found.
    single = PORT_CASES["CatalogQueryPort.single_occupancy_many"]
    for context, outside in ((zone_context, sibling), (plant_context, elsewhere)):
        mixed = _outcome(world, single.call(world.ports, context, [mine, outside]))
        assert mixed == _outcome(world, single.call(world.ports, context, [outside.missing()]))


# --- Seguimiento de VIG-159: las cámaras de las zonas hermanas -----------------------------------


def _by_id(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {node["node_id"]: node for node in nodes}


@pytest.mark.integration
def test_a_zone_context_sees_only_the_cameras_and_warnings_of_its_visible_zones(
    world: PortWorld,
) -> None:
    inventory = InventoryWorld.build(world.stack)
    plant = inventory.plants[0]
    visible, sibling, _ = inventory.zones(plant)
    node = inventory.declare(plant, [visible, sibling])
    now = inventory.next_now()
    # Una cámara declarada en cada zona; la de la zona hermana, por debajo de su mínimo; el
    # adaptador simulado, y solo la zona hermana en modo productivo.
    inventory.write_state(
        node, NodeState(cameras=((25.0, 10.0), (3.0, 10.0)), adapter="simulated"), now
    )
    newest = world.stack.fetch(
        "SELECT camera_id, measured_fps FROM fleet.camera_inventory WHERE node_id = $1",
        node,
    )
    fast = next(row["camera_id"] for row in newest if row["measured_fps"] > 10)
    slow = next(row["camera_id"] for row in newest if row["measured_fps"] < 10)
    inventory.add_catalog(visible, [(fast, "CAM-VISIBLE")])
    inventory.add_catalog(sibling, [(slow, "CAM-HERMANA")])
    inventory.set_gate(visible, mounting="approved", usage="pending")
    inventory.set_gate(sibling, mounting="approved", usage="approved")
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    whole = _by_id(inventory.list_all(inventory.admin))[str(node)]
    assert {c["code"] for c in whole["cameras"]} == {"CAM-VISIBLE", "CAM-HERMANA"}
    assert {"camera_below_min_fps", "simulated_adapter_in_productive"} <= set(whole["warnings"])
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    zone_admin = inventory.member(ScopeLevel.ZONE, visible)
    seen = _by_id(inventory.list_all(zone_admin))[str(node)]
    assert seen["zones"] == [str(visible)]
    assert [(c["camera_id"], c["code"]) for c in seen["cameras"]] == [(str(fast), "CAM-VISIBLE")]
    assert "CAM-HERMANA" not in str(seen) and str(slow) not in str(seen)
    assert "camera_below_min_fps" not in seen["warnings"]
    assert "simulated_adapter_in_productive" not in seen["warnings"]
    # El puerto, con el mismo alcance: ``nodes_by_zone`` y ``node``.
    context = world.data.context(inventory.organization, (ScopeLevel.ZONE, visible))
    (found,) = world.run(world.ports.fleet.nodes_by_zone(context, visible))
    assert [(c.camera_id, c.code) for c in found.cameras] == [(fast, "CAM-VISIBLE")]
    detail = world.run(world.ports.fleet.node(context, node))
    assert [c.camera_id for c in detail.cameras] == [fast]
    assert {w.value for w in detail.warnings}.isdisjoint(
        {"camera_below_min_fps", "simulated_adapter_in_productive"}
    )
    # Con el alcance de la planta (o de la organización), todo.
    plant_context = world.data.context(inventory.organization, (ScopeLevel.PLANT, plant))
    (planted,) = world.run(world.ports.fleet.nodes_by_zone(plant_context, visible))
    assert {c.camera_id for c in planted.cameras} == {fast, slow}
