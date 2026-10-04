"""Actas de alcance sobre PostgreSQL (DE §2.6; ``catalog.mounting_gate_record`` ⛓).

Solo ``INSERT`` y ``SELECT``: un acta firmada nunca cambia (la tabla rechaza todo ``UPDATE``). Una
zona puede acumular actas; vale la última aprobada, que es la que cita la compuerta de montaje.
Toda sentencia nombra la organización del contexto y la zona (defensa en profundidad sobre la
RLS).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.catalog.domain.scope_record import MountingGateRecord
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresScopeRecordRepository"]

_INSERT: Final = text(
    "INSERT INTO catalog.mounting_gate_record (record_id, organization_id, plant_id, zone_id,"
    " scope_text_es, cameras, blur_verification, document_ref, signed_by, role_in_use,"
    " plant_policy_loaded_at_signing, ledger_record_id)"
    " VALUES (:record_id, :organization_id, :plant_id, :zone_id, :scope_text_es,"
    " CAST(:cameras AS jsonb), CAST(:blur_verification AS jsonb), CAST(:document_ref AS jsonb),"
    " :signed_by, :role_in_use, :plant_policy_loaded_at_signing, :ledger_record_id)"
)
_RECORD_IDS: Final = text(
    "SELECT record_id FROM catalog.mounting_gate_record"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id ORDER BY record_id"
)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@repository
class PostgresScopeRecordRepository:
    """Alta y lectura de las actas de alcance de una zona."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def insert(self, transaction: Transaction, record: MountingGateRecord) -> None:
        """Anexa el acta (su registro del expediente ya escrito)."""
        if record.organization_id != transaction.context.organization_id:
            raise ValueError("el acta es de otra organización que la transacción")
        if record.ledger_record_id is None:
            raise ValueError("el acta se anexa con su registro del expediente")
        document_ref: Any = None if record.document_ref is None else record.document_ref.to_json()
        await transaction.execute(
            _INSERT,
            {
                "record_id": record.record_id,
                "organization_id": record.organization_id,
                "plant_id": record.plant_id,
                "zone_id": record.zone_id,
                "scope_text_es": record.scope_text_es,
                "cameras": _dumps([camera.as_json() for camera in record.cameras]),
                "blur_verification": _dumps(record.blur_verification()),
                "document_ref": None if document_ref is None else _dumps(document_ref),
                "signed_by": record.signed_by,
                "role_in_use": record.role_in_use.value,
                "plant_policy_loaded_at_signing": record.plant_policy_loaded_at_signing,
                "ledger_record_id": record.ledger_record_id,
            },
        )

    async def record_ids(self, context: ScopeContext, zone_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """Las actas de la zona (identificadores)."""
        rows = await self._database.read(
            context, _RECORD_IDS, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        return tuple(uuid.UUID(str(row.record_id)) for row in rows)
