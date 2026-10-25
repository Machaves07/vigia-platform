"""Repositorio de ``loop.review_queue_item`` sobre PostgreSQL (TASK-309; LC-LAZ-08; SUP-U05-05 y
06).

La cola de revisión es una tabla propia (DE §2.4), separada de ``loop.finding_state``: la
proyección genérica (``loop.projection.fold``, ya mergeada) deriva el estado ``in_review`` del
hallazgo desde el mismo registro ``detection_for_review_received``, pero **no** mantiene esta
tabla. ``review_queue_item`` existe para servir ``GET /loop/review-queue`` por el índice parcial
``review_queue_item_pending`` sin tocar la proyección general, y para guardar los campos propios
de la cola (umbrales, motivo de revisión, plazo congelado y recordatorios) que ``FindingState``
no lleva.

**Idempotencia** (``insert_pending``, PR-LAZ-09): ``detection_id`` es la clave primaria y
``(organization_id, source_event_id)`` tiene su propia restricción única (``laz_0029``); dos
entregas del mismo evento, o dos trabajadores que entregan la misma detección a la vez,
``INSERT ... ON CONFLICT DO NOTHING`` **sin árbitro**: la segunda no escribe nada y no lanza. Con
``ON CONFLICT (detection_id)``, el choque con la otra restricción única (la de ``source_event_id``)
seguía lanzando en la carrera de dos trabajadores (main en rojo, ci@b84dac4, L-10).

El elemento **nunca sale de la cola por la tarea de recordatorios** (``due_candidates`` y
``send_reminders``): solo ``mark_resolved`` cambia ``state`` a ``resolved``, y solo tras una
resolución humana aceptada por la máquina de estados (``loop.domain.lifecycle``, T05 y T06).
``send_reminders`` es una actualización condicional (``UPDATE ... WHERE ... RETURNING``) que solo
toca las filas cuyo último recordatorio es más viejo que ``reminder_interval_hours``: dos
ejecuciones de la tarea sobre el mismo arrendamiento, o una tarea que corre dos veces, dejan como
máximo un recordatorio por ventana (U04-H-07).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.projection.fold import (
    DEFAULT_AGGREGATION_WINDOW_MINUTES,
    ProjectionInput,
    aggregation_bucket,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "ReminderCandidate",
    "ReminderSent",
    "ReviewQueueCursor",
    "ReviewQueueItemRow",
    "ReviewQueueRepository",
    "ReviewSubjectRow",
]

_INSERT_PENDING: Final = text(
    "INSERT INTO loop.review_queue_item (detection_id, organization_id, plant_id, zone_id,"
    " node_id, anchor_record_id, family, standard_id, standard_version, max_confidence,"
    " review_threshold, publication_threshold, review_reason, observability_state_at_event,"
    " occurred_at_exact, occurred_at_bucket, clip_references, signals_summary, received_at,"
    " review_due_at, state, reminders_sent, source_event_id)"
    " VALUES (:detection_id, :organization_id, :plant_id, :zone_id, :node_id, :anchor_record_id,"
    " :family, :standard_id, :standard_version, :max_confidence, :review_threshold,"
    " :publication_threshold, :review_reason, :observability_state_at_event, :occurred_at_exact,"
    " :occurred_at_bucket, CAST(:clip_references AS jsonb), CAST(:signals_summary AS jsonb),"
    " :received_at, :review_due_at, 'pending', 0, :source_event_id)"
    " ON CONFLICT DO NOTHING"
)

_PENDING_PAGE: Final = text(
    "SELECT detection_id, organization_id, plant_id, zone_id, node_id, anchor_record_id, family,"
    " standard_id, standard_version, max_confidence, review_threshold, publication_threshold,"
    " review_reason, observability_state_at_event, occurred_at_exact, occurred_at_bucket,"
    " clip_references, signals_summary, received_at, review_due_at, state, resolved_at,"
    " reminder_sent_at, reminders_sent, source_event_id"
    " FROM loop.review_queue_item"
    " WHERE organization_id = :organization_id AND state = 'pending'"
    " AND (CAST(:organization_wide AS boolean)"
    "      OR plant_id = ANY(CAST(:scope_plant_ids AS uuid[]))"
    "      OR zone_id = ANY(CAST(:scope_zone_ids AS uuid[])))"
    " AND (CAST(:after_due_at AS timestamptz) IS NULL"
    "      OR (review_due_at, detection_id)"
    "         > (CAST(:after_due_at AS timestamptz), CAST(:after_id AS uuid)))"
    " ORDER BY review_due_at, detection_id"
    " LIMIT :limit"
)

_SUBJECT_FOR_RESOLUTION: Final = text(
    "SELECT q.detection_id, q.organization_id, q.plant_id, q.zone_id, q.anchor_record_id,"
    " q.family, q.standard_id, q.standard_version, q.max_confidence, q.review_threshold,"
    " q.publication_threshold, q.review_reason, q.state AS queue_state,"
    " f.finding_state_id, f.lifecycle_state, f.previous_lifecycle_state, f.due_at"
    " FROM loop.review_queue_item q"
    " JOIN loop.finding_state f"
    " ON f.organization_id = q.organization_id AND f.plant_id = q.plant_id"
    " AND f.subject_id = q.detection_id"
    " WHERE q.organization_id = :organization_id AND q.detection_id = :detection_id"
)

_MARK_RESOLVED: Final = text(
    "UPDATE loop.review_queue_item SET state = 'resolved', resolved_at = :resolved_at"
    " WHERE organization_id = :organization_id AND detection_id = :detection_id"
    " AND state = 'pending'"
)

_DUE_CANDIDATES: Final = text(
    "SELECT detection_id, plant_id, zone_id, review_due_at, reminder_sent_at, reminders_sent"
    " FROM loop.review_queue_item"
    " WHERE organization_id = :organization_id AND state = 'pending'"
    " AND review_due_at <= :now"
    " AND (CAST(:after_due_at AS timestamptz) IS NULL"
    "      OR (review_due_at, detection_id)"
    "         > (CAST(:after_due_at AS timestamptz), CAST(:after_id AS uuid)))"
    " ORDER BY review_due_at, detection_id"
    " LIMIT :limit"
)

_SEND_REMINDERS: Final = text(
    "UPDATE loop.review_queue_item SET reminder_sent_at = :now,"
    " reminders_sent = reminders_sent + 1"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND detection_id = ANY(CAST(:detection_ids AS uuid[]))"
    " AND state = 'pending' AND review_due_at <= :now"
    " AND (reminder_sent_at IS NULL OR reminder_sent_at <= :cutoff)"
    " RETURNING detection_id, zone_id, review_due_at, reminders_sent"
)


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _instant(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewQueueCursor:
    """El cursor de página de ``GET /loop/review-queue`` (keyset sobre ``review_due_at``)."""

    review_due_at: datetime
    detection_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewQueueItemRow:
    """Una fila de ``loop.review_queue_item``."""

    detection_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    anchor_record_id: uuid.UUID
    family: str
    standard_id: uuid.UUID
    standard_version: int
    max_confidence: float
    review_threshold: float
    publication_threshold: float
    review_reason: str
    observability_state_at_event: str
    occurred_at_exact: datetime
    occurred_at_bucket: datetime
    clip_references: tuple[Mapping[str, Any], ...]
    signals_summary: Mapping[str, Any]
    received_at: datetime
    review_due_at: datetime
    state: str
    resolved_at: datetime | None
    reminder_sent_at: datetime | None
    reminders_sent: int
    source_event_id: uuid.UUID

    @property
    def cursor(self) -> ReviewQueueCursor:
        return ReviewQueueCursor(review_due_at=self.review_due_at, detection_id=self.detection_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewSubjectRow:
    """Lo que la resolución necesita, de la cola y de la proyección, de una detección."""

    detection_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    anchor_record_id: uuid.UUID
    family: str
    standard_id: uuid.UUID
    standard_version: int
    max_confidence: float
    review_threshold: float
    publication_threshold: float
    review_reason: str
    queue_state: str
    finding_state_id: uuid.UUID
    lifecycle_state: str
    previous_lifecycle_state: str | None
    due_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReminderCandidate:
    """Una fila vencida, candidata a recordatorio, antes de comprobar su ventana."""

    detection_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    review_due_at: datetime
    reminder_sent_at: datetime | None
    reminders_sent: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReminderSent:
    """Una fila que de verdad recibió el recordatorio (pasó la ventana de ``send_reminders``)."""

    detection_id: uuid.UUID
    zone_id: uuid.UUID
    review_due_at: datetime
    reminders_sent: int


def _item(row: Any) -> ReviewQueueItemRow:
    data = row._mapping
    return ReviewQueueItemRow(
        detection_id=uuid.UUID(str(data["detection_id"])),
        organization_id=uuid.UUID(str(data["organization_id"])),
        plant_id=uuid.UUID(str(data["plant_id"])),
        zone_id=uuid.UUID(str(data["zone_id"])),
        node_id=uuid.UUID(str(data["node_id"])),
        anchor_record_id=uuid.UUID(str(data["anchor_record_id"])),
        family=str(data["family"]),
        standard_id=uuid.UUID(str(data["standard_id"])),
        standard_version=int(data["standard_version"]),
        max_confidence=float(data["max_confidence"]),
        review_threshold=float(data["review_threshold"]),
        publication_threshold=float(data["publication_threshold"]),
        review_reason=str(data["review_reason"]),
        observability_state_at_event=str(data["observability_state_at_event"]),
        occurred_at_exact=data["occurred_at_exact"],
        occurred_at_bucket=data["occurred_at_bucket"],
        clip_references=tuple(_json(data["clip_references"])),
        signals_summary=_json(data["signals_summary"]),
        received_at=data["received_at"],
        review_due_at=data["review_due_at"],
        state=str(data["state"]),
        resolved_at=data["resolved_at"],
        reminder_sent_at=data["reminder_sent_at"],
        reminders_sent=int(data["reminders_sent"]),
        source_event_id=uuid.UUID(str(data["source_event_id"])),
    )


def _subject(row: Any) -> ReviewSubjectRow:
    data = row._mapping
    previous = data["previous_lifecycle_state"]
    return ReviewSubjectRow(
        detection_id=uuid.UUID(str(data["detection_id"])),
        organization_id=uuid.UUID(str(data["organization_id"])),
        plant_id=uuid.UUID(str(data["plant_id"])),
        zone_id=uuid.UUID(str(data["zone_id"])),
        anchor_record_id=uuid.UUID(str(data["anchor_record_id"])),
        family=str(data["family"]),
        standard_id=uuid.UUID(str(data["standard_id"])),
        standard_version=int(data["standard_version"]),
        max_confidence=float(data["max_confidence"]),
        review_threshold=float(data["review_threshold"]),
        publication_threshold=float(data["publication_threshold"]),
        review_reason=str(data["review_reason"]),
        queue_state=str(data["queue_state"]),
        finding_state_id=uuid.UUID(str(data["finding_state_id"])),
        lifecycle_state=str(data["lifecycle_state"]),
        previous_lifecycle_state=None if previous is None else str(previous),
        due_at=data["due_at"],
    )


def _candidate(row: Any) -> ReminderCandidate:
    data = row._mapping
    return ReminderCandidate(
        detection_id=uuid.UUID(str(data["detection_id"])),
        plant_id=uuid.UUID(str(data["plant_id"])),
        zone_id=uuid.UUID(str(data["zone_id"])),
        review_due_at=data["review_due_at"],
        reminder_sent_at=data["reminder_sent_at"],
        reminders_sent=int(data["reminders_sent"]),
    )


def _reminded(row: Any) -> ReminderSent:
    data = row._mapping
    return ReminderSent(
        detection_id=uuid.UUID(str(data["detection_id"])),
        zone_id=uuid.UUID(str(data["zone_id"])),
        review_due_at=data["review_due_at"],
        reminders_sent=int(data["reminders_sent"]),
    )


@repository
class ReviewQueueRepository:
    """Las sentencias de ``loop.review_queue_item`` (LC-LAZ-08)."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def subject_for_authorization(
        self, context: ScopeContext, detection_id: uuid.UUID
    ) -> ReviewSubjectRow | None:
        """La misma fila que ``subject_for_resolution``, fuera de una transacción de escritura
        (el primer paso de ``resolve``, antes de tomar ningún candado)."""
        rows = await self._database.read(
            context,
            _SUBJECT_FOR_RESOLUTION,
            {"organization_id": context.organization_id, "detection_id": detection_id},
        )
        return _subject(rows[0]) if rows else None

    async def insert_pending(
        self,
        transaction: Transaction,
        item: ProjectionInput,
        *,
        event_id: uuid.UUID,
        review_due_at: datetime,
        review_reason: str,
    ) -> bool:
        """Crea el elemento ``pending``; ``False`` si ya existía (idempotente, PR-LAZ-09).

        Todo lo demás sale de ``item``: el documento (campos propios de la detección), el
        registro (``anchor_record_id``) y su recibo (``received_at``, el ancla de
        ``review_due_at``, BR de la restricción ``review_queue_item_due_after_received``).
        ``review_reason`` no está en el contenido de ``DetectionForReview`` (solo en la carga de
        ``detection_for_review_received``, A-51): lo trae quien ya leyó el evento."""
        document = item.document
        occurred_at_exact = _instant(document["node_time"]["started_at"])
        parameters = {
            "detection_id": uuid.UUID(str(document["detection_id"])),
            "organization_id": item.organization_id,
            "plant_id": item.plant_id,
            "zone_id": uuid.UUID(str(document["zone_id"])),
            "node_id": uuid.UUID(str(document["node_id"])),
            "anchor_record_id": item.record_id,
            "family": str(document["family"]),
            "standard_id": uuid.UUID(str(document["standard"]["standard_id"])),
            "standard_version": int(document["standard"]["version"]),
            "max_confidence": float(document["max_confidence"]),
            "review_threshold": float(document["review_threshold"]),
            "publication_threshold": float(document["publication_threshold"]),
            "review_reason": review_reason,
            "observability_state_at_event": str(document["observability_state"]),
            "occurred_at_exact": occurred_at_exact,
            "occurred_at_bucket": aggregation_bucket(
                occurred_at_exact, DEFAULT_AGGREGATION_WINDOW_MINUTES
            ),
            "clip_references": json.dumps(list(_clip_references(document))),
            "signals_summary": json.dumps({"readings": document["signals"]}),
            "received_at": item.received_at,
            "review_due_at": review_due_at,
            "source_event_id": event_id,
        }
        result = await transaction.execute(_INSERT_PENDING, parameters)
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def pending_page(
        self,
        transaction: Transaction,
        *,
        organization_id: uuid.UUID,
        organization_wide: bool,
        plant_ids: Sequence[uuid.UUID],
        zone_ids: Sequence[uuid.UUID],
        after: ReviewQueueCursor | None,
        limit: int,
    ) -> list[ReviewQueueItemRow]:
        """La página de elementos pendientes visibles, ordenada por ``review_due_at``."""
        parameters: dict[str, Any] = {
            "organization_id": organization_id,
            "organization_wide": organization_wide,
            "scope_plant_ids": list(plant_ids),
            "scope_zone_ids": list(zone_ids),
            "after_due_at": None if after is None else after.review_due_at,
            "after_id": None if after is None else after.detection_id,
            "limit": limit,
        }
        result = await transaction.execute(_PENDING_PAGE, parameters)
        return [_item(row) for row in result.all()]

    async def subject_for_resolution(
        self, transaction: Transaction, organization_id: uuid.UUID, detection_id: uuid.UUID
    ) -> ReviewSubjectRow | None:
        """La fila de la cola y la de la proyección, juntas; ``None`` si no existe alguna."""
        result = await transaction.execute(
            _SUBJECT_FOR_RESOLUTION,
            {"organization_id": organization_id, "detection_id": detection_id},
        )
        row = result.first()
        return None if row is None else _subject(row)

    async def mark_resolved(
        self,
        transaction: Transaction,
        organization_id: uuid.UUID,
        detection_id: uuid.UUID,
        *,
        resolved_at: datetime,
    ) -> bool:
        """Pasa el elemento a ``resolved``; ``False`` si ya no estaba ``pending``."""
        result = await transaction.execute(
            _MARK_RESOLVED,
            {
                "organization_id": organization_id,
                "detection_id": detection_id,
                "resolved_at": resolved_at,
            },
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def due_candidates(
        self,
        transaction: Transaction,
        *,
        organization_id: uuid.UUID,
        now: datetime,
        after: ReviewQueueCursor | None,
        limit: int,
    ) -> list[ReminderCandidate]:
        """Elementos vencidos (``review_due_at <= now``), por lotes, sin escribir nada."""
        parameters = {
            "organization_id": organization_id,
            "now": now,
            "after_due_at": None if after is None else after.review_due_at,
            "after_id": None if after is None else after.detection_id,
            "limit": limit,
        }
        result = await transaction.execute(_DUE_CANDIDATES, parameters)
        return [_candidate(row) for row in result.all()]

    async def send_reminders(
        self,
        transaction: Transaction,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        detection_ids: Sequence[uuid.UUID],
        now: datetime,
        cutoff: datetime,
    ) -> list[ReminderSent]:
        """Recordatorio condicional (U04-H-07): solo si el anterior fue antes de ``cutoff``.

        Atómico por fila (``UPDATE ... WHERE ... RETURNING``): una segunda ejecución sobre el
        mismo lote, con el mismo ``now``, no vuelve a tocar una fila que ya se actualizó."""
        if not detection_ids:
            return []
        result = await transaction.execute(
            _SEND_REMINDERS,
            {
                "organization_id": organization_id,
                "plant_id": plant_id,
                "detection_ids": list(detection_ids),
                "now": now,
                "cutoff": cutoff,
            },
        )
        return [_reminded(row) for row in result.all()]


def _clip_references(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Los clips del tramo único de la detección, en el orden de ``finding_state`` (fold._clips)."""
    index = int(document["episode"]["segment_index"])
    entries = [
        {
            "camera_id": str(camera["camera_id"]),
            "clip_id": str(clip["clip_id"]),
            "segment": str(clip["segment"]),
            "segment_index": index,
        }
        for camera in document["cameras"]
        for clip in camera["clips"]
    ]
    return sorted(
        entries, key=lambda clip: (clip["camera_id"], clip["segment_index"], clip["clip_id"])
    )
