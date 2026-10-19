"""``loop.review_queue`` (TASK-309; LC-LAZ-08; SUP-U05-05 y 06; PR-LAZ-08 y 09; BR-LAZ-29 a 35).

La cola de revisión recibe lo que el nodo no se atrevió a publicar: nada sale de ella sin una
resolución humana (H-05, H-23). Este módulo tiene tres piezas:

1. **``ReviewQueueConsumer``** (consumidor ``review_queue``, ``U04_CONSUMERS``, concurrencia 2):
   entrega de ``detection_for_review_received`` (U-03). Lee el contenido completo por
   ``LectorExpediente.get`` con el ``platform_record_id`` del evento, en el mismo contexto que la
   transacción del consumidor (nunca el evento trae el contenido en la carga, BR-NUC-75: la
   detección entera son las dos variantes «con contenido o con referencia» del resumen de la
   tarea, resueltas igual porque el consumidor **siempre** relee del expediente). Crea el
   elemento ``loop.review_queue_item`` (idempotente por ``(organization_id, source_event_id)``,
   PR-LAZ-09) con el plazo ya congelado (``review_deadline_business_days`` vigente al llegar,
   BR-LAZ-145: un cambio de configuración posterior no lo mueve) y aplica el mismo registro a la
   proyección general (``LoopProjectionStore.apply_in_transaction``), que lo deja ``in_review``
   (T02, ya mergeada en ``loop.projection.fold``).
2. **``ReviewQueueService``**: ``GET /loop/review-queue`` (SUP-U05-05, página por clave sobre el
   índice parcial de pendientes) y la resolución humana (SUP-U05-06, ``review_queue.resolve``
   sobre la zona): T05 (confirmada, abre el lazo) o T06 (descartada); registro
   ``review_resolution`` con su ``Label`` y la proyección, en la misma transacción
   (``LoopProjectionStore.write_and_apply``, BR-LAZ-146).
3. **``ReviewQueueReminder``** (tarea periódica ``review_reminders``, horaria, por organización):
   para cada elemento vencido (``review_due_at`` pasado) publica ``review_overdue`` **sin sacarlo
   de la cola** (``state`` sigue ``pending``); U04-H-07: solo si pasaron
   ``reminder_interval_hours`` desde el recordatorio anterior (actualización condicional
   ``UPDATE ... WHERE ... RETURNING``, atómica por fila: dos ejecuciones de la tarea, o una que
   corre dos veces sobre un arrendamiento vencido, dejan como máximo un aviso por ventana).
   ``reminders_sent`` viaja en la carga del evento junto con ``phase`` (``review_overdue_phase``,
   ``ReviewReminderPhase``): ``first`` cuando ``reminders_sent == 1``, ``repeat`` después. El
   catálogo de ``notifications`` (``CatalogEntry`` de ``review_overdue``) usa ``phase`` como
   ``immediate_when``: el primer aviso conserva ``immediate``; las repeticiones bajan a ``normal``
   (el catálogo no puede derivarlo de ``reminders_sent`` solo, porque ``ImmediateWhen`` compara
   contra un conjunto cerrado de valores de cadena, no un contador).

**Motivo de la resolución** (análogo a ``loop.classification``, BLM §1): ``ReviewResolutionOutcome``
solo tiene dos valores (``review_confirmed``, ``review_discarded``, a diferencia de las tres
salidas de la clasificación), así que el mapa de categorías por salida junta, en el descarte, las
dos listas que en la clasificación son salidas distintas (autorizada y falso positivo): ambas
explican por qué la detección no abre el lazo.

**Orden de candados** (el único del lazo, ``loop.application.locks``): la exclusión de la
detección (``lock_finding`` sobre su ``subject_id``, que es ``detection_id``) antes de leer su
estado, igual que la clasificación; la cadena de la planta y el control de la proyección los toma
``write_and_apply`` después, en ese orden. El consumidor no toma ``lock_finding``: una detección
nace una sola vez (T02 parte de «—») y el control de la proyección de la planta ya serializa a
cualquier otro escritor de la misma planta.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound, narrowed
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextRejected,
    FreeTextRejection,
    apply_base_policy,
)
from vigia_platform.loop.adapters.postgres.inbox_query import SessionScope
from vigia_platform.loop.adapters.postgres.review_queue_repository import (
    ReminderCandidate,
    ReviewQueueCursor,
    ReviewQueueItemRow,
    ReviewQueueRepository,
    ReviewSubjectRow,
)
from vigia_platform.loop.application.inbox import session_scope
from vigia_platform.loop.application.locks import lock_finding
from vigia_platform.loop.calendar import business_deadline
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import (
    FindingLifecycleState,
    ReasonCategoryAuthorized,
    ReasonCategoryConfirmed,
    ReasonCategoryFalsePositive,
    ReviewReminderPhase,
    ReviewResolutionOutcome,
)
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    LifecycleSnapshot,
    Rejected,
    ResolveReview,
    transition,
)
from vigia_platform.loop.projection.consumer import RecordReader
from vigia_platform.loop.projection.store import (
    LoopProjectionStore,
    ProjectionRecordMissing,
    projection_input,
)
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent, OutboxEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "DETECTION_FOR_REVIEW_RECEIVED",
    "MAX_REVIEW_QUEUE_PAGE",
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "REVIEW_QUEUE_CONSUMER",
    "REVIEW_QUEUE_CONSUMER_CONCURRENCY",
    "REVIEW_REASONS_BY_OUTCOME",
    "REVIEW_REMINDERS_BATCH_SIZE",
    "REVIEW_REMINDERS_SCHEDULE",
    "REVIEW_REMINDERS_TASK",
    "REVIEW_RESOLUTION",
    "ReviewOverdueEvent",
    "ReviewQueueConsumer",
    "ReviewQueuePage",
    "ReviewQueueReminder",
    "ReviewQueueRequestInvalid",
    "ReviewQueueService",
    "ReviewQueueUnavailable",
    "ReviewResolutionRejected",
    "ReviewResolutionResult",
    "register_review_queue",
    "register_review_reminders",
    "review_overdue_phase",
]

REVIEW_RESOLUTION: Final = "review_resolution"
REVIEW_QUEUE_CONSUMER: Final = "review_queue"
REVIEW_QUEUE_CONSUMER_CONCURRENCY: Final = 2
"""El mamparo del consumidor (TASK-309: «concurrencia 2»)."""
REVIEW_REMINDERS_TASK: Final = "review_reminders"
REVIEW_REMINDERS_SCHEDULE: Final = Schedule.every(3600)
"""Horaria (U04-H-07: el aviso solo sale cuando pasó ``reminder_interval_hours`` desde el
último)."""
REVIEW_REMINDERS_BATCH_SIZE: Final = 200
DETECTION_FOR_REVIEW_RECEIVED: Final = "detection_for_review_received"
MAX_REVIEW_QUEUE_PAGE: Final = 200
"""Página máxima de ``GET /loop/review-queue`` (SUP-U05-05)."""
REASON_MIN_CHARS: Final = 20
REASON_MAX_CHARS: Final = 1000
"""Límites de ``ReasonText`` (``loop.record_types``; DE §2.4), iguales a los de la clasificación."""
SUBJECT_RESOURCE_KIND: Final = "finding"
"""``kind`` del recurso en la auditoría de la denegación: la fila de la bandeja (como en
T03-T20)."""

_REASON_FIELD: Final = FreeTextField(
    REVIEW_RESOLUTION, "/reason_text", REASON_MIN_CHARS, REASON_MAX_CHARS
)

_CONFIRMED_REASONS: Final = frozenset(c.value for c in ReasonCategoryConfirmed)
_DISCARDED_REASONS: Final = frozenset(c.value for c in ReasonCategoryAuthorized) | frozenset(
    c.value for c in ReasonCategoryFalsePositive
)
REVIEW_REASONS_BY_OUTCOME: Final[Mapping[ReviewResolutionOutcome, frozenset[str]]] = (
    MappingProxyType(
        {
            ReviewResolutionOutcome.REVIEW_CONFIRMED: _CONFIRMED_REASONS,
            ReviewResolutionOutcome.REVIEW_DISCARDED: _DISCARDED_REASONS,
        }
    )
)
"""La lista de motivos de cada salida de la resolución: ``review_discarded`` junta las dos listas
que en la clasificación son salidas distintas (autorizada y falso positivo), porque
``ReviewResolutionOutcome`` no las separa (solo dos valores, a diferencia de las tres de
``ClassificationOutcome``)."""

_EVENT_BY_OUTCOME: Final[Mapping[ReviewResolutionOutcome, str]] = MappingProxyType(
    {
        ReviewResolutionOutcome.REVIEW_CONFIRMED: "review_confirmed",
        ReviewResolutionOutcome.REVIEW_DISCARDED: "review_discarded",
    }
)

_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = MappingProxyType(
    {
        LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
        LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: (
            LoopDetailCode.PERSON_ATTRIBUTION_DETECTED
        ),
    }
)

_PLANT_TIMEZONE: Final = text(
    "SELECT timezone FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_LABEL: Final = text(
    "SELECT label_id FROM ledger.label"
    " WHERE organization_id = :organization_id AND source_record_id = :record_id"
)
_INSERT_RESOLUTION: Final = text(
    "INSERT INTO loop.review_resolution (resolution_id, organization_id, plant_id,"
    " zone_id, detection_id, record_id, outcome, reason_category, reason_text,"
    " resolved_by, resolved_at, label_id, correlation_id) VALUES (:resolution_id,"
    " :organization_id, :plant_id, :zone_id, :detection_id, :record_id, :outcome,"
    " :reason_category, :reason_text, CAST(:resolved_by AS jsonb), :resolved_at,"
    " :label_id, :correlation_id)"
)


class ReviewQueueRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str = "cuerpo inválido") -> None:
        super().__init__(reason)


class ReviewResolutionRejected(Exception):
    """Un rechazo de negocio de la resolución, con su ``detail_code`` (LoopDetailCode)."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class ReviewQueueUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _outcome(value: object) -> ReviewResolutionOutcome:
    if not isinstance(value, str):
        raise ReviewQueueRequestInvalid("la salida es una cadena de la lista cerrada")
    try:
        return ReviewResolutionOutcome(value)
    except ValueError:
        raise ReviewQueueRequestInvalid("salida fuera de la lista cerrada") from None


def _reason(
    outcome: ReviewResolutionOutcome, reason_category: object, reason_text: object
) -> tuple[str, str]:
    """La categoría de la lista de la salida y el texto en NFC de 20 a 1000 caracteres."""
    if reason_category is None or reason_text is None:
        raise ReviewResolutionRejected(LoopDetailCode.REASON_REQUIRED)
    if not isinstance(reason_category, str) or not isinstance(reason_text, str):
        raise ReviewQueueRequestInvalid("la categoría y el motivo son cadenas")
    if not reason_category.strip() or not reason_text.strip():
        raise ReviewResolutionRejected(LoopDetailCode.REASON_REQUIRED)
    if reason_category not in REVIEW_REASONS_BY_OUTCOME[outcome]:
        raise ReviewQueueRequestInvalid("categoría fuera de la lista de la salida")
    if len(reason_text.strip()) < REASON_MIN_CHARS:
        raise ReviewResolutionRejected(LoopDetailCode.REASON_TOO_SHORT)
    try:
        normalized = apply_base_policy(reason_text, _REASON_FIELD)
    except FreeTextRejected as rejected:
        if rejected.reason == FreeTextRejection.TOO_SHORT:
            raise ReviewResolutionRejected(LoopDetailCode.REASON_TOO_SHORT) from None
        raise ReviewQueueRequestInvalid("motivo rechazado por la política base") from None
    return reason_category, normalized


def _snapshot(subject: ReviewSubjectRow) -> LifecycleSnapshot:
    previous = subject.previous_lifecycle_state
    return LifecycleSnapshot(
        state=FindingLifecycleState(subject.lifecycle_state),
        previous_state=None if previous is None else FindingLifecycleState(previous),
        due_at=subject.due_at,
    )


def _written(written: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(written.detail_code or "")
            if code is not None:
                raise ReviewResolutionRejected(code)
            raise ReviewQueueRequestInvalid("texto libre rechazado por el expediente")
        if written.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise ReviewQueueUnavailable("ledger_chain")
        raise RuntimeError(f"review_resolution rechazado: {written.code.value}")
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("review_resolution con un identificador ya usado")
    return written


# --- consumidor review_queue -----------------------------------------------------------------


@repository
class ReviewQueueConsumer:
    """El manejador de ``review_queue`` (``ConsumerHandler``): crea el elemento de la cola y
    aplica la proyección general, los dos en la transacción del despachador."""

    def __init__(
        self,
        *,
        store: LoopProjectionStore,
        reader: RecordReader,
        repository: ReviewQueueRepository,
        configuration: PostgresLoopConfigurationRepository,
    ) -> None:
        self._store = store
        self._reader = reader
        self._repository = repository
        self._configuration = configuration

    def __repr__(self) -> str:
        return "ReviewQueueConsumer()"

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        await self.consume(event, transaction)

    async def consume(self, event: OutboxEvent, transaction: Transaction) -> bool:
        """``True`` si creó el elemento (``False`` si ya existía: reentrega o dos trabajadores a
        la vez entregando la misma detección, PR-LAZ-09)."""
        record_id = _uuid(event.payload["platform_record_id"])
        view = await self._reader.get(
            transaction.context, record_id, record_types=(DETECTION_FOR_REVIEW_RECEIVED,)
        )
        if view is None:
            raise ProjectionRecordMissing(str(record_id))
        item = projection_input(view, event.event_id)
        if item.plant_id is None:
            raise ProjectionRecordMissing(str(record_id))
        timezone = await self._plant_timezone(transaction, item.plant_id)
        settings, _version = await self._configuration.settings_in(transaction, item.plant_id)
        review_due_at = business_deadline(
            item.received_at,
            settings["review_deadline_business_days"],
            settings.business_calendar,
            timezone,
        )
        inserted = await self._repository.insert_pending(
            transaction,
            item,
            event_id=event.event_id,
            review_due_at=review_due_at,
            review_reason=str(event.payload["review_reason"]),
        )
        await self._store.apply_in_transaction(transaction, item)
        return inserted

    @staticmethod
    async def _plant_timezone(transaction: Transaction, plant_id: uuid.UUID) -> str:
        row = (
            await transaction.execute(
                _PLANT_TIMEZONE,
                {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
            )
        ).first()
        if row is None:  # pragma: no cover - la ingesta ya validó la planta del certificado
            raise ProjectionRecordMissing(str(plant_id))
        return str(row.timezone)


def register_review_queue(registry: ConsumerRegistry, consumer: ReviewQueueConsumer) -> Consumer:
    """Registra ``review_queue`` (U-04, sin dependencia externa: lee del expediente en proceso)."""
    return registry.register(
        Consumer(
            consumer_name=REVIEW_QUEUE_CONSUMER,
            unit=ActorUnit.U04,
            subscribed_events=(DETECTION_FOR_REVIEW_RECEIVED,),
            handler=consumer,
        )
    )


# --- SUP-U05-05: listar ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewQueuePage:
    items: tuple[ReviewQueueItemRow, ...]
    next_cursor: ReviewQueueCursor | None


# --- SUP-U05-06: resolver ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewResolutionResult:
    """El recibo de la resolución y el estado nuevo del sujeto (SUP-U05-06)."""

    detection_id: uuid.UUID
    resolution_id: uuid.UUID
    record_id: uuid.UUID
    outcome: ReviewResolutionOutcome
    reason_category: str
    lifecycle_state: FindingLifecycleState
    resolved_at: datetime


@repository
class ReviewQueueService:
    """``loop.review_queue``: listar la cola y resolver un elemento (SUP-U05-05 y 06)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        repository: ReviewQueueRepository,
        clock: Clock,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._store = store if store is not None else LoopProjectionStore()
        self._repository = repository
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ReviewQueueService()"

    # --- SUP-U05-05 --------------------------------------------------------------------------

    async def page(
        self,
        context: ScopeContext,
        *,
        after: ReviewQueueCursor | None = None,
        limit: int = MAX_REVIEW_QUEUE_PAGE,
    ) -> ReviewQueuePage:
        """La página de elementos pendientes visibles, por ``review_due_at`` ascendente."""
        if type(limit) is not int or not 1 <= limit <= MAX_REVIEW_QUEUE_PAGE:
            raise ReviewQueueRequestInvalid("limit")
        organization_id = context.organization_id
        reduced = narrowed(
            context,
            PermissionKey.FINDINGS_READ,
            provider_organization_id=self._authorizer.provider_organization_id,
        )
        if reduced is None:
            await self._authorizer.deny(
                context, PermissionKey.FINDINGS_READ, Resource.organization(organization_id)
            )
        scope: SessionScope = session_scope(reduced)
        async with self._database.transaction(reduced) as transaction:
            rows = await self._repository.pending_page(
                transaction,
                organization_id=organization_id,
                organization_wide=scope.organization_wide,
                plant_ids=scope.plant_ids,
                zone_ids=scope.zone_ids,
                after=after,
                limit=limit + 1,
            )
        more = len(rows) > limit
        items = tuple(rows[:limit])
        return ReviewQueuePage(
            items=items, next_cursor=items[-1].cursor if more and items else None
        )

    # --- SUP-U05-06 --------------------------------------------------------------------------

    async def resolve(
        self,
        context: ScopeContext,
        detection_id: uuid.UUID,
        *,
        outcome: object,
        reason_category: object,
        reason_text: object,
    ) -> ReviewResolutionResult:
        """Resuelve ``detection_id`` (T05 o T06); el recibo y el estado nuevo del sujeto."""
        authorized, subject = await self._authorized(context, detection_id)
        chosen = _outcome(outcome)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, subject.organization_id, subject.detection_id)
            current = await self._repository.subject_for_resolution(
                transaction, subject.organization_id, detection_id
            )
            if current is None or current.finding_state_id != subject.finding_state_id:
                raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
            result = transition(_snapshot(current), ResolveReview(chosen))
            if isinstance(result, Rejected):
                raise ReviewResolutionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            category, reason = _reason(chosen, reason_category, reason_text)
            at = to_millisecond(self._clock.now())
            return await self._write(
                transaction, writer_context, current, result, chosen, category, reason, at
            )

    async def _authorized(
        self, context: ScopeContext, detection_id: uuid.UUID
    ) -> tuple[ScopeContext, ReviewSubjectRow]:
        """La fila de la organización y ``review_queue.resolve`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(detection_id) is not uuid.UUID:
            raise ResourceNotFound()
        subject = await self._repository.subject_for_authorization(context, detection_id)
        if subject is None:
            raise ResourceNotFound()
        resource = Resource(
            subject.organization_id,
            SUBJECT_RESOURCE_KIND,
            subject.finding_state_id,
            plant_id=subject.plant_id,
            zone_id=subject.zone_id,
        )
        authorized = await self._authorizer.authorize(
            context, PermissionKey.REVIEW_QUEUE_RESOLVE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.REVIEW_QUEUE_RESOLVE, resource)
        return authorized, subject

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: ReviewSubjectRow,
        accepted: Accepted,
        outcome: ReviewResolutionOutcome,
        category: str,
        reason: str,
        at: datetime,
    ) -> ReviewResolutionResult:
        resolution_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        actor = context.actor
        if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
            raise ResourceNotFound()
        resolved_by = {
            "user_id": str(actor.id),
            "display_name": actor.display_name_snapshot,
            "role_in_use": actor.role_in_use.value,
        }
        content: dict[str, Any] = {
            "resolution_id": str(resolution_id),
            "detection_id": str(current.detection_id),
            "anchor_record_id": str(current.anchor_record_id),
            "outcome": outcome.value,
            "reason_category": category,
            "reason_text": reason,
            "max_confidence": current.max_confidence,
            "review_threshold": current.review_threshold,
            "publication_threshold": current.publication_threshold,
            "review_reason": current.review_reason,
            "family": current.family,
            "standard": {
                "standard_id": str(current.standard_id),
                "version": current.standard_version,
            },
            "zone_id": str(current.zone_id),
            "resolved_by": resolved_by,
            "resolved_at": format_timestamp(at),
        }
        event_name = _EVENT_BY_OUTCOME[outcome]
        events = (
            NewEvent(
                event_name=event_name,
                payload={
                    "zone_id": str(current.zone_id),
                    "detection_id": str(current.detection_id),
                    "resolution_id": str(resolution_id),
                    "record_id": str(record_id),
                    "family": current.family,
                },
            ),
        )
        written, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            REVIEW_RESOLUTION,
            content,
            scope=RecordScope(plant_id=current.plant_id, zone_id=current.zone_id),
            events=events,
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(written)
        finding = step.finding
        if not step.applied or finding is None:  # pragma: no cover - la transición ya se comprobó
            raise RuntimeError("la proyección no aplicó la resolución recién escrita")
        if not await self._repository.mark_resolved(
            transaction, current.organization_id, current.detection_id, resolved_at=at
        ):  # pragma: no cover - la exclusión del sujeto lo impide
            raise RuntimeError("el elemento de la cola ya no estaba pendiente")
        await self._history(transaction, context, current, content, receipt, at)
        return ReviewResolutionResult(
            detection_id=current.detection_id,
            resolution_id=resolution_id,
            record_id=receipt.record_id,
            outcome=outcome,
            reason_category=category,
            lifecycle_state=finding.state,
            resolved_at=at,
        )

    @staticmethod
    async def _history(
        transaction: Transaction,
        context: ScopeContext,
        current: ReviewSubjectRow,
        content: Mapping[str, Any],
        receipt: Receipt,
        at: datetime,
    ) -> None:
        """La fila de ``loop.review_resolution`` (el historial) con la ``Label`` proyectada."""
        labels = (
            await transaction.execute(
                _LABEL,
                {"organization_id": current.organization_id, "record_id": receipt.record_id},
            )
        ).all()
        if len(labels) != 1:
            raise RuntimeError("la resolución no proyectó exactamente una etiqueta")
        await transaction.execute(
            _INSERT_RESOLUTION,
            {
                "resolution_id": _uuid(content["resolution_id"]),
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "detection_id": current.detection_id,
                "record_id": receipt.record_id,
                "outcome": content["outcome"],
                "reason_category": content["reason_category"],
                "reason_text": content["reason_text"],
                "resolved_by": json.dumps(content["resolved_by"]),
                "resolved_at": at,
                "label_id": _uuid(labels[0].label_id),
                "correlation_id": context.correlation_id,
            },
        )


# --- tarea periódica review_reminders -----------------------------------------------------------


def review_overdue_phase(reminders_sent: int) -> ReviewReminderPhase:
    """``first`` en el primer aviso (``reminders_sent == 1``); ``repeat`` en los siguientes."""
    return ReviewReminderPhase.FIRST if reminders_sent == 1 else ReviewReminderPhase.REPEAT


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewOverdueEvent:
    """Lo que ``ReviewQueueReminder`` publica por cada aviso (carga de ``review_overdue``)."""

    zone_id: uuid.UUID
    detection_id: uuid.UUID
    review_due_at: datetime
    reminders_sent: int
    phase: ReviewReminderPhase


@repository
class ReviewQueueReminder:
    """``review_reminders``: un recordatorio por ventana, sin sacar nada de la cola (U04-H-07)."""

    def __init__(
        self,
        *,
        repository: ReviewQueueRepository,
        configuration: PostgresLoopConfigurationRepository,
        outbox: OutboxPort,
        clock: Clock,
        batch_size: int = REVIEW_REMINDERS_BATCH_SIZE,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._repository = repository
        self._configuration = configuration
        self._outbox = outbox
        self._clock = clock
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "ReviewQueueReminder()"

    async def remind(self, transaction: Transaction) -> tuple[ReviewOverdueEvent, ...]:
        """Una pasada sobre los elementos vencidos de la organización de ``transaction``."""
        organization_id = transaction.context.organization_id
        now = to_millisecond(self._clock.now())
        sent: list[ReviewOverdueEvent] = []
        after: ReviewQueueCursor | None = None
        while True:
            candidates = await self._repository.due_candidates(
                transaction,
                organization_id=organization_id,
                now=now,
                after=after,
                limit=self._batch_size,
            )
            if candidates:
                sent.extend(await self._remind_batch(transaction, organization_id, now, candidates))
                after = ReviewQueueCursor(
                    review_due_at=candidates[-1].review_due_at,
                    detection_id=candidates[-1].detection_id,
                )
            if len(candidates) < self._batch_size:
                return tuple(sent)

    async def _remind_batch(
        self,
        transaction: Transaction,
        organization_id: uuid.UUID,
        now: datetime,
        candidates: list[ReminderCandidate],
    ) -> list[ReviewOverdueEvent]:
        by_plant: dict[uuid.UUID, list[uuid.UUID]] = {}
        for candidate in candidates:
            by_plant.setdefault(candidate.plant_id, []).append(candidate.detection_id)
        sent: list[ReviewOverdueEvent] = []
        for plant_id, detection_ids in by_plant.items():
            settings, _version = await self._configuration.settings_in(transaction, plant_id)
            cutoff = now - timedelta(hours=settings["reminder_interval_hours"])
            reminded = await self._repository.send_reminders(
                transaction,
                organization_id=organization_id,
                plant_id=plant_id,
                detection_ids=detection_ids,
                now=now,
                cutoff=cutoff,
            )
            for row in reminded:
                phase = review_overdue_phase(row.reminders_sent)
                await self._outbox.publish(
                    transaction,
                    NewEvent(
                        event_name="review_overdue",
                        payload={
                            "zone_id": str(row.zone_id),
                            "detection_id": str(row.detection_id),
                            "review_due_at": format_timestamp(row.review_due_at),
                            "reminders_sent": row.reminders_sent,
                            "phase": phase.value,
                        },
                        plant_id=plant_id,
                    ),
                )
                sent.append(
                    ReviewOverdueEvent(
                        zone_id=row.zone_id,
                        detection_id=row.detection_id,
                        review_due_at=row.review_due_at,
                        reminders_sent=row.reminders_sent,
                        phase=phase,
                    )
                )
        return sent


def register_review_reminders(
    registry: PeriodicTaskRegistry, reminder: ReviewQueueReminder
) -> PeriodicTask:
    """Registra ``review_reminders`` (horaria, por organización, TASK-309)."""

    async def handler(transaction: Transaction) -> None:
        await reminder.remind(transaction)

    return registry.register(
        REVIEW_REMINDERS_TASK,
        REVIEW_REMINDERS_SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )
