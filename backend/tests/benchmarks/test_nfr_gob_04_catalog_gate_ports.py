"""Banco de NFR-GOB-04: los puertos de lectura del catálogo y las compuertas (TASK-213; LC-GOB-23a).

Sobre PostgreSQL 16 en contenedor, como ``vigia_app`` (con la seguridad a nivel de fila), con **un
año** de datos generados de la organización mayor de NFR-GOB-11: 20 plantas, 60 zonas y
32 estándares por zona. Por zona: 52 versiones del catálogo (una por semana, con un
``ZoneCatalog`` de 32 estándares y 4 cámaras y su sobre), cada versión nueva del catálogo
reversiona un estándar (83 versiones de estándar por zona), 12 intervalos por compuerta
(aprobación y revocación mensuales), 12 acuerdos de uso con tres confirmaciones (uno vigente) y su
proyección y su regresión; por planta, tres versiones de la política.

**10 000 llamadas consecutivas por operación** (NFR-GOB-64), recorriendo las zonas y los instantes
del año, con el marco de ``tests/benchmarks/conftest.py`` (mediana y p95 de cada operación en el
informe, frente a ``baseline.json`` con el factor 1,2 de U-03, NFR-GOB-64; TASK-233 lo incorpora
a la línea base sin duplicarlo). Objetivos `[objetivo propio]` de NFR-GOB-04: p95 ≤ 5 ms en
las operaciones puntuales y ≤ 50 ms en las de lote y rango (``single_occupancy_many`` con 50 zonas,
``standards_at_many`` con 200 referencias, ``gate_history`` de 366 días) y en las que devuelven una
lista (``catalog_history``, ``states_by_plant``). El umbral (regresión frente a la base) se aplica
en ``nightly``: solo corre con ``--hypothesis-profile=nightly``. Solo datos generados.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.benchmarks.conftest import GOB_REGRESSION_FACTOR, Measure
from tests.catalog_ports_support import PortsWorld, ports_world
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.volumetry.scale_data import CatalogZone, catalog_history, load_catalog_history
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import StandardRef
from vigia_platform.shared.context import ScopeContext

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

SEED: Final = 20261005
PLANTS: Final = 20
ZONES_PER_PLANT: Final = 3
STANDARDS: Final = 32
VERSIONS: Final = 52
GATE_INTERVALS: Final = 12
AGREEMENTS: Final = 12
POLICIES: Final = 3
CALLS: Final = 10_000
WEEK: Final = timedelta(days=7)
MONTH: Final = timedelta(days=30)
YEAR: Final = timedelta(days=365)
POINT_MS: Final = 5.0
BATCH_MS: Final = 50.0
CAMERAS: Final = tuple(uuid.UUID(int=i + 1) for i in range(4))


@dataclass(frozen=True)
class SeededZone:
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    standards: tuple[uuid.UUID, ...]


@dataclass
class Bench:
    world: PortsWorld
    context: ScopeContext
    plants: list[uuid.UUID]
    zones: list[SeededZone]
    origin: datetime
    rng: random.Random

    def instant(self) -> datetime:
        """Un instante del año sembrado (después del primer catálogo de toda zona)."""
        return self.origin + timedelta(seconds=self.rng.randrange(60, int(YEAR.total_seconds())))


def _seed(
    world: PortsWorld, rng: random.Random
) -> tuple[uuid.UUID, list[uuid.UUID], list[SeededZone]]:
    """El año de la organización mayor con ``scale_data.catalog_history`` (la misma forma que
    la volumetría de U-03): una versión del catálogo por semana, intervalos y acuerdos mensuales."""
    site = world.site(plants=PLANTS, zones=ZONES_PER_PLANT)
    organization = site.organization_id
    signers = [(role, world.authz.add_user(organization))
               for role in ("coordinator_sst", "plant_manager", "copasst")]  # fmt: skip
    seeded = [
        SeededZone(plant, zone, tuple(uuid.uuid4() for _ in range(STANDARDS)))
        for plant, zone in site.zones()
    ]
    history = catalog_history(
        organization,
        world.user_id,
        signers,
        [CatalogZone(z.plant_id, z.zone_id, z.standards, CAMERAS) for z in seeded],
        list(site.plants),
        origin=BASE_TIME - YEAR,
        now=BASE_TIME,
        rng=rng,
        versions=VERSIONS,
        version_spacing=WEEK,
        gate_intervals=GATE_INTERVALS,
        agreements=AGREEMENTS,
        policies=POLICIES,
    )
    world.run(load_catalog_history(world.authz.sessions.admin, history))
    return organization, list(site.plants), seeded


@pytest.fixture(scope="module")
def bench(postgres_endpoint: PostgresEndpoint) -> Iterator[Bench]:
    with ports_world(postgres_endpoint, "nfr_gob_04_ports") as world:
        rng = random.Random(SEED)  # noqa: S311 - datos sintéticos del banco, no criptografía
        organization, plants, zones = _seed(world, rng)
        assert len(plants) == PLANTS and len(zones) == PLANTS * ZONES_PER_PLANT
        yield Bench(world, world.context(organization), plants, zones,
                    BASE_TIME - YEAR, rng)  # fmt: skip


Call = Callable[[Bench], Awaitable[Any]]


def _zone(bench: Bench) -> SeededZone:
    return bench.rng.choice(bench.zones)


def _ref(bench: Bench) -> StandardRef:
    zone = _zone(bench)
    return StandardRef(zone.zone_id, bench.rng.choice(zone.standards), bench.instant())


def _current_catalog(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.current_catalog(b.context, _zone(b).zone_id)


def _catalog_at(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.catalog_at(b.context, _zone(b).zone_id, b.instant())


def _standard_version(b: Bench) -> Awaitable[Any]:
    zone = _zone(b)
    return b.world.ports.catalog.standard_version(
        b.context, zone.zone_id, b.rng.choice(zone.standards), 1
    )


def _standard_at(b: Bench) -> Awaitable[Any]:
    ref = _ref(b)
    return b.world.ports.catalog.standard_at(b.context, ref.zone_id, ref.standard_id, ref.at)


def _catalog_history(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.catalog_history(b.context, _zone(b).zone_id)


def _single_occupancy(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.single_occupancy(b.context, _zone(b).zone_id, b.instant())


def _single_occupancy_many(b: Bench) -> Awaitable[Any]:
    zones = [zone.zone_id for zone in b.rng.sample(b.zones, 50)]
    return b.world.ports.catalog.single_occupancy_many(b.context, zones, b.instant())


def _standards_at_many(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.standards_at_many(b.context, [_ref(b) for _ in range(200)])


def _regression_state(b: Bench) -> Awaitable[Any]:
    return b.world.ports.catalog.regression_state(b.context, _zone(b).zone_id)


def _state(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.state(b.context, _zone(b).zone_id)


def _states_by_plant(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.states_by_plant(b.context, b.rng.choice(b.plants))


def _state_at(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.state_at(b.context, _zone(b).zone_id, GateKind.USAGE, b.instant())


def _gate_history(b: Bench) -> Awaitable[Any]:
    start = b.instant() - YEAR / 2
    return b.world.ports.gates.gate_history(
        b.context, _zone(b).zone_id, start, start + timedelta(days=366)
    )


def _plant_policy(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.plant_policy(b.context, b.rng.choice(b.plants))


def _current_agreement(b: Bench) -> Awaitable[Any]:
    return b.world.ports.gates.current_agreement(b.context, _zone(b).zone_id)


OPERATIONS: Final[dict[str, tuple[str, float, Call]]] = {
    "current_catalog": ("Catálogo vigente de una zona (32 estándares)", POINT_MS, _current_catalog),
    "catalog_at": ("Catálogo vigente en un instante del año", POINT_MS, _catalog_at),
    "standard_version": ("Versión 1 de un estándar de la zona", POINT_MS, _standard_version),
    "standard_at": ("Versión de un estándar vigente en un instante", POINT_MS, _standard_at),
    "catalog_history": ("Historia del catálogo (52 versiones, una página)", BATCH_MS,
                        _catalog_history),
    "single_occupancy": ("Marca unipersonal de una zona en un instante", POINT_MS,
                         _single_occupancy),
    "single_occupancy_many": ("Marca unipersonal de 50 zonas en un instante", BATCH_MS,
                              _single_occupancy_many),
    "standards_at_many": ("200 referencias de estándar en instantes del año", BATCH_MS,
                          _standards_at_many),
    "regression_state": ("Estado de regresión de una zona", POINT_MS, _regression_state),
    "state": ("Estado de compuertas de una zona", POINT_MS, _state),
    "states_by_plant": ("Compuertas de las zonas de una planta", BATCH_MS, _states_by_plant),
    "state_at": ("Compuerta de uso en un instante, desde la historia", POINT_MS, _state_at),
    "gate_history": ("Historia de las dos compuertas en 366 días", BATCH_MS, _gate_history),
    "plant_policy": ("Política vigente de una planta", POINT_MS, _plant_policy),
    "current_agreement": ("Acuerdo vigente de una zona con sus firmantes", POINT_MS,
                          _current_agreement),
}  # fmt: skip
"""Nombre → (etiqueta, objetivo p95 en ms, llamada con argumentos sorteados)."""


@pytest.mark.parametrize("name", list(OPERATIONS))
def test_nfr_gob_04_port_operation(bench: Bench, measure: Measure, name: str) -> None:
    label, objective, call = OPERATIONS[name]
    assert bench.world.run(call(bench)) is not None or name == "state_at"

    def target() -> object:
        # Sortear los argumentos (sin E/S) cuesta microsegundos frente a la lectura.
        return bench.world.run(call(bench))

    result = measure(
        f"gob_ports_{name}",
        f"{label} (NFR-GOB-04)",
        target,
        objective_ms=objective,
        rounds=CALLS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={
            "calls": CALLS,
            "plants": PLANTS,
            "zones": PLANTS * ZONES_PER_PLANT,
            "standards_per_zone": STANDARDS,
            "catalog_versions_per_zone": VERSIONS,
        },
    )
    assert result.samples == CALLS
