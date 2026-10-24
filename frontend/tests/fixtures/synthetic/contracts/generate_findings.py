"""Genera ``findings.json``: hallazgos sintéticos del kit de conformidad de ``vigia-contracts``.

Los produce ``vigia_contracts.conformance.generators.finding`` (v1.1.0, el mismo commit que
``@vigia/contracts`` en ``package-lock.json``) con una semilla fija, así que el archivo es
reproducible. Solo datos generados: ninguna persona, foto ni registro real.

Desde ``backend/`` (su entorno ya trae el kit fijado en ``uv.lock``)::

    uv run --frozen python ../frontend/tests/fixtures/synthetic/contracts/generate_findings.py
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import finding, zone_catalog
from vigia_contracts.conformance.generators._primitives import JsonObject

SEED = 20261023
COUNT = 5
OUTPUT = Path(__file__).with_name("findings.json")


def generate() -> list[JsonObject]:
    collected: list[JsonObject] = []

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
        collected.append(data.draw(finding(catalog, accepted=True)))

    draw()
    return collected[:COUNT]


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(generate(), ensure_ascii=False, indent=2) + "\n", "utf-8")
