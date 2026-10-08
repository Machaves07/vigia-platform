"""NFR-GOB-13: cardinalidad de métricas acotada por construcción (TASK-233; PAT-GOB-ESC-03).

**Metapropiedad sobre el exportador en memoria tras una ejecución con nodos generados.** La
aplicación completa de U-03 (``GobPlatform``, con PostgreSQL 16 y LocalStack) corre con las
métricas de todas sus unidades y de la cadena de middleware sobre un ``MeterProvider`` con
``InMemoryMetricReader``. Hypothesis genera flotas de 1 a 3 nodos, cada uno en su organización,
y lo que hace cada nodo por las rutas reales: latidos (también repetidos, que se ignoran, y en
ráfaga, que se limitan), hallazgos y eventos con clips de verdad, peticiones inválidas, clips de
verificación confirmados (comisionamiento) y clips huérfanos (``mark_orphan_clips`` tras 25 h).
Sobre **todo** lo exportado, tras cada ejemplo:

- ninguna métrica fuera del catálogo y ninguna etiqueta fuera de la lista permitida de la suya;
- ninguna etiqueta por zona, hallazgo, acta, usuario ni otra entidad (``ENTITY_LABELS``);
- con etiqueta de nodo, solo contadores y medidores, nunca histogramas;
- los histogramas por ruta, solo por ruta, método y código;
- a lo sumo **8 familias** de métricas por nodo (``MAX_NODE_FAMILIES``, ver la nota);
- la extrapolación a escala objetivo (100 nodos, 50 organizaciones) queda por debajo del techo
  de unas **20 000 series** (``SERIES_CEILING``).

**Sonda**: ``test_probe_histogram_with_node_id_is_caught`` emite, sobre el mismo proveedor, un
histograma con ``node_id`` y comprueba que la metapropiedad lo nombra.

**Nota (8 series por nodo).** NFR-GOB-13 y PAT-GOB-ESC-03 dicen «a lo sumo 8 series por nodo»
`[estimación propia]`; NFR-GOB-55 y TASK-223 emiten 8 **familias** por nodo
(``clip_grants_issued/used/orphaned_total``, ``fleet_heartbeats_total`` y cuatro medidores), y
``fleet_heartbeats_total`` se parte por ``result`` (``accepted`` o ``ignored``): un nodo que
reenvía un latido llega a 9 series. La propiedad acota las familias (8) y publica las series por
nodo; la extrapolación cuenta series. La lectura queda declarada en el PR de TASK-233.

Solo datos generados.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge as GaugeData
from opentelemetry.sdk.metrics.export import Histogram as HistogramData
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum

from tests.benchmarks.gob_support import (
    GRANT_SPACING_SECONDS,
    HEARTBEAT_SPACING_SECONDS,
    INGEST_SPACING_SECONDS,
    catalog_zone,
)
from tests.conftest import _active_profile, _seeds_for_profile
from tests.factories import uuid7
from tests.gob_platform_support import GobPlatform, gob_platform, ok
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.observability.metrics import (
    CATALOG,
    METER_NAME,
    Histogram,
    MetricKind,
    MetricSpec,
    PlatformMetrics,
)

pytestmark = pytest.mark.integration

ENTITY_LABELS: Final = frozenset(
    {
        "zone_id",
        "camera_id",
        "finding_id",
        "detection_id",
        "event_id",
        "episode_id",
        "clip_id",
        "record_id",
        "commissioning_record_id",
        "session_id",
        "agreement_id",
        "heartbeat_id",
        "user_id",
    }
)
"""Etiquetas prohibidas en cualquier métrica: zona, hallazgo, acta, usuario y sus parientes."""
NODE_LABEL: Final = "node_id"
ORGANIZATION_LABEL: Final = "organization_id"
ROUTE_HISTOGRAM_LABELS: Final = frozenset({"route", "method", "status_class", "code"})
MAX_NODE_FAMILIES: Final = 8
NODES_AT_SCALE: Final = 100
ORGANIZATIONS_AT_SCALE: Final = 50
SERIES_CEILING: Final = 20_000
BURST: Final = 6
"""Latidos seguidos sin avanzar el reloj: el cubo (4 por minuto) limita los últimos."""
ORPHAN_AFTER: Final = timedelta(hours=25)
BUDGET: Final[Mapping[str, int]] = {"ci": 2, "nightly": 6}
"""Ejemplos por semilla: cada uno levanta organizaciones y zonas por las rutas reales."""
PROBE: Final = "probe_node_latency_ms"


# --- lo exportado --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Point:
    """Una serie exportada: métrica, tipo y conjunto de etiquetas."""

    metric: str
    kind: MetricKind
    attributes: tuple[tuple[str, str], ...]

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(key for key, _ in self.attributes)

    def value_of(self, key: str) -> str | None:
        return dict(self.attributes).get(key)


def exported(reader: InMemoryMetricReader) -> list[Point]:
    """Las series que el lector en memoria tiene ahora (temporalidad acumulada)."""
    data = reader.get_metrics_data()
    points: set[Point] = set()
    if data is None:
        return []
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if isinstance(metric.data, HistogramData):
                    kind = MetricKind.HISTOGRAM
                elif isinstance(metric.data, GaugeData):
                    kind = MetricKind.GAUGE
                elif isinstance(metric.data, Sum):
                    kind = MetricKind.COUNTER
                else:  # pragma: no cover - el catálogo solo tiene esos tres tipos
                    raise AssertionError(f"tipo inesperado en {metric.name}")
                for point in metric.data.data_points:
                    attributes = tuple(
                        sorted((key, str(value)) for key, value in (point.attributes or {}).items())
                    )
                    points.add(Point(metric.name, kind, attributes))
    return sorted(points, key=lambda point: (point.metric, point.attributes))


def violations(points: Sequence[Point], catalog: Sequence[MetricSpec] = CATALOG) -> list[str]:
    """Cada incumplimiento de NFR-GOB-13 en ``points``, nombrado."""
    specs = {str(spec.name): spec for spec in catalog}
    found: list[str] = []
    families: dict[str, set[str]] = defaultdict(set)
    for point in points:
        spec = specs.get(point.metric)
        if spec is None:
            found.append(f"{point.metric}: métrica fuera del catálogo")
        elif not point.keys <= spec.attributes:
            found.append(f"{point.metric}: etiquetas fuera de su lista {sorted(point.keys)}")
        if spec is not None and spec.kind is not point.kind:
            found.append(f"{point.metric}: exportada como {point.kind}, declarada {spec.kind}")
        if point.keys & ENTITY_LABELS:
            found.append(
                f"{point.metric}: etiqueta de entidad {sorted(point.keys & ENTITY_LABELS)}"
            )
        if NODE_LABEL in point.keys:
            if point.kind is MetricKind.HISTOGRAM:
                found.append(f"{point.metric}: histograma con etiqueta de nodo")
            families[str(point.value_of(NODE_LABEL))].add(point.metric)
        if (
            point.kind is MetricKind.HISTOGRAM
            and "route" in point.keys
            and not point.keys <= ROUTE_HISTOGRAM_LABELS
        ):
            found.append(f"{point.metric}: histograma por ruta con {sorted(point.keys)}")
    for node, names in sorted(families.items()):
        if len(names) > MAX_NODE_FAMILIES:
            found.append(f"nodo {node}: {len(names)} familias > {MAX_NODE_FAMILIES}")
    return found


@dataclass(frozen=True)
class Extrapolation:
    shared_series: int
    max_series_per_node: int
    max_families_per_node: int
    max_series_per_organization: int
    at_scale: int


def extrapolate(points: Sequence[Point]) -> Extrapolation:
    """Series a escala objetivo: las compartidas (ni nodo ni organización) tal cual, más el
    máximo observado por nodo por 100 nodos y por organización por 50 organizaciones."""
    per_node: Counter[str] = Counter()
    families: dict[str, set[str]] = defaultdict(set)
    per_organization: Counter[str] = Counter()
    shared = 0
    for point in points:
        node = point.value_of(NODE_LABEL)
        organization = point.value_of(ORGANIZATION_LABEL)
        if node is not None:
            per_node[node] += 1
            families[node].add(point.metric)
        elif organization is not None:
            per_organization[organization] += 1
        else:
            shared += 1
    node_series = max(per_node.values(), default=0)
    organization_series = max(per_organization.values(), default=0)
    return Extrapolation(
        shared_series=shared,
        max_series_per_node=node_series,
        max_families_per_node=max((len(names) for names in families.values()), default=0),
        max_series_per_organization=organization_series,
        at_scale=shared
        + node_series * NODES_AT_SCALE
        + organization_series * ORGANIZATIONS_AT_SCALE,
    )


# --- la ejecución con nodos generados ------------------------------------------------------------


@dataclass(frozen=True)
class NodePlan:
    productive: bool
    heartbeats: int
    duplicate: bool
    burst: bool
    findings: int
    events: int
    rejected: bool
    verification: bool
    orphan: bool


node_plans: Final = st.builds(
    NodePlan,
    productive=st.booleans(),
    heartbeats=st.integers(min_value=1, max_value=3),
    duplicate=st.booleans(),
    burst=st.booleans(),
    findings=st.integers(min_value=0, max_value=2),
    events=st.integers(min_value=0, max_value=2),
    rejected=st.booleans(),
    verification=st.booleans(),
    orphan=st.booleans(),
)
fleets: Final = st.lists(node_plans, min_size=1, max_size=3)


@dataclass
class World:
    gob: GobPlatform
    reader: InMemoryMetricReader
    metrics: PlatformMetrics
    nodes: int = 0


def _drive(gob: GobPlatform, plan: NodePlan) -> None:
    """Un nodo nuevo en su organización y lo que ``plan`` le hace hacer por las rutas reales.

    La hora simulada vuelve antes a la de la base: el nodo anterior pudo avanzarla 25 h (clips
    huérfanos) y la concesión del instalador se compara con ``now()`` de la base (VIG-135).
    """
    gob.resync()
    flow, zone = catalog_zone(gob, productive=plan.productive)
    if not plan.productive:
        flow.mount(zone)
    for _ in range(plan.heartbeats):
        gob.advance(HEARTBEAT_SPACING_SECONDS)
        body = flow.heartbeat(zone)
        ok(flow.post_heartbeat(zone, body))
        if plan.duplicate:
            ok(flow.post_heartbeat(zone, body))
    if plan.burst:
        statuses = {flow.post_heartbeat(zone).status_code for _ in range(BURST)}
        assert 429 in statuses, statuses
    for _ in range(plan.findings if plan.productive else 0):
        gob.advance(INGEST_SPACING_SECONDS)
        assert flow.post_finding(zone, flow.finding(zone)).status_code == 200
    for _ in range(plan.events):
        gob.advance(INGEST_SPACING_SECONDS)
        assert flow.post_event(zone, flow.event(zone)).status_code == 200
    if plan.rejected:
        gob.advance(INGEST_SPACING_SECONDS)
        rejected = gob.run(
            flow.submit(zone, NodeRoute.FINDING, {"finding_id": str(uuid7())}, "finding_id")
        )
        assert rejected.status_code >= 400
    if plan.verification and not plan.productive:
        gob.advance(GRANT_SPACING_SECONDS)
        flow.verification_clip(zone)
    if plan.orphan:
        gob.advance(GRANT_SPACING_SECONDS)
        started = gob.now() - timedelta(minutes=5)
        flow.clip(zone, started, started + timedelta(seconds=30))
        gob.advance(ORPHAN_AFTER.total_seconds())
        gob.run_task("mark_orphan_clips", zone.organization_id)


def _check(world: World) -> Extrapolation:
    points = exported(world.reader)
    assert violations(points) == []
    extrapolation = extrapolate(points)
    assert extrapolation.at_scale < SERIES_CEILING, extrapolation
    return extrapolation


@pytest.fixture(scope="module")
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = PlatformMetrics(provider.get_meter(METER_NAME))
    with gob_platform(
        postgres_endpoint, localstack_endpoint, "cardinality_gob", metrics=metrics
    ) as gob:
        yield World(gob, reader, metrics)
    provider.shutdown()


def test_nfr_gob_13_cardinality_after_a_run_with_generated_nodes(world: World) -> None:
    examples = BUDGET.get(_active_profile(), BUDGET["nightly"])

    @settings(max_examples=examples, deadline=None, database=None)
    @given(fleet=fleets)
    def run(fleet: list[NodePlan]) -> None:
        for plan in fleet:
            _drive(world.gob, plan)
            world.nodes += 1
        _check(world)

    for value in _seeds_for_profile():
        hypothesis_seed(value)(run)()
    # La ejecución no es vacía: además de la flota generada, un nodo que lo hace todo (todas las
    # familias por nodo, también la serie ``ignored``) y su comprobación.
    _drive(world.gob, NodePlan(False, 2, True, True, 0, 1, True, True, True))
    extrapolation = _check(world)
    print(json.dumps(extrapolation.__dict__, ensure_ascii=False))
    assert extrapolation.max_families_per_node == MAX_NODE_FAMILIES, extrapolation
    assert world.nodes >= 2


def test_probe_histogram_with_node_id_is_caught(world: World) -> None:
    """La sonda: un histograma de latencia con ``node_id``, declarado en el catálogo y emitido
    por el mismo ``Histogram`` de la plataforma (con su limpieza de etiquetas), junto a lo que
    exportó la ejecución real: la metapropiedad lo nombra."""
    probe = MetricSpec(
        PROBE,  # type: ignore[arg-type]
        MetricKind.HISTOGRAM,
        "ms",
        "Sonda de TASK-233: latencia por ruta y por nodo.",
        "TASK-233",
        frozenset({"route", "method", NODE_LABEL}),
    )
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    try:
        instrument = Histogram(probe, provider.get_meter(METER_NAME), None)
        instrument.record(
            12.5, {"route": "POST /api/nodes/heartbeats", "method": "POST", NODE_LABEL: "n-1"}
        )
        points = [*exported(world.reader), *exported(reader)]
    finally:
        provider.shutdown()
    found = violations(points, (*CATALOG, probe))
    assert f"{PROBE}: histograma con etiqueta de nodo" in found, found
    assert f"{PROBE}: histograma por ruta con ['method', 'node_id', 'route']" in found, found
    # Sin la sonda, lo exportado por la ejecución real sigue limpio.
    assert violations(exported(world.reader)) == []
