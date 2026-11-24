"""Versión 3 congelada de ``catalog_version_published`` (TASK-594; BR-NUC-52).

TASK-594 añade ``process_context`` a ``changed_fields`` (las variables de proceso de la zona,
A-129): el esquema del tipo crece y la versión 4 es la que se escribe. La 3 (TASK-593) ya pudo
persistirse en una base (``f5b8d59``); sin congelarla, esa base no arrancaría
(``RegistryStartupError``, como en VIG-457).

Sin base de datos, sobre la fila que persistía ``f5b8d59``
(``tests/fixtures/record_types/catalog_version_published_v3_f5b8d59.json``):

- la versión 3 registrada conserva **su** esquema y la 4 solo la amplía, con un único valor nuevo
  en ``changed_fields``;
- ``synchronize`` sobre esa fila arranca, sella y sube el tipo a la 4;
- un registro con ``changed_fields = [process_context]`` solo valida con la 4, y uno de la 3 con
  las dos.

La mutación «v3 desde el modelo vigente» (registrar la 3 con el modelo de la 4) vuelve a impedir el
arranque.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any, Final

import pytest
from pydantic import ValidationError
from vigia_contracts.canonical import canonicalize
from vigia_contracts.conformance.checks._core import draw_seeded
from vigia_contracts.conformance.generators import with_process_context, zone_catalog

from tests.record_type_upgrade_support import V1_5_FIXTURE, persisted_row
from vigia_platform.catalog.record_types import (
    CatalogVersionPublished,
    CatalogVersionPublishedV3,
)
from vigia_platform.ledger.registry import (
    InMemoryRecordTypeStore,
    PersistedRecordType,
    RecordTypeRegistry,
    RegistryStartupError,
)
from vigia_platform.ledger.schema_rules import compatibility_problems, normalized_schema
from vigia_platform.shared.runtime.units import record_type_registry, registered_units

FIXTURE: Final = V1_5_FIXTURE.with_name("catalog_version_published_v3_f5b8d59.json")
COMMIT: Final = "f5b8d592297e9f394031a51d545d4a36009b870e"
NAME: Final = "catalog_version_published"
SEED: Final = 20261124


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return document


def _v3_row() -> PersistedRecordType:
    (row,) = _document()["rows"]
    return persisted_row(row)


def _table() -> dict[str, PersistedRecordType]:
    """La tabla que deja ``f5b8d59``: la de hoy con ``catalog_version_published`` en la 3."""
    current = {compiled.record_type: compiled.to_persisted() for compiled in _registry().latest()}
    return {**current, NAME: _v3_row()}


def _registry() -> RecordTypeRegistry:
    return record_type_registry(registered_units())


def test_the_fixture_is_the_v3_row_that_f5b8d59_persisted() -> None:
    document = _document()
    assert document["generated_from"]["commit"] == COMMIT
    assert document["generated_from"]["contract"] == "vigia-contracts v1.6.0"
    row = _v3_row()
    assert (row.record_type, row.schema_version) == (NAME, 3)


def test_the_registered_v3_keeps_its_exact_schema_and_model() -> None:
    registry = _registry()
    compiled = registry.get(NAME, schema_version=3)
    assert compiled.definition.content_model is CatalogVersionPublishedV3
    assert normalized_schema(compiled.content_schema) == normalized_schema(_v3_row().content_schema)


def test_v4_only_adds_process_context_to_changed_fields_and_is_the_one_written() -> None:
    registry = _registry()
    latest = registry.get(NAME)
    assert latest.schema_version == 4
    assert latest.definition.content_model is CatalogVersionPublished
    old = _v3_row().content_schema
    assert compatibility_problems(old, latest.content_schema) == []
    before = old["$defs"]["CatalogChangedField"]["enum"]
    after = latest.content_schema["$defs"]["CatalogChangedField"]["enum"]
    assert after == [*before, "process_context"]
    assert (
        latest.definition.free_text_paths
        == registry.get(NAME, schema_version=3).definition.free_text_paths
    )


def test_synchronizing_over_the_v3_row_starts_and_raises_the_type_to_4() -> None:
    table = _table()
    registry = _registry()
    store = InMemoryRecordTypeStore(table)
    asyncio.run(registry.synchronize(store))  # sin RegistryStartupError
    assert registry.sealed
    after = asyncio.run(store.load())
    changed = {name: row.schema_version for name, row in after.items() if row != table.get(name)}
    assert changed == {NAME: 4}
    assert after[NAME] == registry.get(NAME).to_persisted()


def _published(changed_fields: tuple[str, ...]) -> str:
    """Un ``catalog_version_published`` con variables de proceso: firma sintética."""
    payload = draw_seeded(with_process_context(draw_seeded(zone_catalog(), SEED), minimum=1), SEED)
    envelope = {
        "payload": payload,
        "payload_canonical_sha256": hashlib.sha256(canonicalize(payload)).hexdigest(),
        "signature": "A" * 86 + "==",
        "key_id": "catalog-sintetica-1",
        "signed_at": "2026-11-24T08:00:01.000Z",
    }
    return json.dumps(
        {
            "source_key": f"{payload['zone_id']}:{payload['version']}",
            "zone_id": payload["zone_id"],
            "catalog_version": payload["version"],
            "changed_fields": list(changed_fields),
            "reason_es": "Variables de proceso de la zona sintética",
            "envelope": envelope,
        }
    )


def test_a_process_context_change_only_validates_with_v4() -> None:
    registry = _registry()
    v3, v4 = (registry.get(NAME, schema_version=n) for n in (3, 4))
    previous = _published(("signals",))
    v3.validate_json(previous)
    v4.validate_json(previous)
    current = _published(("process_context",))
    v4.validate_json(current)
    with pytest.raises(ValidationError):
        v3.validate_json(current)


def test_registering_v3_with_the_current_model_prevents_startup() -> None:
    """La mutación con nombre «v3 desde el modelo vigente»."""
    registry = RecordTypeRegistry()
    fresh = _registry()
    for compiled in fresh.all_versions():
        definition = compiled.definition
        if definition.record_type == NAME:
            if definition.schema_version == 4:
                continue
            if definition.schema_version == 3:
                definition = replace(definition, content_model=CatalogVersionPublished)
        registry.register(definition)
    with pytest.raises(RegistryStartupError) as failure:
        asyncio.run(registry.synchronize(InMemoryRecordTypeStore(_table())))
    assert (
        f"{NAME}: el esquema de la versión 3 cambió sin subir schema_version"
        in failure.value.problems
    )
    assert not registry.sealed
