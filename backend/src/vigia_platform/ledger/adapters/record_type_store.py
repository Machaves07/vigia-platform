"""Adaptador PostgreSQL de ``RecordTypeStore`` (tabla global ``ledger.record_type``; A-52).

``RecordTypeRegistry.synchronize`` contrasta los tipos que registró cada unidad con la tabla,
guarda los nuevos o las versiones mayores y sella el registro. La tabla es global (sin
organización ni seguridad a nivel de fila, domain-entities §6), pero se lee y escribe dentro de
una ``Transaction`` de ``shared.db``, el único camino a la base: la raíz de composición la abre
con el contexto de auditoría de la proveedora, sincroniza y confirma.

``vigia_app`` tiene ``SELECT``, ``INSERT`` y ``UPDATE`` (nuc_0001), nunca ``DELETE``. Guardar
nunca baja una versión: si dos procesos arrancan a la vez, el ``UPDATE`` solo sustituye una fila
por otra de versión mayor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.ledger.registry import PersistedRecordType
from vigia_platform.shared.db import Transaction

__all__ = ["SqlRecordTypeStore"]

_LOAD: Final = text(
    "SELECT record_type, writer_unit, chain_level, schema_version, content_schema,"
    " source_key_path, free_text_paths, evidence_paths, label_rule, outbox_events,"
    " chain_follows_scope FROM ledger.record_type"
)
_SAVE: Final = text(
    "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
    " content_schema, source_key_path, free_text_paths, evidence_paths, label_rule,"
    " outbox_events, chain_follows_scope) VALUES (:record_type, :writer_unit, :chain_level,"
    " :schema_version, CAST(:content_schema AS jsonb), :source_key_path,"
    " CAST(:free_text_paths AS text[]), CAST(:evidence_paths AS text[]),"
    " CAST(:label_rule AS jsonb), CAST(:outbox_events AS text[]), :chain_follows_scope)"
    " ON CONFLICT (record_type) DO UPDATE SET writer_unit = EXCLUDED.writer_unit,"
    " chain_level = EXCLUDED.chain_level, schema_version = EXCLUDED.schema_version,"
    " content_schema = EXCLUDED.content_schema, source_key_path = EXCLUDED.source_key_path,"
    " free_text_paths = EXCLUDED.free_text_paths, evidence_paths = EXCLUDED.evidence_paths,"
    " label_rule = EXCLUDED.label_rule, outbox_events = EXCLUDED.outbox_events,"
    " chain_follows_scope = EXCLUDED.chain_follows_scope"
    " WHERE ledger.record_type.schema_version < EXCLUDED.schema_version"
)


def _document(value: Any) -> Any:
    """``jsonb`` llega decodificado con el controlador de SQLAlchemy; si llega como texto, se
    decodifica aquí."""
    return json.loads(value) if isinstance(value, str) else value


class SqlRecordTypeStore:
    """El puerto sobre una transacción abierta; no la confirma."""

    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction

    async def load(self) -> Mapping[str, PersistedRecordType]:
        result = await self._transaction.execute(_LOAD)
        rows: dict[str, PersistedRecordType] = {}
        for row in result.mappings().all():
            label_rule = _document(row["label_rule"])
            rows[row["record_type"]] = PersistedRecordType(
                record_type=row["record_type"],
                writer_unit=row["writer_unit"],
                chain_level=row["chain_level"],
                schema_version=row["schema_version"],
                content_schema=_document(row["content_schema"]),
                source_key_path=row["source_key_path"],
                free_text_paths=tuple(row["free_text_paths"]),
                evidence_paths=tuple(row["evidence_paths"]),
                label_rule=None if label_rule is None else dict(label_rule),
                outbox_events=tuple(row["outbox_events"]),
                chain_follows_scope=row["chain_follows_scope"],
            )
        return rows

    async def save(self, row: PersistedRecordType) -> None:
        await self._transaction.execute(
            _SAVE,
            {
                "record_type": row.record_type,
                "writer_unit": row.writer_unit,
                "chain_level": row.chain_level,
                "schema_version": row.schema_version,
                "content_schema": json.dumps(row.content_schema, allow_nan=False),
                "source_key_path": row.source_key_path,
                "free_text_paths": list(row.free_text_paths),
                "evidence_paths": list(row.evidence_paths),
                "label_rule": None
                if row.label_rule is None
                else json.dumps(row.label_rule, allow_nan=False),
                "outbox_events": list(row.outbox_events),
                "chain_follows_scope": row.chain_follows_scope,
            },
        )
