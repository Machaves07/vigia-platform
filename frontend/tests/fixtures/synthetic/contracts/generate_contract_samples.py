"""Genera ``contract_samples.json``: ``Finding`` y ``Heartbeat`` sintéticos para PR-APP-19 (VIG-280).

Los produce el kit de conformidad de ``vigia-contracts`` (``vigia_contracts.conformance.generators``,
v1.1.0, el mismo commit que ``@vigia/contracts`` en ``package-lock.json``) con una semilla fija, así
que el archivo es reproducible: son objetos generados desde el esquema del contrato, el oráculo de
``tests/properties/pr_app_19_contract_validation.test.ts``. Solo datos generados: ninguna persona,
foto ni registro real.

Desde ``backend/`` (su entorno ya trae el kit fijado en ``uv.lock``)::

    uv run --frozen python ../frontend/tests/fixtures/synthetic/contracts/generate_contract_samples.py
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import finding, heartbeat, zone_catalog
from vigia_contracts.conformance.generators._primitives import JsonObject

SEED = 20261023
COUNT = 12
OUTPUT = Path(__file__).with_name("contract_samples.json")


def generate() -> dict[str, list[JsonObject]]:
    findings: list[JsonObject] = []
    heartbeats: list[JsonObject] = []

    @seed(SEED)
    @settings(
        max_examples=COUNT,
        database=None,
        derandomize=False,
        suppress_health_check=list(HealthCheck),
    )
    @given(st.data())
    def draw(data: st.DataObject) -> None:
        catalog = data.draw(zone_catalog())
        findings.append(data.draw(finding(catalog, accepted=True)))
        heartbeats.append(data.draw(heartbeat(catalog)))

    draw()
    return {"findings": findings[:COUNT], "heartbeats": heartbeats[:COUNT]}


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", "utf-8")
