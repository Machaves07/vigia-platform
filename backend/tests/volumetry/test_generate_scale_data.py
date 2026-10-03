"""La volumetría en miniatura (``--scale smoke``) corre de punta a punta (TASK-142, VIG-91).

``generate_scale_data.py --scale target`` solo corre en ``nightly`` y a mano; esta prueba de
integración, con la escala ``smoke`` contra el PostgreSQL de la sesión, impide que el script se
pudra entre medias: genera con el disparador real y la marca histórica (que se restaura, o el
script falla), cuenta lo generado por tipo, verifica una cadena de planta con el motor de
verificación de la plataforma (íntegra, o el script falla), mide cada operación de NFR-NUC-01 y
escribe el informe con las métricas de NFR-NUC-03.

Solo datos generados.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.volumetry.generate_scale_data import SCALES, run

pytestmark = pytest.mark.integration

EXPECTED_OPERATIONS = {
    "ledger_list_200_plant_scope",
    "ledger_list_200_plant_scope_mid_year",
    "ledger_list_200_organization_scope",
    "coverage_timeline_31d_hot_zone",
    "coverage_timeline_31d_zone",
    "coverage_state_at_hot_zone",
    "coverage_state_at_zone",
    "ledger_write",
}


def test_smoke_scale_generates_measures_and_reports(
    postgres_endpoint: PostgresEndpoint, tmp_path: Path
) -> None:
    scale = SCALES["smoke"]
    out = tmp_path / "volumetria.json"
    report = run(postgres_endpoint, scale, seed=7, workers=2, out=out)

    assert json.loads(out.read_text(encoding="utf-8")) == report
    rates = scale.rates
    normal = rates.findings + 2 * rates.observability_pairs + rates.classifications
    hot = rates.findings + 2 * rates.hot_zone_pairs + rates.classifications
    largest_zones = scale.largest_plants * scale.largest_zones_per_plant
    other_zones = sum(scale.other_plants) * scale.other_zones_per_plant
    bootstrap = 2 * (largest_zones + other_zones)  # compuerta y comunicación de cada zona
    expected_records = (
        (largest_zones - 1) * normal * scale.days
        + hot * scale.days
        + other_zones * normal * scale.other_days
        + bootstrap
    )
    written = report["written"]
    assert sum(count for name, count in written.items() if name != "audit_entry") == (
        expected_records
    )
    assert written["audit_entry"] == scale.audit_per_day * scale.days

    growth = report["nfr_nuc_03"]
    assert growth["ledger_bytes_per_day"] > 0
    assert growth["audit_entries_per_day"] == scale.audit_per_day
    assert {timing["name"] for timing in report["nfr_nuc_01"]} == EXPECTED_OPERATIONS
    for timing in report["nfr_nuc_01"]:
        assert 0 < timing["median_ms"] <= timing["p95_ms"]
        assert timing["objective_met"] is (timing["p95_ms"] <= timing["objective_ms"])
    # La cadena verificada es la de la segunda planta: su año, su arranque y las escrituras del
    # banco de escritura (2 de calentamiento y las rondas).
    assert report["verification"]["records"] == (
        scale.largest_zones_per_plant * (normal * scale.days + 2) + 2 + scale.rounds
    )
