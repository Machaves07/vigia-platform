"""Zonas y ritmos de los bancos de U-03 (TASK-233; NFR-GOB-01, 03, 05, 09 y 10).

Los bancos de U-03 corren sobre ``GobPlatform`` (la aplicación completa de
``tests/gob_platform_support.py``, con PostgreSQL 16 y LocalStack) y preparan sus zonas por las
**rutas reales** con ``Onboarding``. Aquí está lo que comparten:

- ``catalog_zone``: una zona con ``standards`` estándares y ``cameras`` cámaras (la matriz máxima
  de NFR-GOB-05 es 32 y 8), con su nodo declarado y dado de alta y, si se pide, montada y en
  ``productive``.
- Los **ritmos**: el reloj simulado avanza entre rondas, fuera de la medida, lo justo para que el
  cubo de fichas por nodo de NFR-GOB-33 (4 latidos por minuto, 240 registros por minuto, 480
  concesiones por minuto, 30 catálogos por minuto) nunca responda ``rate_limited``: el banco mide
  la ruta, no el límite.

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import math
import secrets
import uuid
from datetime import timedelta
from typing import Any, Final

from tests.gob_platform_support import (
    COEXISTENCE,
    REASON,
    GobPlatform,
    GobZone,
    Onboarding,
    close_body,
    ok,
    stamp,
    zone_parameters,
)
from tests.isolation.gob_world import unique_node_code

__all__ = [
    "CATALOG_SPACING_SECONDS",
    "GRANT_SPACING_SECONDS",
    "HEARTBEAT_SPACING_SECONDS",
    "INGEST_SPACING_SECONDS",
    "MAX_CAMERAS",
    "MAX_STANDARDS",
    "WALK_TEST_PASSES_PER_CELL",
    "catalog_zone",
    "closed_record",
    "declared_node",
    "open_walk_test",
    "standard_body",
]

MAX_STANDARDS: Final = 32
MAX_CAMERAS: Final = 8
HEARTBEAT_SPACING_SECONDS: Final = 15.5
"""4 latidos por minuto y nodo (NFR-GOB-33): una ficha cada 15 s."""
INGEST_SPACING_SECONDS: Final = 0.3
"""240 hallazgos, detecciones y eventos por minuto y nodo: una ficha cada 0,25 s."""
GRANT_SPACING_SECONDS: Final = 0.15
"""480 concesiones de subida por minuto y nodo: una ficha cada 0,125 s."""
CATALOG_SPACING_SECONDS: Final = 2.1
"""30 lecturas de catálogo por minuto y nodo: una ficha cada 2 s."""


def standard_body(index: int) -> dict[str, Any]:
    """Un estándar de coexistencia más de la zona (sin parámetros: el catálogo ya existe)."""
    return {
        **COEXISTENCE,
        "title_es": f"Coexistencia en la celda, estándar {index}",
        "reason_es": REASON,
    }


def catalog_zone(
    gob: GobPlatform,
    *,
    standards: int = 1,
    cameras: int = 2,
    productive: bool = False,
    within: GobZone | None = None,
) -> tuple[Onboarding, GobZone]:
    """Una zona con su nodo dado de alta, ``standards`` estándares y ``cameras`` cámaras.

    Cada estándar más es una versión del catálogo por ``POST /zones/{zone_id}/standards``; con
    ``productive``, la zona se monta y pasa a ``productive`` por las rutas de H-49 y H-50.
    """
    flow = Onboarding(gob)
    zone = flow.zone(cameras=cameras, within=within)
    for index in range(1, standards):
        ok(
            gob.call(
                "POST",
                f"/zones/{zone.zone_id}/standards",
                cookie=zone.admin,
                json_body=standard_body(index),
            ),
            201,
        )
    if productive:
        flow.mount(zone)
        flow.productive(zone)
    return flow, zone


WALK_TEST_PASSES_PER_CELL: Final = 25
"""Con las cuatro posturas de un estándar, 100 pases, como H-51."""
LATENCY_REPETITIONS: Final = 100
"""Pases con los que el acta mide la latencia (BR-GOB-48): por debajo, ``latency_not_measured``."""


def open_walk_test(
    flow: Onboarding, zone: GobZone, passes_per_cell: int = WALK_TEST_PASSES_PER_CELL
) -> str:
    """Sesión de walk-test de la zona montada con **todas** sus celdas completas (``detected``);
    devuelve ``session_id``."""
    session = ok(
        flow.as_installer(
            zone,
            "POST",
            f"/zones/{zone.zone_id}/walk-tests",
            {"passes_per_cell": passes_per_cell},
        ),
        201,
    )
    session_id: str = session["session_id"]
    current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
    rows = current["session"]["rows"]
    # Al menos las 100 repeticiones con las que el acta mide la latencia (BR-GOB-48).
    per_row = max(passes_per_cell, math.ceil(LATENCY_REPETITIONS / len(rows)))
    for row in rows:
        for _ in range(per_row):
            ok(
                flow.as_installer(
                    zone,
                    "POST",
                    f"/walk-tests/{session_id}/passes",
                    {"row_id": row["row_id"], "result": "detected"},
                ),
                201,
            )
    return session_id


def closed_record(
    flow: Onboarding, zone: GobZone, passes_per_cell: int = WALK_TEST_PASSES_PER_CELL
) -> str:
    """El acta de comisionamiento de la zona montada, **cerrada por la ruta** (H-51): matriz
    completa, una prueba de oclusión declarada por cámara y el clip de verificación; devuelve
    ``commissioning_record_id``."""
    gob = flow.gob
    session_id = open_walk_test(flow, zone, passes_per_cell)
    for camera in zone.cameras:
        ended = gob.now() - timedelta(seconds=2)
        body = {
            "camera_id": str(camera),
            "started_at": stamp(ended - timedelta(seconds=20)),
            "ended_at": stamp(ended),
        }
        path = f"/walk-tests/{session_id}/occlusion-tests"
        ok(flow.as_installer(zone, "POST", path, body), 201)
        ok(flow.as_installer(zone, "POST", path, {**body, "declared_reason_es": REASON}), 201)
    flow.verification_clip(zone)
    record = ok(
        flow.as_installer(zone, "POST", f"/walk-tests/{session_id}/close", close_body(gob, zone))
    )
    record_id: str = record["commissioning_record_id"]
    return record_id


def declared_node(flow: Onboarding, within: GobZone, *, cameras: int = 1) -> GobZone:
    """Otra zona de la planta de ``within`` con su catálogo y su nodo **declarado**, sin alta.

    Como ``Onboarding.zone(within=..., enrolled=False)`` pero sin avanzar el reloj: el banco del
    alta prepara una por ronda y 10 minutos por ronda vencerían la sesión del instalador.
    """
    gob = flow.gob
    zone_id = uuid.uuid4()
    gob.authz.add_zone(within.organization_id, within.plant_id, zone_id)
    camera_ids = tuple(uuid.uuid4() for _ in range(cameras))
    signal_id = uuid.uuid4()
    ok(
        gob.call(
            "POST",
            f"/zones/{zone_id}/standards",
            cookie=within.admin,
            json_body={
                **COEXISTENCE,
                "reason_es": REASON,
                "zone_parameters": zone_parameters(camera_ids, signal_id),
            },
        ),
        201,
    )
    zone = dataclasses.replace(
        within,
        zone_id=zone_id,
        cameras=camera_ids,
        signal_id=signal_id,
        node_id=None,
        certificate=None,
        hardware_fingerprint=secrets.token_hex(32),
    )
    declared = ok(
        flow.as_installer(
            zone,
            "POST",
            f"/plants/{zone.plant_id}/nodes",
            {"code": unique_node_code(), "zone_ids": [str(zone_id)]},
        ),
        201,
    )
    zone.node_id = uuid.UUID(declared["node_id"])
    return zone
