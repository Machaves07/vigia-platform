"""NFR-GOB-03: rutas de la consola y del inventario (TASK-233; PAT-GOB-REN-08; NFR-GOB-64).

Banco de cada ruta de NFR-GOB-03 sobre la aplicación completa de U-03 (``GobPlatform``) con
PostgreSQL 16 y LocalStack, con sesión real y por las rutas reales. Una organización con:

- **100 nodos** dados de alta en una planta, cada uno con su zona, 2 cámaras (8 la del acta) y su
  latido, de modo que ``GET /fleet/nodes`` devuelve una página completa con sus cámaras; el nodo
  del detalle lleva 100 latidos de historia (la página de ``GET /fleet/nodes/{node_id}``);
- la zona del **acta**, con la matriz máxima de NFR-GOB-05 (32 estándares, 8 cámaras, 4 posturas,
  3 pases por celda: 3 072 pases), su walk-test cerrado por ``POST /walk-tests/{id}/close`` y
  32 versiones del catálogo; sobre ella se leen el catálogo vigente, el historial, las compuertas
  y la transparencia;
- otra zona con la misma matriz y su walk-test **abierto** con todos los pases, para
  ``GET /zones/{zone_id}/walk-tests/current``.

=========================================== =========================================
Banco                                       Objetivo p95 `[objetivo propio]`
=========================================== =========================================
``gob_console_fleet_nodes``                 ≤ 500 ms (página de 100 nodos)
``gob_console_fleet_node_detail``           ≤ 300 ms (con 100 latidos de historia)
``gob_console_walk_test_current``           ≤ 200 ms
``gob_console_zone_catalog``                ≤ 200 ms
``gob_console_catalog_versions``            ≤ 200 ms
``gob_console_zone_gates``                  ≤ 200 ms
``gob_console_zone_transparency``           ≤ 200 ms
``gob_console_commissioning_record``        ≤ 300 ms (acta estructurada)
``gob_console_documents``                   ≤ 300 ms (concesión del documento firmado)
=========================================== =========================================

Las lecturas las hace la administración de la organización; ``POST /documents``, el instalador
del proveedor con su concesión (como en H-49). Factor de regresión 1,2. Perfil ``nightly``. Los
datos a escala objetivo con un año de la organización mayor (NFR-GOB-11) los mide la volumetría
(``tests/volumetry/generate_scale_data.py``). Solo datos generados.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from tests.benchmarks.conftest import GOB_REGRESSION_FACTOR, Measure
from tests.benchmarks.gob_support import (
    HEARTBEAT_SPACING_SECONDS,
    MAX_CAMERAS,
    MAX_STANDARDS,
    catalog_zone,
    closed_record,
    declared_node,
    open_walk_test,
)
from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

NODES: Final = 100
HISTORY: Final = 100
MATRIX_PASSES_PER_CELL: Final = 3
PERSON_SPACING_SECONDS: Final = 0.2
"""600 peticiones por minuto y sesión (U-02): una ficha cada 0,1 s."""
DOCUMENT_BYTES: Final = 4_096


@dataclass
class Console:
    gob: GobPlatform
    flow: Onboarding
    record_zone: GobZone
    walk_zone: GobZone
    record_id: str
    detail_node: uuid.UUID


def _build(gob: GobPlatform) -> Console:
    gob.resync()
    flow, record_zone = catalog_zone(gob, standards=MAX_STANDARDS, cameras=MAX_CAMERAS)
    flow.mount(record_zone)
    record_id = closed_record(flow, record_zone, MATRIX_PASSES_PER_CELL)
    _, walk_zone = catalog_zone(
        gob, standards=MAX_STANDARDS, cameras=MAX_CAMERAS, within=record_zone
    )
    flow.mount(walk_zone)
    open_walk_test(flow, walk_zone, MATRIX_PASSES_PER_CELL)
    zones = [record_zone, walk_zone]
    while len(zones) < NODES:
        zone = declared_node(flow, record_zone, cameras=2)
        zone.certificate = flow.enroll(zone)
        zones.append(zone)
    for zone in zones:
        ok(flow.post_heartbeat(zone))
    detail = zones[-1]
    for _ in range(HISTORY - 1):
        gob.advance(HEARTBEAT_SPACING_SECONDS)
        ok(flow.post_heartbeat(detail))
    return Console(gob, flow, record_zone, walk_zone, record_id, detail.node)


@pytest.fixture(scope="module")
def console(_gob_world: GobPlatform) -> Iterator[Console]:
    yield _build(_gob_world)


def _document_body(console: Console) -> dict[str, Any]:
    data = uuid.uuid4().bytes * (DOCUMENT_BYTES // 16)
    return {
        "plant_id": str(console.record_zone.plant_id),
        "kind": "plant_policy",
        "content_type": "application/pdf",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


Request = Callable[[Console], tuple[str, str, dict[str, Any]]]


def _admin_get(path: Callable[[Console], str]) -> Request:
    return lambda c: ("GET", path(c), {"cookie": c.record_zone.admin})


ROUTES: Final[dict[str, tuple[str, float, Request]]] = {
    "fleet_nodes": (
        "GET /fleet/nodes, página de 100 nodos con sus cámaras",
        500.0,
        _admin_get(lambda c: "/fleet/nodes"),
    ),
    "fleet_node_detail": (
        "GET /fleet/nodes/{node_id} con 100 latidos de historia",
        300.0,
        _admin_get(lambda c: f"/fleet/nodes/{c.detail_node}"),
    ),
    "walk_test_current": (
        "GET /zones/{zone_id}/walk-tests/current, matriz máxima con todos sus pases",
        200.0,
        _admin_get(lambda c: f"/zones/{c.walk_zone.zone_id}/walk-tests/current"),
    ),
    "zone_catalog": (
        "GET /zones/{zone_id}/catalog, 32 estándares y 8 cámaras",
        200.0,
        _admin_get(lambda c: f"/zones/{c.record_zone.zone_id}/catalog"),
    ),
    "catalog_versions": (
        "GET /zones/{zone_id}/catalog/versions, 32 versiones",
        200.0,
        _admin_get(lambda c: f"/zones/{c.record_zone.zone_id}/catalog/versions"),
    ),
    "zone_gates": (
        "GET /zones/{zone_id}/gates",
        200.0,
        _admin_get(lambda c: f"/zones/{c.record_zone.zone_id}/gates"),
    ),
    "zone_transparency": (
        "GET /zones/{zone_id}/transparency",
        200.0,
        _admin_get(lambda c: f"/zones/{c.record_zone.zone_id}/transparency"),
    ),
    "commissioning_record": (
        "GET /commissioning-records/{record_id}, acta estructurada de la matriz máxima",
        300.0,
        _admin_get(lambda c: f"/commissioning-records/{c.record_id}"),
    ),
    "documents": (
        "POST /documents, concesión de subida de un documento firmado",
        300.0,
        lambda c: (
            "POST",
            "/documents",
            {
                "cookie": c.record_zone.installer,
                "concession": c.record_zone.concession,
                "json_body": _document_body(c),
            },
        ),
    ),
}
"""Banco → (etiqueta, objetivo p95 en ms, petición)."""


@pytest.mark.parametrize("route", list(ROUTES))
def test_nfr_gob_03_console_route(console: Console, measure: Measure, route: str) -> None:
    label, objective, request = ROUTES[route]
    gob = console.gob
    responses: list[httpx.Response] = []
    prepared: list[tuple[str, str, dict[str, Any]]] = []

    def setup() -> None:
        gob.advance(PERSON_SPACING_SECONDS)
        prepared[:] = [request(console)]

    def target() -> None:
        method, path, options = prepared[0]
        responses.append(gob.run(gob.send(method, path, **options)))

    measure(
        f"gob_console_{route}",
        f"{label} (NFR-GOB-03)",
        target,
        setup=setup,
        objective_ms=objective,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"nodes": NODES, "history": HISTORY, "standards": MAX_STANDARDS,
                 "cameras": MAX_CAMERAS, "passes_per_cell": MATRIX_PASSES_PER_CELL},
    )  # fmt: skip
    expected = 201 if route == "documents" else 200
    statuses = {response.status_code for response in responses}
    assert statuses == {expected}, [r.text for r in responses if r.status_code != expected][:3]
    if route == "fleet_nodes":
        assert len(responses[-1].json()["nodes"]) == NODES
