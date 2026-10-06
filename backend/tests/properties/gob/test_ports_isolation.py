"""PR-GOB-12 (TASK-213; NFR-GOB-30; LC-GOB-23a) sobre **cada** operación de ``CatalogQueryPort``
y ``GateQueryPort``, contra PostgreSQL 16. Bloqueante.

Oráculo (perfil ``ci``, semillas registradas por ``tests/conftest.py``): con el contexto de la
organización A (de organización, de una planta o de una zona) y el identificador conocido de un
recurso de la organización B, cada una de las quince operaciones, incluidas las formas por lote
(``single_occupancy_many``, ``standards_at_many``) y por rango (``gate_history``), responde
**exactamente** como con un identificador que no existe: ``ResourceNotFound`` con los mismos
argumentos y sin ningún dato. Lo mismo con un recurso de A fuera del alcance del contexto (la
otra planta o la zona hermana). Un lote con un solo elemento de B, en cualquier posición entre
elementos visibles de A, también es ``ResourceNotFound`` entero, nunca un resultado parcial.

Para que la prueba no pase por una consulta vacía, el mismo recurso responde con su propio
contexto (B ve lo suyo) y los recursos visibles de A responden. Solo datos generados
(NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.catalog_ports_support import (
    OPERATION_NAMES,
    PortsWorld,
    ZoneData,
    missing,
    operations,
    ports_world,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.domain.ports import StandardRef
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ScopeContext, ScopeLevel

pytestmark = pytest.mark.integration

LEVELS = ("organization", "plant", "zone")


@dataclass
class World:
    ports: PortsWorld
    mine: ZoneData
    """La zona de A visible con cualquier contexto de A de esta prueba."""
    sibling: ZoneData
    """La otra zona de la misma planta de A (fuera de un contexto de zona)."""
    elsewhere: ZoneData
    """Una zona de la otra planta de A (fuera de un contexto de planta o de zona)."""
    theirs: ZoneData
    """Una zona de la organización B."""

    def context(self, level: str) -> ScopeContext:
        organization = self.mine.organization_id
        if level == "plant":
            return self.ports.context(organization, (ScopeLevel.PLANT, self.mine.plant_id))
        if level == "zone":
            return self.ports.context(organization, (ScopeLevel.ZONE, self.mine.zone_id))
        return self.ports.context(organization)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with ports_world(postgres_endpoint, "pr_gob_12_ports") as built:
        site = built.site(plants=2, zones=2)
        populated = [built.populate(site.organization_id, p, z) for p, z in site.zones()]
        mine, sibling, elsewhere, _ = populated
        other = built.site()
        theirs = built.populate(other.organization_id, *other.zones()[0])
        yield World(built, mine, sibling, elsewhere, theirs)


def _outcome(
    world: World, awaitable: Awaitable[Any]
) -> tuple[type[BaseException], tuple[Any, ...]]:
    """La excepción y sus argumentos; un resultado es un fallo (se vio un dato)."""
    try:
        result = world.ports.run(awaitable)
    except ResourceNotFound as error:
        return type(error), error.args
    pytest.fail(f"respondió con datos: {result!r}")


def _hidden(world: World, level: str) -> list[ZoneData]:
    """Los recursos de A fuera del alcance del contexto de ese nivel."""
    if level == "zone":
        return [world.sibling, world.elsewhere]
    if level == "plant":
        return [world.elsewhere]
    return []


@given(name=st.sampled_from(OPERATION_NAMES), level=st.sampled_from(LEVELS))
def test_pr_gob_12_another_organization_is_exactly_like_a_missing_resource(
    world: World, name: str, level: str
) -> None:
    context = world.context(level)
    ports = world.ports.ports
    absent = _outcome(world, operations(ports, missing(world.theirs))[name](context))
    assert absent[0] is ResourceNotFound
    assert _outcome(world, operations(ports, world.theirs)[name](context)) == absent
    for hidden in _hidden(world, level):
        if (
            level == "zone"
            and name in ("states_by_plant", "plant_policy")
            and (hidden.plant_id == world.mine.plant_id)
        ):
            continue  # la planta de un contexto de zona es la suya (con solo su zona)
        assert _outcome(world, operations(ports, hidden)[name](context)) == absent, hidden
    # La prueba no pasa por una consulta vacía: A ve lo suyo y B ve lo de B.
    world.ports.run(operations(ports, world.mine)[name](context))
    theirs = world.ports.context(world.theirs.organization_id)
    world.ports.run(operations(ports, world.theirs)[name](theirs))


@given(
    level=st.sampled_from(LEVELS),
    visible=st.integers(0, 49),
    position=st.integers(0, 49),
)
def test_pr_gob_12_one_foreign_element_hides_the_whole_batch(
    world: World, level: str, visible: int, position: int
) -> None:
    context = world.context(level)
    catalog = world.ports.ports.catalog
    zones = [world.mine.zone_id] * visible
    zones.insert(min(position, len(zones)), world.theirs.zone_id)
    absent = _outcome(world, catalog.single_occupancy_many(context, [uuid.uuid4()]))
    assert _outcome(world, catalog.single_occupancy_many(context, zones)) == absent
    mine = StandardRef(world.mine.zone_id, world.mine.standard_id, world.mine.at)
    theirs = StandardRef(world.theirs.zone_id, world.theirs.standard_id, world.theirs.at)
    refs = [mine] * visible
    refs.insert(min(position, len(refs)), theirs)
    assert _outcome(world, catalog.standards_at_many(context, refs)) == absent
    # El estándar de B citado con una zona visible de A: tampoco existe en esa zona.
    crossed = [*([mine] * visible), StandardRef(world.mine.zone_id, theirs.standard_id, theirs.at)]
    assert _outcome(world, catalog.standards_at_many(context, crossed)) == absent
    # Sin el elemento de B, la misma lista responde entera.
    assert (
        len(world.ports.run(catalog.single_occupancy_many(context, [world.mine.zone_id] * visible)))
        == visible
    )
    assert len(world.ports.run(catalog.standards_at_many(context, [mine] * visible))) == visible
