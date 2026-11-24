"""Actualización desde ``18981b6`` (contrato v1.5.0) al contrato v1.6.0 (TASK-593; BR-NUC-52).

v1.6.0 amplía por adición tres modelos que el expediente guarda tal cual: ``Finding`` y
``DetectionForReview`` (``process_context`` opcional y ``clips`` de 0 a 2, A-128 y A-129) y el
``ZoneCatalog`` del sobre de ``catalog_version_published`` (``process_context``, con la descripción
de cada variable como texto libre). Sin subir ``schema_version`` la plataforma de integración no
arrancaría sobre su base (``RegistryStartupError``, como en VIG-457).

Estas pruebas siembran ``InMemoryRecordTypeStore`` con la tabla que persistía ``18981b6``
(``tests/fixtures/record_types/ledger_record_type_18981b6.json``) y comprueban, sin base de datos:

- ``synchronize`` del registro de producción arranca, sella y solo sube los tres tipos cambiados
  (``catalog_version_published`` a la 4, con TASK-594; ``finding_received`` y
  ``detection_for_review_received`` a la 2); el resto de filas no cambia;
- cada versión que ``18981b6`` persistió sigue registrada con **su** esquema (congeladas en
  ``record_types_frozen_v1_5``) y la nueva solo la amplía;
- un hallazgo con ``process_context`` o sin clips solo valida con la versión 2, y uno de v1.5.0 con
  las dos; el sobre con variables de proceso solo con la 3.

La mutación «v1.5 desde el contrato vigente» (registrar las versiones anteriores con los modelos
actuales) vuelve a impedir el arranque.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from typing import Any, Final

import pytest
from pydantic import ValidationError
from vigia_contracts.conformance.checks._core import draw_seeded
from vigia_contracts.conformance.generators import finding, with_process_context, zone_catalog

from tests.record_type_upgrade_support import (
    V1_5_COMMIT,
    V1_6_UPGRADED_TYPES,
    v1_5_document,
    v1_5_rows,
)
from vigia_platform.catalog.record_types_frozen_v1_5 import CatalogVersionPublishedV2
from vigia_platform.fleet.record_types_frozen_v1_5 import DetectionForReviewV1, FindingV1
from vigia_platform.ledger.registry import (
    InMemoryRecordTypeStore,
    RecordTypeRegistry,
    RegistryStartupError,
)
from vigia_platform.ledger.schema_rules import compatibility_problems, normalized_schema
from vigia_platform.shared.runtime.units import record_type_registry, registered_units

ROWS: Final = v1_5_rows()
SEED: Final = 20261123
RECEIPT: Final = {
    "platform_record_id": "0190f3a1-7b2c-7d4e-8f90-1a2b3c4d5e6f",
    "received_at": "2026-11-23T09:30:00.000Z",
    "status": "accepted",
}


def _registry() -> RecordTypeRegistry:
    return record_type_registry(registered_units())


def test_the_fixture_is_the_table_that_18981b6_persisted() -> None:
    document = v1_5_document()
    assert document["generated_from"]["commit"] == V1_5_COMMIT
    assert document["generated_from"]["contract"] == "vigia-contracts v1.5.0"
    assert {name: ROWS[name].schema_version for name in V1_6_UPGRADED_TYPES} == {
        "catalog_version_published": 2,
        "finding_received": 1,
        "detection_for_review_received": 1,
    }


def test_synchronizing_over_the_18981b6_table_starts_and_only_raises_the_changed_types() -> None:
    registry = _registry()
    store = InMemoryRecordTypeStore(ROWS)
    asyncio.run(registry.synchronize(store))  # sin RegistryStartupError
    assert registry.sealed
    after = asyncio.run(store.load())
    changed = {name: row.schema_version for name, row in after.items() if row != ROWS.get(name)}
    assert changed == V1_6_UPGRADED_TYPES
    for name in changed:
        assert after[name] == registry.get(name).to_persisted()
    again = InMemoryRecordTypeStore(after)
    asyncio.run(_registry().synchronize(again))
    assert asyncio.run(again.load()) == after


def test_every_version_that_18981b6_persisted_keeps_its_exact_schema() -> None:
    registry = _registry()
    for name, row in sorted(ROWS.items()):
        compiled = registry.get(name, schema_version=row.schema_version)
        assert normalized_schema(compiled.content_schema) == normalized_schema(
            row.content_schema
        ), name
    frozen = {
        ("catalog_version_published", 2): CatalogVersionPublishedV2,
        ("finding_received", 1): FindingV1,
        ("detection_for_review_received", 1): DetectionForReviewV1,
    }
    for (name, version), model in frozen.items():
        assert registry.get(name, schema_version=version).definition.content_model is model


def test_the_new_versions_only_widen_and_are_the_ones_written() -> None:
    registry = _registry()
    for name, version in V1_6_UPGRADED_TYPES.items():
        latest = registry.get(name)
        assert latest.schema_version == version, name
        old = ROWS[name].content_schema
        assert normalized_schema(old) != normalized_schema(latest.content_schema), name
        assert compatibility_problems(old, latest.content_schema) == [], name


def _finding() -> dict[str, Any]:
    """Un hallazgo aceptado del kit v1.6.0 con valores de proceso y clips."""
    catalog = draw_seeded(with_process_context(draw_seeded(zone_catalog(), SEED), minimum=1), SEED)
    document: dict[str, Any] = draw_seeded(finding(catalog, process_context=True), SEED)
    return {**document, "receipt": RECEIPT}


def test_a_v1_5_finding_validates_with_both_versions_and_a_v1_6_one_only_with_the_second() -> None:
    registry = _registry()
    v1, v2 = (registry.get("finding_received", schema_version=n) for n in (1, 2))
    current = _finding()
    previous = {key: value for key, value in current.items() if key != "process_context"}
    v1.validate_json(json.dumps(previous))
    v2.validate_json(json.dumps(previous))
    v2.validate_json(json.dumps(current))
    with pytest.raises(ValidationError):
        v1.validate_json(json.dumps(current))
    without_clips = copy.deepcopy(previous)
    for camera in without_clips["cameras"]:
        camera["clips"] = []
    v2.validate_json(json.dumps(without_clips))
    with pytest.raises(ValidationError):
        v1.validate_json(json.dumps(without_clips))


def test_the_v2_catalog_envelope_has_no_process_context() -> None:
    schema = CatalogVersionPublishedV2.model_json_schema()
    assert "process_context" not in schema["$defs"]["Payload"]["properties"]
    latest = _registry().get("catalog_version_published").content_schema
    assert "process_context" in latest["$defs"]["Payload"]["properties"]
    assert (
        "/envelope/payload/process_context/variables[*]/description_es"
        in _registry().get("catalog_version_published").definition.free_text_paths
    )


def test_registering_the_previous_versions_with_the_current_models_prevents_startup() -> None:
    """La mutación con nombre «v1.5 desde el contrato vigente»."""
    registry = RecordTypeRegistry()
    fresh = _registry()
    for compiled in fresh.all_versions():
        definition = compiled.definition
        name = definition.record_type
        if name in V1_6_UPGRADED_TYPES:
            if definition.schema_version > ROWS[name].schema_version:
                continue  # las posteriores (también la 4 del catálogo, TASK-594)
            if definition.schema_version == ROWS[name].schema_version:
                latest = fresh.get(name).definition
                definition = replace(
                    definition,
                    content_model=latest.content_model,
                    free_text_paths=latest.free_text_paths,
                )
        registry.register(definition)
    with pytest.raises(RegistryStartupError) as failure:
        asyncio.run(registry.synchronize(InMemoryRecordTypeStore(ROWS)))
    problems = failure.value.problems
    for name in V1_6_UPGRADED_TYPES:
        version = ROWS[name].schema_version
        assert (
            f"{name}: el esquema de la versión {version} cambió sin subir schema_version"
            in problems
        ), problems
    assert not registry.sealed
