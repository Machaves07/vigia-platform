"""PR-GOB-30 (TASK-213; LC-GOB-23a; PAT-GOB-REN-04) sobre ``CatalogQueryPort`` y ``GateQueryPort``,
contra PostgreSQL 16.

Oráculo (perfil ``ci``, semilla fija y semilla de la sesión registradas por ``tests/conftest.py``):

- ``single_occupancy_many``: para una lista generada de hasta 50 zonas (con repeticiones, en
  cualquier orden) y un instante opcional, devuelve **exactamente** lo mismo que repetir
  ``single_occupancy`` zona a zona, en el orden declarado, y lo que dice el modelo de las versiones
  (``issued_at <= at < superseded_at``, la de número mayor si una duró cero); si una zona no tiene
  catálogo en el instante, la llamada entera es ``ResourceNotFound``, nunca un resultado parcial;
- ``standards_at_many``: para hasta 200 referencias ``{zone_id, standard_id, at}`` (instantes en
  los bordes de las vigencias o en cualquier punto), lo mismo que repetir ``standard_at`` y lo que
  dice el modelo de vigencias ``[effective_from, effective_until)``; una sin versión vigente hace
  ``ResourceNotFound`` la llamada entera;
- ``gate_history``: para una historia generada de las dos compuertas de una zona (intervalos
  contiguos o con hueco, aprobados o revocados, el último quizá abierto) y un rango de hasta
  366 días, exactamente los intervalos del modelo que se solapan con ``[from, to]``, por compuerta
  y ``effective_from``; lo mismo, en el mismo orden, que ``state_at`` en los bordes de cada
  intervalo; y ``state_at`` en un instante cualquiera es el intervalo del modelo o ``None``;
- los topes: 51 zonas, 201 referencias, 367 días (o 366 días y un microsegundo) responden
  ``PortLimitExceeded`` y **nunca** un resultado truncado; 50, 200 y 366 días exactos, todo.

Las zonas del catálogo se generan una vez por módulo con una semilla fija (``random.Random``);
cada ejemplo de compuertas usa una zona nueva (la historia es de solo anexar). Solo datos
generados (NFR-CTR-43); las marcas parten de ``BASE_TIME``.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.catalog_ports_support import PortsWorld, ports_world
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import (
    PortLimitExceeded,
    SingleOccupancy,
    StandardRef,
)
from vigia_platform.catalog.domain.standard import DeclaredStandardVersion
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ScopeContext

pytestmark = pytest.mark.integration

US: Final = timedelta(microseconds=1)
MS: Final = timedelta(milliseconds=1)
DAY: Final = timedelta(days=1)
LIMIT: Final = timedelta(days=366)
POOL_SEED: Final = 20261005
ZONES: Final = 8
STANDARDS: Final = 4


@dataclass(frozen=True)
class Version:
    number: int
    issued_at: datetime
    superseded_at: datetime | None
    single_occupancy: bool
    window: int


@dataclass(frozen=True)
class StandardSpan:
    standard_id: uuid.UUID
    version: int
    catalog_version: int
    effective_from: datetime
    effective_until: datetime | None


@dataclass
class Zone:
    zone_id: uuid.UUID
    versions: list[Version] = field(default_factory=list)
    standards: list[StandardSpan] = field(default_factory=list)
    standard_ids: list[uuid.UUID] = field(default_factory=list)

    def version_at(self, at: datetime | None) -> Version | None:
        if at is None:
            return next((v for v in self.versions if v.superseded_at is None), None)
        valid = [
            v
            for v in self.versions
            if v.issued_at <= at and (v.superseded_at is None or v.superseded_at > at)
        ]
        return max(valid, key=lambda v: v.number) if valid else None

    def standard_at(self, standard_id: uuid.UUID, at: datetime) -> StandardSpan | None:
        valid = [
            s
            for s in self.standards
            if s.standard_id == standard_id
            and s.effective_from <= at
            and (s.effective_until is None or s.effective_until > at)
        ]
        assert len(valid) <= 1
        return valid[0] if valid else None

    def borders(self) -> list[datetime]:
        edges = [v.issued_at for v in self.versions]
        return sorted({e + d for e in edges for d in (-MS, -US, timedelta(0), US, MS)})


@dataclass
class World:
    ports: PortsWorld
    context: ScopeContext
    plant: uuid.UUID
    zones: list[Zone]
    origin: datetime


def _build_zone(world: PortsWorld, organization: uuid.UUID, plant: uuid.UUID,
                rng: random.Random, origin: datetime) -> Zone:  # fmt: skip
    zone = Zone(uuid.uuid4())
    world.authz.add_zone(organization, plant, zone.zone_id)
    keys = (organization, plant, zone.zone_id)
    if rng.random() < 0.15:
        return zone  # una zona sin catálogo
    start = origin + timedelta(milliseconds=rng.randrange(0, 30 * 86_400_000))
    count = rng.randint(1, 6)
    issued = [start]
    for _ in range(count - 1):
        # A veces cero (una versión que dura cero, como la de un reloj que retrocede).
        gap = rng.choice((0, 1, 3_600_000, rng.randrange(1, 60 * 86_400_000)))
        issued.append(issued[-1] + timedelta(milliseconds=gap))
    for index, at in enumerate(issued):
        until = issued[index + 1] if index + 1 < count else None
        version = Version(index + 1, at, until, rng.random() < 0.5, rng.randint(15, 480))
        zone.versions.append(version)
        world.add_version(
            *keys,
            version.number,
            at,
            superseded_at=until,
            single_occupancy=version.single_occupancy,
            window=version.window,
        )
    # Estándares: nacen en una versión del catálogo; en cada una siguiente, una versión nueva
    # (que retira la anterior), un retiro sin sucesora o nada.
    current: dict[uuid.UUID, StandardSpan] = {}
    for version in zone.versions:
        if version.number == 1 or rng.random() < 0.3:
            for _ in range(rng.randint(1, STANDARDS) if version.number == 1 else 1):
                standard_id = uuid.uuid4()
                zone.standard_ids.append(standard_id)
                current[standard_id] = StandardSpan(
                    standard_id, 1, version.number, version.issued_at, None
                )
                world.add_standard(*keys, standard_id, 1, version.number, version.issued_at)
        if version.number == 1:
            continue
        for standard_id, span in list(current.items()):
            if span.catalog_version == version.number:
                continue
            action = rng.choice(("keep", "keep", "version", "retire"))
            if action == "keep":
                continue
            closed = StandardSpan(
                standard_id, span.version, span.catalog_version, span.effective_from,
                version.issued_at,
            )  # fmt: skip
            zone.standards.append(closed)
            world.execute(
                "UPDATE catalog.declared_standard_version SET retired_in_catalog_version = $3"
                " WHERE standard_id = $1 AND version = $2",
                standard_id,
                span.version,
                version.number,
            )
            del current[standard_id]
            if action == "version":
                successor = StandardSpan(
                    standard_id, span.version + 1, version.number, version.issued_at, None
                )
                current[standard_id] = successor
                world.add_standard(
                    *keys, standard_id, successor.version, version.number, version.issued_at
                )
    zone.standards.extend(current.values())
    return zone


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with ports_world(postgres_endpoint, "pr_gob_30_catalog") as built:
        site = built.site(plants=1, zones=0)
        (plant,) = site.plants
        rng = random.Random(POOL_SEED)  # noqa: S311 - datos sintéticos de prueba, no criptografía
        origin = BASE_TIME
        zones = [_build_zone(built, site.organization_id, plant, rng, origin) for _ in range(ZONES)]
        assert any(z.versions for z in zones) and any(not z.versions for z in zones)
        yield World(built, built.context(site.organization_id), plant, zones, origin)


def _instant(data: st.DataObject, zones: list[Zone], label: str) -> datetime:
    """Un borde de una versión (o su vecino) o un instante cualquiera del intervalo del pool."""
    borders = sorted({b for z in zones for b in z.borders()})
    if borders and data.draw(st.booleans(), label=f"{label} en un borde"):
        return data.draw(st.sampled_from(borders), label=label)
    offset = data.draw(st.integers(-86_400_000, 400 * 86_400_000), label=label)
    return BASE_TIME + timedelta(milliseconds=offset)


def _expected_occupancy(zone: Zone, at: datetime | None) -> SingleOccupancy | None:
    version = zone.version_at(at)
    if version is None:
        return None
    return SingleOccupancy(
        zone_id=zone.zone_id,
        single_occupancy=version.single_occupancy,
        aggregation_window_minutes=version.window,
        catalog_version=version.number,
        issued_at=version.issued_at,
    )


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_pr_gob_30_single_occupancy_many_equals_the_unit_form(
    world: World, data: st.DataObject
) -> None:
    run = world.ports.run
    catalog = world.ports.ports.catalog
    indices = data.draw(st.lists(st.integers(0, ZONES - 1), max_size=50), label="zonas")
    at = data.draw(st.none() | st.just(0), label="¿instante?")
    moment = None if at is None else _instant(data, world.zones, "at")
    zones = [world.zones[i] for i in indices]
    ids = [z.zone_id for z in zones]
    expected = [_expected_occupancy(z, moment) for z in zones]
    units: list[SingleOccupancy | None] = []
    for zone in zones:
        try:
            units.append(run(catalog.single_occupancy(world.context, zone.zone_id, moment)))
        except ResourceNotFound:
            units.append(None)
    assert units == expected
    if all(item is not None for item in expected):
        assert list(run(catalog.single_occupancy_many(world.context, ids, moment))) == units
    else:
        # Una sola zona sin catálogo en el instante: la llamada entera, nunca parcial.
        with pytest.raises(ResourceNotFound):
            run(catalog.single_occupancy_many(world.context, ids, moment))
    # El tope: 51 zonas es un error antes de consultar, nunca 50 de ellas.
    padded = (ids or [world.zones[0].zone_id]) * 51
    with pytest.raises(PortLimitExceeded):
        run(catalog.single_occupancy_many(world.context, padded[:51], moment))


def _standard_matches(found: DeclaredStandardVersion, span: StandardSpan, zone: Zone) -> bool:
    return (
        found.zone_id == zone.zone_id
        and found.standard_id == span.standard_id
        and found.version == span.version
        and found.catalog_version == span.catalog_version
        and found.effective_from == span.effective_from
        and found.effective_until == span.effective_until
    )


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_pr_gob_30_standards_at_many_equals_the_unit_form(
    world: World, data: st.DataObject
) -> None:
    run = world.ports.run
    catalog = world.ports.ports.catalog
    with_standards = [z for z in world.zones if z.standard_ids]
    picks = data.draw(
        st.lists(
            st.tuples(st.sampled_from(with_standards), st.integers(0, 10**6), st.booleans()),
            max_size=200,
        ),
        label="referencias",
    )
    refs: list[StandardRef] = []
    expected: list[StandardSpan | None] = []
    zones: list[Zone] = []
    for zone, pick, foreign in picks:
        # A veces el estándar de otra zona de la organización: no es de esta zona.
        owner = with_standards[pick % len(with_standards)] if foreign else zone
        standard_id = owner.standard_ids[pick % len(owner.standard_ids)]
        moment = _instant(data, [zone], "at")
        refs.append(StandardRef(zone.zone_id, standard_id, moment))
        expected.append(zone.standard_at(standard_id, moment))
        zones.append(zone)
    units: list[DeclaredStandardVersion | None] = []
    for ref in refs:
        try:
            units.append(run(catalog.standard_at(world.context, ref.zone_id, ref.standard_id,
                                                 ref.at)))  # fmt: skip
        except ResourceNotFound:
            units.append(None)
    for unit, span, zone in zip(units, expected, zones, strict=True):
        if span is None:
            assert unit is None
        else:
            assert unit is not None and _standard_matches(unit, span, zone)
    if all(unit is not None for unit in units):
        assert list(run(catalog.standards_at_many(world.context, refs))) == units
    else:
        with pytest.raises(ResourceNotFound):
            run(catalog.standards_at_many(world.context, refs))
    padded = (refs or [StandardRef(with_standards[0].zone_id, uuid.uuid4(), BASE_TIME)]) * 201
    with pytest.raises(PortLimitExceeded):
        run(catalog.standards_at_many(world.context, padded[:201]))


# --- gate_history ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    gate: GateKind
    status: str
    start: timedelta
    end: timedelta | None


@st.composite
def gate_histories(draw: st.DrawFn) -> list[Interval]:
    """Intervalos sin solape por compuerta (la exclusión GiST), contiguos o con hueco."""
    intervals: list[Interval] = []
    for gate in GateKind:
        count = draw(st.integers(0, 5))
        cursor = timedelta(milliseconds=draw(st.integers(0, 30 * 86_400_000)))
        for index in range(count):
            length = timedelta(
                milliseconds=draw(
                    st.one_of(st.integers(1, 1_000), st.integers(1, 200 * 86_400_000))
                )
            )
            last = index == count - 1
            end = None if last and draw(st.booleans()) else cursor + length
            intervals.append(
                Interval(gate, draw(st.sampled_from(("approved", "revoked"))), cursor, end)
            )
            if end is None:
                break
            cursor = end + timedelta(milliseconds=draw(st.sampled_from((0, 0, 1, 90 * 86_400_000))))
    return intervals


def _borders(intervals: list[Interval]) -> list[int]:
    edges = [i.start for i in intervals] + [i.end for i in intervals if i.end is not None]
    ms = [edge // MS for edge in edges]
    return sorted({m + d for m in ms for d in (-1, 0, 1) if m + d >= 0})


def _point(data: st.DataObject, intervals: list[Interval], label: str) -> int:
    borders = _borders(intervals)
    if borders and data.draw(st.booleans(), label=f"{label} en un borde"):
        return data.draw(st.sampled_from(borders), label=label)
    return data.draw(st.integers(0, 500 * 86_400_000), label=label)


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(intervals=gate_histories(), data=st.data())
def test_pr_gob_30_gate_history_equals_state_at_on_the_borders(
    world: World, intervals: list[Interval], data: st.DataObject
) -> None:
    ports = world.ports
    run, gates = ports.run, ports.ports.gates
    organization = world.context.organization_id
    zone = uuid.uuid4()
    ports.authz.add_zone(organization, world.plant, zone)
    origin = world.origin
    records: dict[Interval, uuid.UUID | None] = {}
    for interval in intervals:
        records[interval] = ports.add_interval(
            organization,
            world.plant,
            zone,
            interval.gate,
            interval.status,
            origin + interval.start,
            None if interval.end is None else origin + interval.end,
        )
    offset = _point(data, intervals, "from")
    limit_ms = LIMIT // MS
    length = data.draw(st.one_of(st.just(limit_ms), st.just(0), st.integers(0, limit_ms)),
                       label="to - from")  # fmt: skip
    start = origin + offset * MS
    end = start + length * MS

    def overlaps(i: Interval) -> bool:
        return origin + i.start <= end and (i.end is None or origin + i.end > start)

    history = run(gates.gate_history(world.context, zone, start, end))
    expected = sorted((i for i in intervals if overlaps(i)), key=lambda i: (i.gate.value, i.start))
    assert [
        (h.gate, h.status.value, h.effective_from, h.effective_until, h.record_id) for h in history
    ] == [
        (i.gate, i.status, origin + i.start, None if i.end is None else origin + i.end, records[i])
        for i in expected
    ]
    # Lo mismo, en el mismo orden, que state_at en los bordes de cada intervalo.
    at_borders = []
    for interval in history:
        found = run(gates.state_at(world.context, zone, interval.gate, interval.effective_from))
        assert found == interval
        if interval.effective_until is not None:
            last = run(gates.state_at(world.context, zone, interval.gate,
                                      interval.effective_until - US))  # fmt: skip
            assert last == interval
        at_borders.append(found)
    assert tuple(at_borders) == history
    # state_at en un instante cualquiera: el intervalo del modelo o ninguno (pending).
    probe = origin + _point(data, intervals, "instante") * MS
    gate = data.draw(st.sampled_from(list(GateKind)), label="compuerta")
    covering = [
        i
        for i in intervals
        if i.gate is gate
        and origin + i.start <= probe
        and (i.end is None or origin + i.end > probe)
    ]
    found = run(gates.state_at(world.context, zone, gate, probe))
    if covering:
        assert found is not None and found.effective_from == origin + covering[0].start
    else:
        assert found is None
    for too_long in (LIMIT + US, LIMIT + DAY):
        with pytest.raises(PortLimitExceeded):
            run(gates.gate_history(world.context, zone, start, start + too_long))
