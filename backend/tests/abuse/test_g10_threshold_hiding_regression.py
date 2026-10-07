"""G-10 · Cambio de umbral sin motivo para ocultar una regresión (business-rules §12; H-44).

**Qué intenta**: ajustar el umbral de publicación del catálogo de una zona productiva sin dejar
rastro, o marcando él mismo que el cambio «no afecta» al acta, para no reejecutar la matriz.

**Qué lo detiene**:

- BR-GOB-03: toda versión declara ``reason_es`` obligatorio, autor, rol y ``changed_fields``; sin
  motivo, ``invalid_request`` y no se publica nada;
- BR-GOB-51: un cambio de umbrales dispara regresión;
- BR-GOB-56: la marca la calcula el **sistema** desde lo que cambió, nunca el autor: un cuerpo que
  intenta fijar ``changed_fields``, ``skip_regression`` o ``regression_marked`` es
  ``invalid_request`` (lista cerrada de campos) y no escribe nada. Con motivo, la versión nueva
  lleva ``changed_fields = [thresholds]``, su autor y su fecha, y la zona queda con la regresión
  ``pending`` sin dejar de ser ``productive`` (BR-GOB-53).

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok

pytestmark = pytest.mark.integration

REASON = "Se ajusta el umbral tras la recalibración de la cámara frontal"
LOWER: dict[str, Any] = {"review": 0.5, "publication": 0.95}


def _versions(gob: GobPlatform, zone: GobZone) -> list[Any]:
    return gob.fetch(
        "SELECT catalog_version, issued_by, reason_es, changed_fields, issued_at"
        " FROM catalog.zone_catalog_version WHERE zone_id = $1 ORDER BY catalog_version",
        zone.zone_id,
    )


def _regression(gob: GobPlatform, zone: GobZone) -> dict[str, Any]:
    body: dict[str, Any] = ok(
        gob.call("GET", f"/zones/{zone.zone_id}/regression", cookie=zone.admin)
    )
    return body


@pytest.mark.parametrize(
    "tamper",
    [
        {},
        {"reason_es": REASON, "changed_fields": ["single_occupancy"]},
        {"reason_es": REASON, "skip_regression": True},
        {"reason_es": REASON, "regression_marked": False},
        {"reason_es": REASON, "affected_row_ids": []},
    ],
    ids=["sin_motivo", "changed_fields", "skip_regression", "regression_marked", "filas"],
)
def test_g10_no_threshold_change_without_reason_or_with_a_self_declared_mark(
    gob: GobPlatform, tamper: dict[str, Any]
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    before = _versions(gob, zone)
    response = gob.call(
        "PUT", f"/zones/{zone.zone_id}/thresholds", cookie=zone.admin, json_body=LOWER | tamper
    )
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"
    assert _versions(gob, zone) == before
    assert _regression(gob, zone)["state"] == "current"
    assert gob.records(zone.organization_id, "walk_test_regression_marked") == []


def test_g10_with_reason_the_system_marks_the_regression_and_the_zone_keeps_working(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    changed = gob.call(
        "PUT",
        f"/zones/{zone.zone_id}/thresholds",
        cookie=zone.admin,
        json_body=LOWER | {"reason_es": REASON},
    )
    assert changed.status_code == 200, changed.text
    latest = _versions(gob, zone)[-1]
    assert latest["catalog_version"] == 2
    assert list(latest["changed_fields"]) == ["thresholds"]  # calculado, no declarado
    assert (latest["issued_by"], latest["reason_es"]) == (zone.admin_id, REASON)
    assert latest["issued_at"] is not None
    regression = _regression(gob, zone)
    assert regression["state"] == "pending", regression
    assert len(gob.records(zone.organization_id, "walk_test_regression_marked")) == 1
    # La regresión no bloquea: la zona sigue productiva y los hallazgos entran (BR-GOB-53).
    assert flow.mode(zone) == "productive"
    assert flow.post_finding(zone, flow.finding(zone)).status_code == 200
