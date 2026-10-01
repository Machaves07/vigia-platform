"""``CheckpointStore`` sobre PostgreSQL (LC-NUC-14): cabezas, último punto de control y escritura.

Lo que ``ledger.chain.checkpoints`` (módulo aislado, sin SQLAlchemy) necesita de la base:

- ``heads`` y ``head``: ``ledger.chain_head`` de la organización del contexto (``vigia_app`` solo la
  lee) y si el registro de la cabeza ya es un punto de control (la idempotencia por cabeza);
- ``latest``: el punto de control de mayor secuencia de una cadena, leído por el índice de la
  cadena desde la cabeza hacia atrás;
- ``append``: en el expediente, por ``EscritorExpediente`` (el único camino de escritura) con el
  tipo ``checkpoint``, la planta de la cadena como alcance y el evento ``checkpoint_written``; en
  la auditoría, por ``AuditWriter`` con ``operation = checkpoint`` y el contenido en ``filters``,
  con el evento publicado en la misma transacción. Si el disparador rechaza el punto de control
  porque la cabeza avanzó (restricción ``ledger_record_checkpoint_coverage``, ``nuc_0005``), no
  queda nada escrito y se lanza ``CheckpointCoverageConflict``; cualquier otro rechazo del
  expediente sube como ``CheckpointRejected``.

Todas las consultas van con el ``ScopeContext`` recibido: la seguridad a nivel de fila limita cada
lectura y escritura a su organización.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    CHECK_VIOLATION,
    CHECKPOINT_COVERAGE_CONSTRAINT,
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    Receipt,
    RecordScope,
    violated_constraint,
)
from vigia_platform.ledger.chain.checkpoints import (
    CHECKPOINT,
    ChainHeadState,
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointCoverageConflict,
    StoredCheckpoint,
    event_payload,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort

__all__ = ["CheckpointRejected", "SqlCheckpointStore"]

CHECKPOINT_WRITTEN: Final = "checkpoint_written"

_HEADS: Final = text(
    "SELECT h.kind, h.plant_id, h.last_sequence, h.last_hash,"
    " CASE WHEN h.kind = 'ledger' THEN EXISTS ("
    "   SELECT 1 FROM ledger.ledger_record AS r"
    "   WHERE r.organization_id = h.organization_id AND r.chain_sequence = h.last_sequence"
    "   AND ((h.plant_id IS NULL AND r.plant_id IS NULL) OR r.plant_id = h.plant_id)"
    "   AND r.record_type = 'checkpoint')"
    " ELSE EXISTS ("
    "   SELECT 1 FROM shared.audit_entry AS a"
    "   WHERE a.organization_id = h.organization_id AND a.chain_sequence = h.last_sequence"
    "   AND a.operation = 'checkpoint') END AS last_is_checkpoint"
    " FROM ledger.chain_head AS h"
    " WHERE h.organization_id = :organization_id"
    " AND (CAST(:kind AS text) IS NULL OR (h.kind = CAST(:kind AS text)"
    "   AND h.plant_id IS NOT DISTINCT FROM CAST(:plant_id AS uuid)))"
    " ORDER BY h.kind, h.plant_id NULLS FIRST"
)

_LATEST_PLANT: Final = text(
    "SELECT record_id AS entry_id, chain_sequence, record_hash AS entry_hash, content"
    " FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND record_type = 'checkpoint' ORDER BY chain_sequence DESC LIMIT 1"
)
_LATEST_ORGANIZATION: Final = text(
    "SELECT record_id AS entry_id, chain_sequence, record_hash AS entry_hash, content"
    " FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id IS NULL"
    " AND record_type = 'checkpoint' ORDER BY chain_sequence DESC LIMIT 1"
)
_LATEST_AUDIT: Final = text(
    "SELECT entry_id, chain_sequence, entry_hash, filters AS content FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND operation = 'checkpoint'"
    " ORDER BY chain_sequence DESC LIMIT 1"
)

_RECORD_BY_ID: Final = text(
    "SELECT record_id AS entry_id, chain_sequence, record_hash AS entry_hash, content"
    " FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND record_id = :entry_id"
    " AND received_at = :moment"
)
_ENTRY_BY_ID: Final = text(
    "SELECT entry_id, chain_sequence, entry_hash, filters AS content FROM shared.audit_entry"
    " WHERE organization_id = :organization_id AND entry_id = :entry_id"
    " AND occurred_at = :moment"
)


class CheckpointRejected(Exception):
    """El expediente rechazó el punto de control con un código cerrado."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"punto de control rechazado por el expediente: {rejection.code.value}")
        self.rejection = rejection


def _is_coverage_conflict(error: sa_exc.IntegrityError) -> bool:
    return violated_constraint(error, CHECK_VIOLATION) == CHECKPOINT_COVERAGE_CONSTRAINT


def _stored(chain: CheckpointChain, row: Row[Any]) -> StoredCheckpoint:
    content = CheckpointContent.from_json(json.loads(bytes(row.content)))
    return StoredCheckpoint(
        chain=chain,
        sequence=int(row.chain_sequence),
        entry_id=_uuid(row.entry_id),
        entry_hash=str(row.entry_hash),
        content=content,
    )


@repository
class SqlCheckpointStore:
    """``CheckpointStore`` sobre ``shared.db``, ``EscritorExpediente`` y ``AuditWriter``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        audit: AuditWriter,
        outbox: OutboxPort,
    ) -> None:
        self._database = database
        self._writer = writer
        self._audit = audit
        self._outbox = outbox

    async def heads(self, context: ScopeContext) -> Sequence[ChainHeadState]:
        rows = await self._database.read(context, _HEADS, self._head_parameters(context, None))
        return tuple(_head(row) for row in rows)

    async def head(self, context: ScopeContext, chain: CheckpointChain) -> ChainHeadState | None:
        rows = await self._database.read(context, _HEADS, self._head_parameters(context, chain))
        return _head(rows[0]) if rows else None

    async def latest(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> StoredCheckpoint | None:
        parameters: dict[str, Any] = {"organization_id": context.organization_id}
        if chain.kind is ChainKind.AUDIT:
            statement = _LATEST_AUDIT
        elif chain.plant_id is None:
            statement = _LATEST_ORGANIZATION
        else:
            statement = _LATEST_PLANT
            parameters["plant_id"] = chain.plant_id
        rows = await self._database.read(context, statement, parameters)
        return _stored(chain, rows[0]) if rows else None

    async def append(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        if chain.kind is ChainKind.AUDIT:
            return await self._append_audit(context, chain, content)
        return await self._append_ledger(context, chain, content)

    # --- interno -----------------------------------------------------------------------------

    @staticmethod
    def _head_parameters(context: ScopeContext, chain: CheckpointChain | None) -> dict[str, Any]:
        return {
            "organization_id": context.organization_id,
            "kind": None if chain is None else chain.kind.value,
            "plant_id": None if chain is None else chain.plant_id,
        }

    async def _append_ledger(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        event = NewEvent(event_name=CHECKPOINT_WRITTEN, payload=event_payload(chain, content))
        try:
            result = await self._writer.write(
                context,
                CHECKPOINT,
                content.to_json(),
                scope=RecordScope(plant_id=chain.plant_id),
                events=(event,),
            )
        except sa_exc.IntegrityError as error:
            if _is_coverage_conflict(error):
                raise CheckpointCoverageConflict() from None
            raise
        if isinstance(result, LedgerRejection):
            raise CheckpointRejected(result)
        return await self._read_back(context, chain, _RECORD_BY_ID, result)

    async def _append_audit(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        event = NewEvent(event_name=CHECKPOINT_WRITTEN, payload=event_payload(chain, content))
        try:
            async with self._database.transaction(context) as transaction:
                receipt = await self._audit.append(
                    context,
                    AuditOperation.CHECKPOINT,
                    filters=content.to_json(),
                    transaction=transaction,
                )
                await self._outbox.publish(transaction, event)
        except sa_exc.IntegrityError as error:
            if _is_coverage_conflict(error):
                raise CheckpointCoverageConflict() from None
            raise
        rows = await self._database.read(
            context,
            _ENTRY_BY_ID,
            {
                "organization_id": context.organization_id,
                "entry_id": receipt.entry_id,
                "moment": receipt.occurred_at,
            },
        )
        return _stored(chain, rows[0])

    async def _read_back(
        self,
        context: ScopeContext,
        chain: CheckpointChain,
        statement: Any,
        receipt: Receipt,
    ) -> StoredCheckpoint:
        parameters: Mapping[str, Any] = {
            "organization_id": context.organization_id,
            "entry_id": receipt.record_id,
            "moment": receipt.received_at,
        }
        rows = await self._database.read(context, statement, parameters)
        return _stored(chain, rows[0])


def _uuid(value: object) -> uuid.UUID:
    """El UUID del controlador (``asyncpg`` tiene su propio tipo) como ``uuid.UUID``."""
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _head(row: Row[Any]) -> ChainHeadState:
    plant_id = None if row.plant_id is None else _uuid(row.plant_id)
    return ChainHeadState(
        chain=CheckpointChain(ChainKind(row.kind), plant_id),
        last_sequence=int(row.last_sequence),
        last_hash=str(row.last_hash),
        last_is_checkpoint=bool(row.last_is_checkpoint),
    )
