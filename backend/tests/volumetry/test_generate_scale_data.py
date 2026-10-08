"""La volumetría en miniatura (``--scale smoke``) corre de punta a punta (TASK-142, VIG-91; U-03 en
TASK-233).

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

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.volumetry.generate_scale_data import GOB_SCALES, SCALES, run, run_gob

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


GOB_ROUTES = {
    f"gob_console_{name}"
    for name in (
        "fleet_nodes",
        "fleet_node_detail",
        "walk_test_current",
        "zone_catalog",
        "catalog_versions",
        "zone_gates",
        "zone_transparency",
        "commissioning_record",
        "documents",
    )
}
GOB_PORTS = {
    f"gob_ports_{name}"
    for name in (
        "floor_select_1",
        "current_catalog",
        "catalog_at",
        "standard_at",
        "catalog_history",
        "single_occupancy",
        "single_occupancy_many",
        "standards_at_many",
        "state",
        "states_by_plant",
        "state_at",
        "gate_history",
        "plant_policy",
        "current_agreement",
    )
}


def test_smoke_scale_u03_generates_measures_and_reports(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> None:
    """La parte de U-03 (TASK-233) en miniatura: el núcleo por las rutas reales, la historia en
    masa, las rutas de NFR-GOB-03 y los puertos de NFR-GOB-04, ``gate_history`` de 366 días desde
    el primer mes y las métricas de presupuesto de NFR-GOB-07."""
    scale = SCALES["smoke"]
    gob = GOB_SCALES["smoke"]
    rates = gob.rates
    section = run_gob(postgres_endpoint, localstack_endpoint, scale, seed=7, workers=2)

    written = section["written"]
    zones = gob.extra_plants * gob.zones_per_plant
    nodes = 3 + zones + gob.other_active_nodes
    per_node = rates.heartbeat_days * 86_400 // rates.heartbeat_seconds
    assert written["heartbeat_history"] == nodes * per_node
    assert written["clip_upload_grant"] == nodes * rates.grant_days * rates.grants_per_zone_day
    assert written["enrollment_attempt"] == nodes * rates.history_months * (
        rates.attempts_per_node_month
    )
    assert written["fleet_alarm"] == nodes * rates.history_months * rates.alarms_per_node_month
    assert written["catalog_versions"] == zones * rates.history_months * (
        rates.catalog_versions_per_month
    )
    # El año del expediente lleva las transiciones de comunicación de cada nodo generado.
    assert written["node_communication_state_changed"] == zones * (
        2 * gob.ledger_rates.communication_pairs * gob.ledger_days + 1
    )
    budget = section["nfr_gob_07"]
    assert budget["fleet_heartbeat_rows_per_day"] == nodes * per_node // rates.heartbeat_days
    assert budget["fleet_heartbeat_rows_per_day_at_100_nodes"] == 100 * 86_400 // (
        rates.heartbeat_seconds
    )
    assert budget["fleet_upload_grants_per_day_at_300_zones"] == 300 * rates.grants_per_zone_day
    assert budget["catalog_versions_per_day"] > 0
    assert {timing["name"] for timing in section["nfr_gob_03"]} == GOB_ROUTES
    assert {timing["name"] for timing in section["nfr_gob_04"]} == GOB_PORTS
    for timing in section["nfr_gob_03"] + section["nfr_gob_04"]:
        assert 0 < timing["median_ms"] <= timing["p95_ms"]
        if timing["objective_ms"] is not None:
            assert timing["objective_met"] is (timing["p95_ms"] <= timing["objective_ms"])
    history = section["gate_history_366_days"]
    assert history["complete"] is True
    assert history["intervals"] == {"mounting": rates.history_months, "usage": rates.history_months}
