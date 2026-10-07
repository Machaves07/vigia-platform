"""G-6 · Administrador que intenta habilitar una familia de productividad (business-rules §12; P7).

**Qué intenta**: usar el catálogo para declarar un indicador de desempeño (tiempo de permanencia de
una persona, cadencia, «mapa de calor») como si fuera un estándar de seguridad.

**Qué lo detiene**:

- BR-GOB-13: las tres preguntas de admisión (estándar escrito, remedio de ingeniería o de proceso,
  dato que pierde sentido por persona); una negativa rechaza nombrando el ``failed_criterion``
  (``invalid_request`` con ``catalog_admission_rejected``);
- BR-GOB-15 (con BR-GOB-13): toda evaluación, también la rechazada, queda en la cadena de la planta
  como ``standard_admission_test``; un rechazo no se sobrescribe: reintentar es otra evaluación;
- BR-GOB-16: declarar un estándar de una familia no admitida en la planta es
  ``catalog_family_not_admitted`` y no publica nada;
- BR-GOB-17: ``family`` es la lista cerrada del contrato (``coexistence``, ``guard_bypass``,
  ``dwell``, ``startup_transition``): ningún valor nuevo entra por la ruta;
- BR-GOB-18: la prohibición no tiene clave de permiso: no hay clave ni ruta de productividad,
  desempeño o mapa de calor que alguien pudiera conceder.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.gob_platform_support import REASON, GobPlatform, Onboarding, detail_of
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import iter_declared_routes

pytestmark = pytest.mark.integration

PRODUCTIVITY = re.compile(r"productiv|performance|desempe|heat|calor|ranking|cadence|cycle", re.I)
DWELL_STANDARD: dict[str, Any] = {
    "family": "dwell",
    "title_es": "Permanencia de cada operario en la celda",
    "declared_text": "Medir cuánto permanece cada operario frente a la prensa durante el turno.",
    "predicate": {"all_of": [{"presence": True}], "min_duration_ms": 60_000},
    "reason_es": REASON,
}


def _admission(gob: GobPlatform, cookie: Any, plant: Any, family: str, **answers: bool) -> Any:
    values = {"standard": True, "remedy": True, "subject": True, **answers}
    return gob.call(
        "POST",
        f"/plants/{plant}/admissions",
        cookie=cookie,
        json_body={"family": family, "answers": values},
    )


def test_g06_a_productivity_family_is_rejected_recorded_and_never_declared(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)
    plant = zone.plant_id
    versions = gob.fetch(
        "SELECT count(*) AS n FROM catalog.zone_catalog_version WHERE zone_id = $1", zone.zone_id
    )[0]["n"]

    # El remedio sería entrenar o sancionar a la persona, y el dato es de la persona.
    for _ in range(2):
        rejected = _admission(gob, zone.admin, plant, "dwell", remedy=False, subject=False)
        assert detail_of(rejected) == (400, "invalid_request", "catalog_admission_rejected")
    rows = gob.fetch(
        "SELECT admission_id, family, result, failed_criterion, ledger_record_id"
        " FROM catalog.family_admission WHERE plant_id = $1 AND family = 'dwell'"
        " ORDER BY evaluated_at",
        plant,
    )
    assert [(r["result"], r["failed_criterion"]) for r in rows] == [("rejected", "remedy")] * 2
    assert rows[0]["admission_id"] != rows[1]["admission_id"]  # reintentar es otra evaluación
    recorded = [
        content
        for content in gob.contents(zone.organization_id, "standard_admission_test")
        if content["family"] == "dwell"
    ]
    assert [(c["result"], c["failed_criterion"]) for c in recorded] == [("rejected", "remedy")] * 2

    # BR-GOB-16: sin admisión, el estándar no se declara y no se publica ninguna versión.
    declared = gob.call(
        "POST", f"/zones/{zone.zone_id}/standards", cookie=zone.admin, json_body=DWELL_STANDARD
    )
    assert detail_of(declared) == (409, "conflict", "catalog_family_not_admitted"), declared.text
    after = gob.fetch(
        "SELECT count(*) AS n FROM catalog.zone_catalog_version WHERE zone_id = $1", zone.zone_id
    )[0]["n"]
    assert after == versions


@pytest.mark.parametrize("family", ["productivity", "cycle_time", "heat_map", "DWELL"])
def test_g06_families_outside_the_closed_list_are_invalid_and_write_nothing(
    gob: GobPlatform, family: str
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)
    before = len(gob.records(zone.organization_id, "standard_admission_test"))
    response = _admission(gob, zone.admin, zone.plant_id, family)
    assert response.status_code in (400, 422), response.text
    assert response.json()["code"] == "invalid_request"
    assert len(gob.records(zone.organization_id, "standard_admission_test")) == before
    # Ni bajo la concesión del instalador del proveedor.
    under = flow.as_installer(
        zone,
        "POST",
        f"/plants/{zone.plant_id}/admissions",
        {"family": family, "answers": {"standard": True, "remedy": True, "subject": True}},
    )
    assert under.status_code in (400, 404, 422), under.text
    assert len(gob.records(zone.organization_id, "standard_admission_test")) == before


def test_g06_no_permission_key_or_route_can_grant_a_productivity_metric(
    gob: GobPlatform,
) -> None:
    """BR-GOB-18: la prohibición no se concede, porque no existe nada que conceder."""
    keys = [key.value for key in PermissionKey]
    assert [key for key in keys if PRODUCTIVITY.search(key)] == []
    paths = [route.path for route in iter_declared_routes(gob.app.routes)]
    assert len(paths) > 50
    assert [path for path in paths if PRODUCTIVITY.search(path)] == []
