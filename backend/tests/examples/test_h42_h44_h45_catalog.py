"""Pruebas de ejemplo de las historias del catálogo de U-03 (TASK-209; SCR-04).

Cada prueba recorre el criterio de aceptación de su historia con un caso concreto, de extremo a
extremo, sobre la aplicación real y PostgreSQL 16 real (``tests/catalog_routes_support.py``): solo
peticiones HTTP, como las hará U-05.

- **H-42** Cada versión del catálogo se conserva y se consulta con su sobre idéntico al emitido
  (BR-GOB-01, 02, 06; PAT-GOB-REN-02).
- **H-44** El umbral cambiado muestra motivo, autor y fecha, y deja la regresión pendiente sin
  detener la zona (BR-GOB-03, 51 a 56).
- **H-45** Una zona se configura de punta a punta solo con las rutas de parámetros, sin cambio de
  código (nota de business-rules §1, v1.5).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from vigia_contracts.canonical import canonical_sha256
from vigia_contracts.signing import verify

from tests.catalog_routes_support import (
    GUARD_ON,
    PRESENCE,
    REASON,
    CatalogRoutes,
    camera,
    catalog_routes_world,
    first_standard_body,
    standard_body,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def routes(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "catalog_examples") as world:
        yield world


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def test_h42_every_version_is_kept_with_the_envelope_it_was_issued_with(
    routes: CatalogRoutes,
) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)
    calls_before = routes.signer.calls
    issued: list[dict[str, Any]] = [routes.configure(admin, zone)]
    for path, body in (
        ("thresholds", {"review": 0.35, "publication": 0.85}),
        ("signals", {"signals": []}),
        ("windows", {"episode": {"grouping_window_ms": 4000}}),
        ("catalog/single-occupancy", {"single_occupancy": True}),
    ):
        issued.append(
            _ok(
                routes.request("PUT", f"/zones/{zone}/{path}", admin, {**body, "reason_es": REASON})
            )
        )
    signed = routes.signer.calls - calls_before
    stored = [routes.versions(zone)[i]["envelope"] for i in range(len(issued))]

    for view, raw in zip(issued, stored, strict=True):
        version = view["catalog_version"]
        detail = _ok(routes.request("GET", f"/zones/{zone}/catalog/versions/{version}", admin))
        # El sobre de la consulta es el emitido: mismo contenido, mismo hash canónico, misma firma.
        emitted = json.loads(raw)
        assert detail["envelope"] == emitted
        assert canonical_sha256(detail["envelope"]) == canonical_sha256(emitted)
        assert detail["catalog"] == view["catalog"] == emitted["payload"]
        payload = verify(
            detail["envelope"], routes.signer.keyset(), "catalog", routes.signer.world.clock
        )
        assert payload["version"] == version
    # Consultar nunca vuelve a firmar (NFR-GOB-10).
    assert routes.signer.calls - calls_before == signed
    history = _ok(routes.request("GET", f"/zones/{zone}/catalog/versions", admin))
    assert [v["catalog_version"] for v in history["versions"]] == [5, 4, 3, 2, 1]


def test_h44_a_changed_threshold_shows_reason_author_and_date_and_leaves_regression_pending(
    routes: CatalogRoutes,
) -> None:
    site = routes.site()
    ((plant, zone),) = site.zones()
    routes.productive(site, plant, zone)
    admin_id, admin = routes.person(site)
    routes.configure(admin, zone)
    reason = "Ajuste del umbral de publicación tras la revisión mensual"

    changed = _ok(
        routes.request(
            "PUT",
            f"/zones/{zone}/thresholds",
            admin,
            {"review": 0.45, "publication": 0.9, "reason_es": reason},
        )
    )

    (entry, _) = _ok(routes.request("GET", f"/zones/{zone}/catalog/versions", admin))["versions"]
    assert entry["changed_fields"] == ["thresholds"]
    assert (entry["reason_es"], entry["issued_by"]) == (reason, str(admin_id))
    assert entry["role_in_use"] == "administrator"
    assert entry["issued_at"] == changed["issued_at"]
    assert changed["catalog"]["thresholds"] == {"review": 0.45, "publication": 0.9}
    regression = _ok(routes.request("GET", f"/zones/{zone}/regression", admin))
    assert regression["state"] == "pending"
    assert (regression["cause"], regression["catalog_version"]) == ("catalog_change", 2)
    assert regression["affected_row_ids"] == "all"
    # La marca no bloquea nada: la zona sigue productiva.
    assert routes.resulting_mode(zone) == "productive"
    # Sin motivo no hay cambio de umbral (G-10).
    refused = routes.request(
        "PUT", f"/zones/{zone}/thresholds", admin, {"review": 0.4, "publication": 0.9}
    )
    assert refused.status_code == 400 and refused.json()["code"] == "invalid_request"
    assert len(routes.versions(zone)) == 2


def test_h45_a_zone_is_configured_end_to_end_with_the_parameter_routes_only(
    routes: CatalogRoutes,
) -> None:
    site = routes.site()
    ((_, zone),) = site.zones()
    admin = routes.member(site)
    cameras = [camera(0, "E"), camera(1, "E"), camera(2, "E")]

    first = _ok(
        routes.request(
            "POST",
            f"/zones/{zone}/standards",
            admin,
            first_standard_body(
                cameras=cameras[:2],
                minimum_coverage={
                    "required_count": 1,
                    "required_camera_ids": [cameras[0]["camera_id"]],
                },
            ),
        ),
        201,
    )
    steps: list[tuple[str, str, dict[str, Any]]] = [
        ("PUT", "cameras", {"cameras": cameras}),
        (
            "PUT",
            "minimum-coverage",
            {
                "required_count": 2,
                "required_camera_ids": [cameras[0]["camera_id"], cameras[2]["camera_id"]],
            },
        ),
        (
            "PUT",
            "signals",
            {
                "signals": [
                    {
                        "signal_id": str(uuid.UUID(int=50_000, version=4)),
                        "code": "SG-E1",
                        "role": "guard",
                        "asserted_level": "low",
                        "source": {"reader": "plc-2", "channel": 7},
                        "description_es": "Resguardo lateral",
                    }
                ]
            },
        ),
        ("PUT", "thresholds", {"review": 0.5, "publication": 0.95}),
        (
            "PUT",
            "windows",
            {"clip_window": {"pre_seconds": 12, "post_seconds": 18}, "episode": {}},
        ),
        (
            "PUT",
            "catalog/single-occupancy",
            {"single_occupancy": True, "aggregation_window_minutes": 120},
        ),
        (
            "POST",
            "standards",
            standard_body(
                "guard_bypass",
                {"all_of": [PRESENCE, GUARD_ON], "min_duration_ms": 0},
                "Resguardo lateral cerrado con persona dentro",
            ),
        ),
    ]
    for method, path, body in steps:
        status = 201 if method == "POST" else 200
        payload = body if "reason_es" in body else {**body, "reason_es": REASON}
        _ok(routes.request(method, f"/zones/{zone}/{path}", admin, payload), status)
    retired = first["catalog"]["standards"][0]["standard_id"]

    _ok(
        routes.request(
            "POST",
            f"/zones/{zone}/standards/{retired}/retirement",
            admin,
            {"reason_es": REASON, "effective_from": format_timestamp(routes.authz.now())},
        ),
        201,
    )

    final = _ok(routes.request("GET", f"/zones/{zone}/catalog", admin))
    catalog = final["catalog"]
    assert final["catalog_version"] == 1 + len(steps) + 1
    assert [c["camera_id"] for c in catalog["cameras"]] == [c["camera_id"] for c in cameras]
    assert catalog["minimum_coverage"]["required_count"] == 2
    assert [s["code"] for s in catalog["signals"]] == ["SG-E1"]
    assert catalog["thresholds"] == {"review": 0.5, "publication": 0.95}
    assert catalog["clip_window"] == {"pre_seconds": 12, "post_seconds": 18}
    assert catalog["episode"]["grouping_window_ms"] == 3000  # D-11
    assert [s["family"] for s in catalog["standards"]] == ["guard_bypass"]
    assert (final["single_occupancy"], final["aggregation_window_minutes"]) == (True, 120)
    # Cada paso es una versión firmada con su motivo; las cámaras nuevas, en la proyección.
    assert [r["catalog_version"] for r in routes.versions(zone)] == list(
        range(1, final["catalog_version"] + 1)
    )
    streams = {
        row["stream_reference"]
        for row in routes.fetch(
            "SELECT stream_reference FROM catalog.zone_camera WHERE zone_id = $1", zone
        )
    }
    assert streams == {c["stream_reference"] for c in cameras}
