"""La tabla ``ledger.record_type`` que dejaba vigia-platform ``f64df12`` (contrato v1.1.0; VIG-457).

``tests/fixtures/record_types/ledger_record_type_f64df12.json`` son las 58 filas que persiste el
arranque de ``f64df12`` (``record_type_registry(registered_units()).latest()`` →
``CompiledType.to_persisted()``, generadas desde ese commit): solo esquemas, rutas y nombres de
eventos, ningún dato. Es la base de las pruebas de actualización: el código actual tiene que
arrancar sobre ella sin ``RegistryStartupError`` (BR-NUC-52).

``UPGRADED_TYPES`` es el oráculo, escrito a mano: los tipos cuyo esquema cambió de ``f64df12`` a
``main`` (contratos v1.4.0 y v1.5.0, vigia-platform#316 y #317, y v1.6.0, TASK-593) y la versión
que los escribe ahora.

``ledger_record_type_18981b6.json`` es la misma tabla con vigia-platform ``18981b6`` (contrato
v1.5.0), la que dejó la plataforma de integración antes de adoptar v1.6.0; ``V1_6_UPGRADED_TYPES``
es su oráculo: los tres tipos cuyo esquema amplía v1.6.0 (TASK-593).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from vigia_platform.ledger.registry import PersistedRecordType

__all__ = [
    "F64DF12_COMMIT",
    "F64DF12_FIXTURE",
    "UPGRADED_TYPES",
    "V1_5_COMMIT",
    "V1_5_FIXTURE",
    "V1_6_UPGRADED_TYPES",
    "f64df12_document",
    "f64df12_rows",
    "persisted_row",
    "v1_5_document",
    "v1_5_rows",
]

F64DF12_FIXTURE: Final = (
    Path(__file__).parent / "fixtures" / "record_types" / "ledger_record_type_f64df12.json"
)
F64DF12_COMMIT: Final = "f64df12ad91deb1e6457fa5d4db4afe15290a99c"

UPGRADED_TYPES: Final = {
    "catalog_version_published": 3,
    "ingest_rejected": 2,
    "finding_received": 2,
    "detection_for_review_received": 2,
}
"""Tipo → versión que lo escribe ahora; en ``f64df12`` los cuatro estaban en la 1."""

V1_5_FIXTURE: Final = F64DF12_FIXTURE.with_name("ledger_record_type_18981b6.json")
V1_5_COMMIT: Final = "18981b64c94cb68a3b03d66b11babe9f8db0e808"

V1_6_UPGRADED_TYPES: Final = {
    "catalog_version_published": 3,
    "finding_received": 2,
    "detection_for_review_received": 2,
}
"""Tipo → versión que lo escribe con v1.6.0; con ``18981b6`` estaban en la 2, la 1 y la 1."""


def f64df12_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(F64DF12_FIXTURE.read_text(encoding="utf-8"))
    return document


def persisted_row(row: dict[str, Any]) -> PersistedRecordType:
    return PersistedRecordType(
        record_type=row["record_type"],
        writer_unit=row["writer_unit"],
        chain_level=row["chain_level"],
        schema_version=row["schema_version"],
        content_schema=row["content_schema"],
        source_key_path=row["source_key_path"],
        free_text_paths=tuple(row["free_text_paths"]),
        evidence_paths=tuple(row["evidence_paths"]),
        label_rule=None if row["label_rule"] is None else dict(row["label_rule"]),
        outbox_events=tuple(row["outbox_events"]),
        chain_follows_scope=row["chain_follows_scope"],
    )


def f64df12_rows() -> dict[str, PersistedRecordType]:
    """Las filas de la tabla, por tipo."""
    return {row["record_type"]: persisted_row(row) for row in f64df12_document()["rows"]}


def v1_5_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(V1_5_FIXTURE.read_text(encoding="utf-8"))
    return document


def v1_5_rows() -> dict[str, PersistedRecordType]:
    """Las filas de la tabla de ``18981b6`` (contrato v1.5.0), por tipo."""
    return {row["record_type"]: persisted_row(row) for row in v1_5_document()["rows"]}
