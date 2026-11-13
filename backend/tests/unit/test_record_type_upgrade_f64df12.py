"""Actualización desde ``f64df12``: el registro actual arranca sobre su tabla (VIG-457, BR-NUC-52).

La plataforma de integración (A-107) corría ``f64df12`` (contrato v1.1.0). Al pasar a ``main``
(v1.4.0 y v1.5.0) la API y ``vigia-admin`` no arrancaban: ``RegistryStartupError`` porque el
esquema de la versión 1 de ``catalog_version_published`` y de ``ingest_rejected`` cambió sin subir
``schema_version``. Con una base nueva no pasaba nada, así que el CI no lo veía.

Estas pruebas siembran ``InMemoryRecordTypeStore`` con la tabla que persistía ``f64df12``
(``tests/fixtures/record_types/ledger_record_type_f64df12.json``) y comprueban, sin base de datos:

- ``synchronize`` del registro de producción no lanza nada, sella y solo sube a la versión 2 los
  dos tipos cambiados; el resto de filas no cambia;
- cada versión que ``f64df12`` persistió sigue registrada con **su** esquema (la 1 congelada);
- la versión 2 solo amplía la 1 (``compatibility_problems``) y es la que escriben los escritores;
- un contenido histórico v1 sigue validando con su versión, y uno que solo admite v1.5.0 no.

La prueba de integración ``tests/integration/test_record_type_upgrade_f64df12.py`` repite la
actualización contra PostgreSQL 16 con las migraciones reales y un registro histórico escrito.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from tests.record_type_upgrade_support import (
    F64DF12_COMMIT,
    UPGRADED_TYPES,
    f64df12_document,
    f64df12_rows,
)
from vigia_platform.catalog.record_types import CatalogVersionPublished
from vigia_platform.catalog.record_types_frozen import CatalogVersionPublishedV1
from vigia_platform.fleet.record_types import IngestRejected
from vigia_platform.fleet.record_types_frozen import IngestRejectedV1
from vigia_platform.ledger.registry import (
    InMemoryRecordTypeStore,
    RecordTypeRegistry,
    RegistryStartupError,
)
from vigia_platform.ledger.schema_rules import compatibility_problems, normalized_schema
from vigia_platform.shared.runtime.units import record_type_registry, registered_units

ROWS = f64df12_rows()


def _registry() -> RecordTypeRegistry:
    return record_type_registry(registered_units())


def test_the_fixture_is_the_table_that_f64df12_persisted() -> None:
    document = f64df12_document()
    assert document["generated_from"]["commit"] == F64DF12_COMMIT
    assert document["generated_from"]["contract"] == "vigia-contracts v1.1.0"
    assert len(ROWS) == 58
    assert {name: ROWS[name].schema_version for name in UPGRADED_TYPES} == {
        "catalog_version_published": 1,
        "ingest_rejected": 1,
    }


def test_synchronizing_over_the_f64df12_table_starts_and_only_raises_the_changed_types() -> None:
    registry = _registry()
    store = InMemoryRecordTypeStore(ROWS)
    asyncio.run(registry.synchronize(store))  # sin RegistryStartupError
    assert registry.sealed
    after = asyncio.run(store.load())
    changed = {name: row.schema_version for name, row in after.items() if row != ROWS.get(name)}
    assert changed == {"catalog_version_published": 2, "ingest_rejected": 2}
    for name in changed:
        assert after[name] == registry.get(name).to_persisted()
    # Volver a arrancar sobre la tabla ya actualizada tampoco cambia nada.
    again = InMemoryRecordTypeStore(after)
    asyncio.run(_registry().synchronize(again))
    assert asyncio.run(again.load()) == after


def test_every_version_that_f64df12_persisted_keeps_its_exact_schema() -> None:
    registry = _registry()
    for name, row in sorted(ROWS.items()):
        compiled = registry.get(name, schema_version=row.schema_version)
        assert normalized_schema(compiled.content_schema) == normalized_schema(
            row.content_schema
        ), name


def test_only_the_changed_types_have_a_new_version_and_it_only_widens() -> None:
    registry = _registry()
    raised = {
        name: registry.get(name).schema_version
        for name, row in ROWS.items()
        if registry.get(name).schema_version != row.schema_version
    }
    assert raised == UPGRADED_TYPES
    for name in UPGRADED_TYPES:
        old, new = ROWS[name].content_schema, registry.get(name).content_schema
        assert normalized_schema(old) != normalized_schema(new), name
        assert compatibility_problems(old, new) == [], name


def test_writers_write_version_2_with_the_current_models() -> None:
    registry = _registry()
    assert registry.get("catalog_version_published").schema_version == 2
    assert registry.get("catalog_version_published").definition.content_model is (
        CatalogVersionPublished
    )
    assert registry.get("catalog_version_published", schema_version=1).definition.content_model is (
        CatalogVersionPublishedV1
    )
    assert registry.get("ingest_rejected").schema_version == 2
    assert registry.get("ingest_rejected").definition.content_model is IngestRejected
    assert registry.get("ingest_rejected", schema_version=1).definition.content_model is (
        IngestRejectedV1
    )


_INGEST_REJECTED = {
    "node_id": "5b0b1c7e-2f43-4c1a-9d65-0d3c7f1e8a21",
    "zone_id": "0f6a3d52-8e1b-4b7c-a5d9-3e2c1b0a9f87",
    "code": "zone_gate_not_approved",
    "correlation_id": "6d4e2a10-9c3b-4f8e-b1a7-2c5d8e0f3b64",
    "received_at": "2026-11-12T09:30:00.000Z",
}


def test_a_historical_v1_ingest_rejected_still_validates_with_its_version() -> None:
    registry = _registry()
    historical = json.dumps({**_INGEST_REJECTED, "record_kind": "finding"})
    registry.get("ingest_rejected", schema_version=1).validate_json(historical)
    registry.get("ingest_rejected", schema_version=2).validate_json(historical)


def test_the_v1_list_of_record_kinds_is_the_one_of_v1_1_0() -> None:
    registry = _registry()
    episode_photo = json.dumps({**_INGEST_REJECTED, "record_kind": "episode_photo"})
    with pytest.raises(ValidationError):
        registry.get("ingest_rejected", schema_version=1).validate_json(episode_photo)
    registry.get("ingest_rejected", schema_version=2).validate_json(episode_photo)


def test_reintroducing_the_change_without_a_new_version_prevents_startup() -> None:
    """La mutación con nombre «v1 desde el contrato vigente»: registrar ``ingest_rejected`` y
    ``catalog_version_published`` solo con los modelos actuales en la versión 1 (lo que hacía
    ``main`` antes de VIG-457) vuelve a impedir el arranque, con los dos problemas del informe."""
    registry = RecordTypeRegistry()
    for compiled in _registry().all_versions():
        definition = compiled.definition
        if definition.record_type in UPGRADED_TYPES:
            if definition.schema_version != 1:
                continue
            current = _registry().get(definition.record_type).definition.content_model
            definition = replace(definition, content_model=current)
        registry.register(definition)
    with pytest.raises(RegistryStartupError) as failure:
        asyncio.run(registry.synchronize(InMemoryRecordTypeStore(ROWS)))
    assert sorted(failure.value.problems) == [
        "catalog_version_published: el esquema de la versión 1 cambió sin subir schema_version",
        "ingest_rejected: el esquema de la versión 1 cambió sin subir schema_version",
    ]
    assert not registry.sealed
