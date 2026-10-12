"""NFR-GOB-01: latencia de las rutas del contrato hacia el nodo (TASK-233; PAT-GOB-REN-08).

Banco de cada ruta del nodo por el objetivo local de la conformidad (TASK-230): la aplicación
completa de U-03 (``GobPlatform``) con PostgreSQL 16, LocalStack y el doble ``MemoryKms`` de
``vigia-node-ca``; el nodo se presenta con las cabeceras mTLS del balanceador. Cada banco prepara
su propia organización y una zona ``productive`` por las rutas reales, y cada ronda prepara fuera
de la medida lo que el nodo ya tendría (el documento, los clips subidos, el código de alta) y
avanza el reloj simulado lo justo para que el cubo de fichas no limite (``gob_support``).

=================================== ==================================================
Banco                               Objetivo p95 `[objetivo propio]` de NFR-GOB-01
=================================== ==================================================
``gob_node_finding_1_clip``         ≤ 500 ms (p99 ≤ 1 500 ms), el hallazgo mínimo
``gob_node_finding_2_clips``        ≤ 500 ms (p99 ≤ 1 500 ms), dos cámaras con su clip
``gob_node_detection``              ≤ 500 ms (p99 ≤ 1 500 ms), con su clip
``gob_node_observability_event``    ≤ 200 ms
``gob_node_heartbeat``              ≤ 150 ms, con compuertas firmadas y catálogos
``gob_node_clip_grant``             ≤ 100 ms
``gob_node_zone_catalog``           ≤ 100 ms (catálogo firmado de la zona)
``gob_node_enrollment``             ≤ 2 000 ms, con la firma del doble de KMS
=================================== ==================================================

La ingesta siempre verifica sus clips en S3 (``HeadObject``). El «hallazgo sin clips» de TASK-233
no es representable: ``FindingSubmission`` exige al menos un clip por cámara presente
(``cameras[].clips`` con ``minItems`` 1), así que el banco mínimo lleva un clip.

Mediana, p95 y p99 en el informe; el p99 de la ingesta se compara con su objetivo en
``details``. Factor de regresión 1,2 (NFR-GOB-64). Perfil ``nightly``. Solo datos generados.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.models import api

from tests.benchmarks.conftest import GOB_REGRESSION_FACTOR, Measure, Result
from tests.benchmarks.gob_support import (
    CATALOG_SPACING_SECONDS,
    GRANT_SPACING_SECONDS,
    HEARTBEAT_SPACING_SECONDS,
    INGEST_SPACING_SECONDS,
    catalog_zone,
    declared_node,
)
from tests.factories import uuid7
from tests.gob_platform_support import GobPlatform, GobZone, Onboarding
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

INGEST_P95_MS: Final = 500.0
INGEST_P99_MS: Final = 1_500.0
EVENT_MS: Final = 200.0
HEARTBEAT_MS: Final = 150.0
GRANT_MS: Final = 100.0
CATALOG_MS: Final = 100.0
ENROLLMENT_MS: Final = 2_000.0


class Rounds:
    """Lo que prepara cada ronda (fuera de la medida) y las respuestas que deja la medida."""

    def __init__(self) -> None:
        self.prepared: Any = None
        self.responses: list[httpx.Response] = []

    def statuses(self) -> set[int]:
        return {response.status_code for response in self.responses}


def _ingest_details(result: Result) -> None:
    """El p99 de la ingesta frente a su objetivo propio (1 500 ms) va en ``details``."""
    assert result.p99 is not None
    result.details |= {
        "objective_p99_ms": INGEST_P99_MS,
        "objective_p99_met": result.p99 <= INGEST_P99_MS,
    }


def _zone(gob: GobPlatform) -> tuple[Onboarding, GobZone]:
    return catalog_zone(gob, cameras=2, productive=True)


def _finding(flow: Onboarding, zone: GobZone, cameras: int) -> dict[str, Any]:
    """Un ``FindingSubmission`` con un clip ``full`` ya subido por cada una de ``cameras``
    cámaras: la originaria y, con 2, una corroboradora (el contrato exige al menos un clip por
    cámara presente, así que 1 clip es el hallazgo mínimo)."""
    gob = flow.gob
    started = gob.now() - timedelta(minutes=5)
    ended = started + timedelta(seconds=30)
    clips = []
    for camera in range(cameras):
        gob.advance(GRANT_SPACING_SECONDS)
        clips.append(flow.clip(zone, started, ended, camera=camera))
    document = flow.finding(zone, started, clip=clips[0])
    document["cameras"] = [
        {"camera_id": str(zone.cameras[camera]), "originating": camera == 0,
         "clips": [clips[camera]]}
        for camera in range(cameras)
    ]  # fmt: skip
    api.parse_finding_submission(json.dumps(document).encode())
    return document


def _bench_ingest(
    gob: GobPlatform,
    measure: Measure,
    *,
    name: str,
    label: str,
    route: NodeRoute,
    id_field: str,
    prepare: Callable[[Onboarding, GobZone], dict[str, Any]],
    objective: float,
    p99: bool,
) -> None:
    flow, zone = _zone(gob)
    rounds = Rounds()

    def setup() -> None:
        gob.advance(INGEST_SPACING_SECONDS)
        rounds.prepared = prepare(flow, zone)

    def target() -> None:
        rounds.responses.append(gob.run(flow.submit(zone, route, rounds.prepared, id_field)))

    result = measure(
        name,
        label,
        target,
        setup=setup,
        objective_ms=objective,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"route": f"{route.method} {route.relative_path}"},
    )
    if p99:
        _ingest_details(result)
    assert rounds.statuses() == {200}, [r.text for r in rounds.responses if r.status_code != 200]


def test_nfr_gob_01_finding_1_clip(gob: GobPlatform, measure: Measure) -> None:
    _bench_ingest(
        gob,
        measure,
        name="gob_node_finding_1_clip",
        label="Ingesta de un hallazgo con su clip verificado en S3, el mínimo (NFR-GOB-01)",
        route=NodeRoute.FINDING,
        id_field="finding_id",
        prepare=lambda flow, zone: _finding(flow, zone, 1),
        objective=INGEST_P95_MS,
        p99=True,
    )


def test_nfr_gob_01_finding_2_clips(gob: GobPlatform, measure: Measure) -> None:
    _bench_ingest(
        gob,
        measure,
        name="gob_node_finding_2_clips",
        label="Ingesta de un hallazgo con 2 clips (2 cámaras) verificados en S3 (NFR-GOB-01)",
        route=NodeRoute.FINDING,
        id_field="finding_id",
        prepare=lambda flow, zone: _finding(flow, zone, 2),
        objective=INGEST_P95_MS,
        p99=True,
    )


def test_nfr_gob_01_detection(gob: GobPlatform, measure: Measure) -> None:
    def prepare(flow: Onboarding, zone: GobZone) -> dict[str, Any]:
        flow.gob.advance(GRANT_SPACING_SECONDS)
        return flow.detection(zone)

    _bench_ingest(
        gob,
        measure,
        name="gob_node_detection",
        label="Ingesta de una detección para revisión con su clip (NFR-GOB-01)",
        route=NodeRoute.DETECTION_REVIEW,
        id_field="detection_id",
        prepare=prepare,
        objective=INGEST_P95_MS,
        p99=True,
    )


def test_nfr_gob_01_observability_event(gob: GobPlatform, measure: Measure) -> None:
    _bench_ingest(
        gob,
        measure,
        name="gob_node_observability_event",
        label="Evento de observabilidad de una cámara (NFR-GOB-01)",
        route=NodeRoute.OBSERVABILITY_EVENT,
        id_field="event_id",
        prepare=lambda flow, zone: flow.event(zone),
        objective=EVENT_MS,
        p99=False,
    )


def test_nfr_gob_01_heartbeat(gob: GobPlatform, measure: Measure) -> None:
    flow, zone = _zone(gob)
    rounds = Rounds()

    def setup() -> None:
        gob.advance(HEARTBEAT_SPACING_SECONDS)
        rounds.prepared = flow.heartbeat(zone)

    def target() -> None:
        rounds.responses.append(
            gob.run(gob.node_send("POST", NodeRoute.HEARTBEAT.path, certificate=zone.cert,
                                  body=rounds.prepared))
        )  # fmt: skip

    measure(
        "gob_node_heartbeat",
        "Latido con la respuesta de compuertas firmadas y catálogos (NFR-GOB-01)",
        target,
        setup=setup,
        objective_ms=HEARTBEAT_MS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"route": "POST /heartbeats", "zones": 1, "cameras": len(zone.cameras)},
    )
    assert rounds.statuses() == {200}
    assert all(response.json()["gate_states"] for response in rounds.responses)


def test_nfr_gob_01_clip_grant(gob: GobPlatform, measure: Measure) -> None:
    _, zone = _zone(gob)
    rounds = Rounds()

    def setup() -> None:
        gob.advance(GRANT_SPACING_SECONDS)
        rounds.prepared = {
            "clip_id": str(uuid7()),
            "camera_id": str(zone.cameras[0]),
            "zone_id": str(zone.zone_id),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": uuid7().hex * 2,
            "size_bytes": 1_048_576,
            "duration_ms": 50_000,
            "purpose": "evidence",
        }

    def target() -> None:
        rounds.responses.append(
            gob.run(gob.node_send("POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert,
                                  body=rounds.prepared))
        )  # fmt: skip

    measure(
        "gob_node_clip_grant",
        "Concesión de subida de un clip (URL prefirmada) (NFR-GOB-01)",
        target,
        setup=setup,
        objective_ms=GRANT_MS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"route": "POST /clip-uploads"},
    )
    assert rounds.statuses() == {200}, [r.text for r in rounds.responses if r.status_code != 200]


def test_nfr_gob_01_zone_catalog(gob: GobPlatform, measure: Measure) -> None:
    _, zone = _zone(gob)
    path = NodeRoute.ZONE_CATALOG.path.format(zone_id=zone.zone_id)
    rounds = Rounds()

    def setup() -> None:
        gob.advance(CATALOG_SPACING_SECONDS)

    def target() -> None:
        rounds.responses.append(gob.run(gob.node_send("GET", path, certificate=zone.cert)))

    measure(
        "gob_node_zone_catalog",
        "Catálogo firmado de la zona para el nodo (NFR-GOB-01)",
        target,
        setup=setup,
        objective_ms=CATALOG_MS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"route": "GET /zones/{zone_id}/catalog"},
    )
    assert rounds.statuses() == {200}


def test_nfr_gob_01_enrollment(gob: GobPlatform, measure: Measure) -> None:
    """Cada ronda: una zona más de la planta con su nodo declarado y su código (fuera de la
    medida); la medida es ``POST /enrollment`` con la firma de ``vigia-node-ca`` en el doble."""
    flow, first = _zone(gob)
    rounds = Rounds()

    def setup() -> None:
        zone = declared_node(flow, first)
        rounds.prepared = (flow.enrollment_body(zone, flow.code(zone)), gob.address())

    def target() -> None:
        body, address = rounds.prepared
        rounds.responses.append(
            gob.run(gob.node_send("POST", NodeRoute.ENROLLMENT.path, certificate=None, body=body,
                                  address=address))
        )  # fmt: skip

    measure(
        "gob_node_enrollment",
        "Alta de un nodo con la firma del doble de KMS de vigia-node-ca (NFR-GOB-01)",
        target,
        setup=setup,
        objective_ms=ENROLLMENT_MS,
        regression_factor=GOB_REGRESSION_FACTOR,
        details={"route": "POST /enrollment", "kms": "MemoryKms (P-256)"},
    )
    assert rounds.statuses() == {200}, [r.text for r in rounds.responses if r.status_code != 200]
