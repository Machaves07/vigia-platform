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
import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError
from vigia_contracts.canonical import canonicalize

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


_ZONE = "3c1f0a52-7d4e-4b8a-9e21-5f6d7c8b9a01"


def _catalog_v1_1_0(version: int) -> dict[str, Any]:
    """Un ``ZoneCatalog`` con la forma de v1.1.0: sin ``guard_conditions`` ni ``no_data``."""
    return {
        "zone_id": _ZONE,
        "zone_code": "ZN-PRENSAS",
        "plant_id": "7a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d",
        "organization_id": "1f2e3d4c-5b6a-4978-8a6b-5c4d3e2f1a0b",
        "version": version,
        "issued_at": "2026-11-01T08:00:00.000Z",
        "cameras": [
            {
                "camera_id": "9e8d7c6b-5a49-4382-a716-05f4e3d2c1b0",
                "code": "CAM-01",
                "role_in_zone": "primary",
                "declared_min_fps": 10.0,
            }
        ],
        "minimum_coverage": {
            "required_count": 1,
            "required_camera_ids": ["9e8d7c6b-5a49-4382-a716-05f4e3d2c1b0"],
        },
        "signals": [
            {
                "signal_id": "4b5c6d7e-8f90-4a1b-9c2d-3e4f5a6b7c8e",
                "code": "SIG-ENERGIA",
                "role": "energy",
                "asserted_level": "high",
                "source": {"reader": "plc-prensa", "channel": 3},
                "description_es": "Energía de la prensa",
            }
        ],
        "standards": [
            {
                "standard_id": "6c7d8e9f-0a1b-4c2d-8e3f-4a5b6c7d8e9f",
                "version": 1,
                "family": "dwell",
                "title_es": "Permanencia en la zona de la prensa",
                "declared_text": "Nadie permanece en la zona mientras la prensa está energizada.",
                "declared_by": {
                    "user_id": "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a",
                    "display_name": "Coordinación SST sintética",
                    "role": "coordinator_sst",
                },
                "effective_from": "2026-11-01T08:00:00.000Z",
                "tier_policy": "tier_1_when_signal_valid",
                "predicate": {
                    "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
                    "min_duration_ms": 5000,
                },
            }
        ],
        "thresholds": {"review": 0.4, "publication": 0.7},
        "clip_window": {"pre_seconds": 10, "post_seconds": 10},
        "episode": {"grouping_window_ms": 3000, "max_segment_ms": 900000},
        "commissioning_watermark": True,
    }


def _published(
    catalog_version: int = 3,
    *,
    payload_version: int | None = None,
    changed_fields: tuple[str, ...] = ("standards",),
    source_key: str | None = None,
) -> str:
    """Un ``catalog_version_published`` histórico (v1): firma sintética de baja entropía."""
    payload = _catalog_v1_1_0(catalog_version if payload_version is None else payload_version)
    envelope = {
        "payload": payload,
        "payload_canonical_sha256": hashlib.sha256(canonicalize(payload)).hexdigest(),
        "signature": "A" * 86 + "==",
        "key_id": "catalog-sintetica-1",
        "signed_at": "2026-11-01T08:00:01.000Z",
    }
    return json.dumps(
        {
            "source_key": source_key or f"{_ZONE}:{catalog_version}",
            "zone_id": _ZONE,
            "catalog_version": catalog_version,
            "changed_fields": list(changed_fields),
            "reason_es": "Ajuste del estándar de permanencia",
            "envelope": envelope,
        }
    )


def test_a_historical_v1_catalog_version_still_validates_with_its_version_and_v2() -> None:
    registry = _registry()
    historical = _published()
    registry.get("catalog_version_published", schema_version=1).validate_json(historical)
    registry.get("catalog_version_published", schema_version=2).validate_json(historical)


def test_the_v1_envelope_is_the_one_of_v1_1_0() -> None:
    """``guard_conditions`` (v1.2.0, A-77) no existe en la versión 1: la 2 sí la admite."""
    registry = _registry()
    document = json.loads(_published())
    payload = document["envelope"]["payload"]
    guard = {
        "signal_id": "8a9b0c1d-2e3f-4a5b-8c6d-7e8f9a0b1c2d",
        "code": "SIG-JUMBO",
        "role": "guard",
        "asserted_level": "high",
        "source": {"reader": "plc-prensa", "channel": 4},
        "description_es": "Jumbo en posición",
    }
    payload["signals"].append(guard)
    payload["standards"][0]["family"] = "coexistence"
    payload["standards"][0]["predicate"] = {
        "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
        "min_duration_ms": 0,
        "guard_conditions": [{"signal_id": guard["signal_id"], "value": "asserted"}],
    }
    document["envelope"]["payload_canonical_sha256"] = hashlib.sha256(
        canonicalize(payload)
    ).hexdigest()
    widened = json.dumps(document)
    with pytest.raises(ValidationError):
        registry.get("catalog_version_published", schema_version=1).validate_json(widened)
    registry.get("catalog_version_published", schema_version=2).validate_json(widened)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            _published(3, source_key=f"{_ZONE}:4"),
            "source_key debe ser <zone_id>:<catalog_version>",
        ),
        (
            _published(3, changed_fields=("standards", "standards")),
            "changed_fields no admite valores repetidos",
        ),
        (
            _published(4, payload_version=3),
            "el sobre firmado es de otra zona o de otra versión del catálogo",
        ),
    ],
    ids=["source_key", "changed_fields", "envelope"],
)
def test_the_frozen_v1_keeps_the_rules_between_fields(document: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CatalogVersionPublishedV1.__pydantic_validator__.validate_json(document, strict=True)


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
