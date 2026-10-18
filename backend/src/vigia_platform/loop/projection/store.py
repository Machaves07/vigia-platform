"""Repositorio de la proyección del lazo sobre PostgreSQL (LC-LAZ-02; TASK-305; BR-LAZ-146).

``LoopProjectionStore.apply_in_transaction(transaction, source)`` aplica un registro del
expediente a ``loop.episode``, ``loop.finding_state`` y ``loop.loop_projection_checkpoint``
**en la transacción del llamador**, con el núcleo puro ``fold.step``: lo que la reconstrucción
(TASK-306) volvería a calcular es exactamente lo que aquí se escribe. Si la transacción se
revierte, no queda ni el registro ni la proyección (BR-LAZ-146, PR-NUC-32).

**Cómo se usa desde una operación de U-04** (clasificación, cola, acciones, cierre…):
``write_and_apply(tx, writer, context, record_type, content, ...)``, que escribe el registro por
``EscritorExpediente.write(..., transaction=tx)`` y, en la misma ``tx``, llama
``apply_in_transaction(tx, receipt)``. **No** se usa el parámetro ``projection`` del escritor:
corre antes del ``INSERT`` del registro, así que ni el registro ni su ``chain_sequence`` existen
todavía y el orden de candados de abajo se invertiría. Un ``Receipt`` ``accepted_duplicate`` no
se vuelve a aplicar (el registro ya estaba en el expediente y en la proyección). La vía de
eventos de U-03 (TASK-341) pasa un ``ProjectionInput`` con su ``event_id``
(``projection_input``) y, si ``Step.late_segment`` llega, escribe primero el registro
``late_segment_attached`` y lo aplica después.

**Orden de candados** (único para toda operación que escriba la proyección; AGENTS.md):

1. la cadena de la planta (la toma el disparador del expediente en el ``INSERT`` del registro);
2. la fila ``loop_projection_checkpoint`` de la planta (``SELECT … FOR UPDATE``, creada si falta);
3. las filas ``episode`` y ``finding_state`` del sujeto.

El paso 2 serializa a todos los que aplican sobre la misma planta, por la vía de escritura o por
la de eventos: dos aplicaciones concurrentes del mismo registro esperan una a la otra y la
segunda ve ``last_record_id`` y no hace nada (PR-LAZ-43). Una planta no bloquea a otra.

Las columnas que se actualizan son solo las que ``laz_0029`` concede a ``vigia_app``
(``_EPISODE_MUTABLE`` y ``_FINDING_STATE_MUTABLE``): la organización, la planta, la zona, el
sujeto, el estándar con que se evaluó y el instante del hecho no cambian nunca.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import Row
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.ledger.application.reader import LedgerRecordView
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    Receipt,
    RecordScope,
)
from vigia_platform.loop.adapters.postgres.tables import EPISODE, FINDING_STATE
from vigia_platform.loop.projection.digest import EMPTY_PARTITION_DIGEST, replace_term
from vigia_platform.loop.projection.fold import (
    PROJECTED_RECORD_TYPES,
    EpisodeRow,
    FindingStateRow,
    ProjectionInput,
    Step,
    episode_term,
    finding_term,
    step,
    subject_id_of,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent

__all__ = ["LoopProjectionStore", "ProjectionRecordMissing", "projection_input"]

_RECORD: Final = text(
    "SELECT record_id, organization_id, plant_id, chain_sequence, record_type, received_at,"
    " content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND record_id = :record_id"
    " AND received_at = :received_at"
)
_RECORD_ANY_PARTITION: Final = text(
    "SELECT record_id, organization_id, plant_id, chain_sequence, record_type, received_at,"
    " content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND record_id = :record_id"
)
_ENSURE_CHECKPOINT: Final = text(
    "INSERT INTO loop.loop_projection_checkpoint (organization_id, plant_id)"
    " VALUES (:organization_id, :plant_id) ON CONFLICT (organization_id, plant_id) DO NOTHING"
)
_LOCK_CHECKPOINT: Final = text(
    "SELECT last_applied_ledger_sequence, last_applied_event_id, projection_digest"
    " FROM loop.loop_projection_checkpoint"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id FOR UPDATE"
)
_ADVANCE_CHECKPOINT: Final = text(
    "UPDATE loop.loop_projection_checkpoint SET"
    " last_applied_ledger_sequence = GREATEST(last_applied_ledger_sequence, :sequence),"
    " last_applied_event_id = COALESCE(:event_id, last_applied_event_id),"
    " projection_digest = :projection_digest"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)

_EPISODE_MUTABLE: Final = (
    "tier",
    "segment_record_ids",
    "segment_count",
    "expected_segment_count",
    "missing_segment_indexes",
    "started_at",
    "ended_at",
    "total_episode_duration_ms",
    "total_condition_duration_ms",
    "max_confidence",
    "clip_references",
    "continues_open",
    "last_segment_received_at",
    "last_event_id",
    "projection_updated_at",
)
"""Columnas de ``loop.episode`` con ``UPDATE`` de ``vigia_app`` (``laz_0029``)."""

_FINDING_STATE_FIXED: Final = frozenset(
    {
        "finding_state_id",
        "organization_id",
        "plant_id",
        "zone_id",
        "subject_kind",
        "subject_id",
        "anchor_record_id",
        "family",
        "standard_id",
        "standard_version",
        "occurred_at_exact",
        "occurred_at_bucket",
    }
)
_FINDING_STATE_MUTABLE: Final = tuple(
    field.name
    for field in dataclasses.fields(FindingStateRow)
    if field.name not in _FINDING_STATE_FIXED
)
"""Columnas de ``loop.finding_state`` con ``UPDATE`` de ``vigia_app`` (``laz_0029``): todas menos
lo que nace con el hallazgo."""


class ProjectionRecordMissing(LookupError):
    """El registro del recibo no está en el expediente visible para la transacción."""


def projection_input(view: LedgerRecordView, event_id: uuid.UUID | None = None) -> ProjectionInput:
    """El ``ProjectionInput`` de un registro leído por ``LectorExpediente`` (vía de eventos)."""
    return ProjectionInput(
        record_id=view.record_id,
        organization_id=view.organization_id,
        plant_id=view.plant_id,
        chain_sequence=view.chain_sequence,
        record_type=view.record_type,
        received_at=view.received_at,
        document=view.document(),
        event_id=event_id,
    )


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _episode(row: Row[Any]) -> EpisodeRow:
    data = row._mapping
    return EpisodeRow(
        episode_id=_uuid(data["episode_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        family=data["family"],
        tier=data["tier"],
        standard_id=_uuid(data["standard_id"]),
        standard_version=int(data["standard_version"]),
        segment_record_ids=tuple(_uuid(item) for item in data["segment_record_ids"]),
        segment_count=int(data["segment_count"]),
        expected_segment_count=int(data["expected_segment_count"]),
        missing_segment_indexes=tuple(int(item) for item in data["missing_segment_indexes"]),
        started_at=data["started_at"],
        ended_at=data["ended_at"],
        total_episode_duration_ms=int(data["total_episode_duration_ms"]),
        total_condition_duration_ms=int(data["total_condition_duration_ms"]),
        max_confidence=float(data["max_confidence"]),
        clip_references=tuple(data["clip_references"]),
        observability_state_at_start=data["observability_state_at_start"],
        continues_open=bool(data["continues_open"]),
        last_segment_received_at=data["last_segment_received_at"],
        last_event_id=_uuid(data["last_event_id"]),
        projection_updated_at=data["projection_updated_at"],
    )


_UUID_FIELDS: Final = frozenset(
    field.name for field in dataclasses.fields(FindingStateRow) if field.name.endswith("_id")
)


def _finding(row: Row[Any]) -> FindingStateRow:
    data = row._mapping
    values: dict[str, Any] = {}
    for field in dataclasses.fields(FindingStateRow):
        value = data[field.name]
        values[field.name] = _optional_uuid(value) if field.name in _UUID_FIELDS else value
    return FindingStateRow(**values)


def _episode_values(row: EpisodeRow) -> dict[str, Any]:
    values = dataclasses.asdict(row)
    values["segment_record_ids"] = list(row.segment_record_ids)
    values["missing_segment_indexes"] = list(row.missing_segment_indexes)
    values["clip_references"] = [dict(clip) for clip in row.clip_references]
    return values


@repository
class LoopProjectionStore:
    """``apply_in_transaction``: la proyección en la misma transacción que el registro."""

    async def apply_in_transaction(
        self, transaction: Transaction, source: ProjectionInput | Receipt
    ) -> Step:
        """Aplica ``source`` (el recibo de una escritura o el registro de un evento).

        Devuelve el ``Step`` aplicado; ``applied`` falso si ya estaba aplicado, si es un
        duplicado del escritor o si el tipo no afecta a la proyección. Lanza
        ``ProjectionInconsistent`` si el registro no es aplicable (y la transacción no debe
        confirmarse) y ``ProjectionRecordMissing`` si el recibo no corresponde a ningún registro
        visible."""
        if isinstance(source, Receipt):
            if source.status is not AcceptanceStatus.ACCEPTED:
                return Step(None, None, False)
            item = await self.record_input(
                transaction, source.record_id, received_at=source.received_at
            )
        else:
            item = source
        if item.record_type not in PROJECTED_RECORD_TYPES or item.plant_id is None:
            return Step(None, None, False)
        partition = {"organization_id": item.organization_id, "plant_id": item.plant_id}
        await transaction.execute(_ENSURE_CHECKPOINT, partition)
        checkpoint = (await transaction.execute(_LOCK_CHECKPOINT, partition)).one()
        digest: str = checkpoint.projection_digest or EMPTY_PARTITION_DIGEST
        subject = subject_id_of(item)
        result = Step(None, None, False)
        if subject is not None:
            episode = await self._episode(transaction, item, subject)
            finding = await self._finding(transaction, item, subject)
            result = step(episode, finding, item)
            if result.applied:
                digest = await self._write(transaction, episode, finding, result, digest)
        await transaction.execute(
            _ADVANCE_CHECKPOINT,
            {
                **partition,
                "sequence": item.chain_sequence,
                "event_id": item.event_id,
                "projection_digest": digest,
            },
        )
        return result

    async def write_and_apply(
        self,
        transaction: Transaction,
        writer: EscritorExpediente,
        context: ScopeContext,
        record_type: str,
        content: Mapping[str, Any],
        *,
        scope: RecordScope | None = None,
        events: Sequence[NewEvent] = (),
        occurred_at: datetime | None = None,
        record_id: uuid.UUID | None = None,
    ) -> tuple[Receipt | LedgerRejection, Step]:
        """Escribe el registro en ``transaction`` y aplica su proyección, **en ese orden**.

        Es la forma de escribir de las operaciones de U-04: fija el orden de candados del
        módulo (cadena de la planta, después el control). Un rechazo no toca la proyección.
        ``record_id`` es el del registro si se escribe (para citarlo en la carga de sus eventos);
        sin él, lo genera el escritor."""
        receipt = await writer.write(
            context,
            record_type,
            content,
            scope=scope,
            events=events,
            occurred_at=occurred_at,
            transaction=transaction,
            record_id=record_id,
        )
        if isinstance(receipt, LedgerRejection):
            return receipt, Step(None, None, False)
        return receipt, await self.apply_in_transaction(transaction, receipt)

    async def record_input(
        self,
        transaction: Transaction,
        record_id: uuid.UUID,
        *,
        received_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> ProjectionInput:
        """El registro ``record_id`` de la organización de la transacción, como lo lee el pliegue.

        Con ``received_at`` (el del ``Receipt`` del escritor) la consulta va a su partición; la
        vía de eventos (TASK-341) pasa el ``event_id`` del evento que lo anunció."""
        parameters: dict[str, Any] = {
            "organization_id": transaction.context.organization_id,
            "record_id": record_id,
        }
        if received_at is None:
            result = await transaction.execute(_RECORD_ANY_PARTITION, parameters)
        else:
            result = await transaction.execute(_RECORD, {**parameters, "received_at": received_at})
        rows = result.all()
        if not rows:
            raise ProjectionRecordMissing(str(record_id))
        (row,) = rows
        document: Mapping[str, Any] = json.loads(bytes(row.content))
        return ProjectionInput(
            record_id=_uuid(row.record_id),
            organization_id=_uuid(row.organization_id),
            plant_id=_optional_uuid(row.plant_id),
            chain_sequence=int(row.chain_sequence),
            record_type=str(row.record_type),
            received_at=row.received_at,
            document=document,
            event_id=event_id,
        )

    @staticmethod
    async def _episode(
        transaction: Transaction, item: ProjectionInput, subject: uuid.UUID
    ) -> EpisodeRow | None:
        # ``episode_id`` es la clave primaria: no hace falta la planta. El alcance por planta lo
        # pone ``finding_state`` (un tramo de otra planta no encuentra el estado y falla al crear).
        statement = (
            select(EPISODE)
            .where(EPISODE.c.organization_id == item.organization_id)
            .where(EPISODE.c.episode_id == subject)
            .with_for_update()
        )
        rows = (await transaction.execute(statement)).all()
        return _episode(rows[0]) if rows else None

    @staticmethod
    async def _finding(
        transaction: Transaction, item: ProjectionInput, subject: uuid.UUID
    ) -> FindingStateRow | None:
        statement = (
            select(FINDING_STATE)
            .where(FINDING_STATE.c.organization_id == item.organization_id)
            .where(FINDING_STATE.c.plant_id == item.plant_id)
            .where(FINDING_STATE.c.subject_id == subject)
            .with_for_update()
        )
        rows = (await transaction.execute(statement)).all()
        return _finding(rows[0]) if rows else None

    @staticmethod
    async def _write(
        transaction: Transaction,
        episode: EpisodeRow | None,
        finding: FindingStateRow | None,
        result: Step,
        digest: str,
    ) -> str:
        if result.episode is not None and result.episode != episode:
            values = _episode_values(result.episode)
            if episode is None:
                await transaction.execute(insert(EPISODE).values(**values))
            else:
                changes = {name: values[name] for name in _EPISODE_MUTABLE}
                await transaction.execute(
                    update(EPISODE)
                    .where(EPISODE.c.organization_id == episode.organization_id)
                    .where(EPISODE.c.episode_id == episode.episode_id)
                    .values(**changes)
                )
            old = None if episode is None else episode_term(episode)
            digest = replace_term(digest, old, episode_term(result.episode))
        if result.finding is not None and result.finding != finding:
            values = dataclasses.asdict(result.finding)
            if finding is None:
                await transaction.execute(insert(FINDING_STATE).values(**values))
            else:
                changes = {name: values[name] for name in _FINDING_STATE_MUTABLE}
                await transaction.execute(
                    update(FINDING_STATE)
                    .where(FINDING_STATE.c.organization_id == finding.organization_id)
                    .where(FINDING_STATE.c.finding_state_id == finding.finding_state_id)
                    .where(FINDING_STATE.c.occurred_at_bucket == finding.occurred_at_bucket)
                    .values(**changes)
                )
            old = None if finding is None else finding_term(finding)
            digest = replace_term(digest, old, finding_term(result.finding))
        return digest
