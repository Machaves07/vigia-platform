"""``loop.classification`` (LC-LAZ-07; S-PLA-10; SUP-U05-03): clasificar y reclasificar un hallazgo.

``classify(context, finding_state_id, ...)`` es ``clasificar`` de business-logic-model.md §1, en el
orden de sus pasos:

1. **Alcance**: la fila de la proyección del sujeto (hallazgo o detección confirmada) de la
   organización y ``findings.classify`` sobre **su zona** (BR-NUC-13). Inexistente, de otra
   organización, fuera de alcance o bajo concesión de proveedor: ``ResourceNotFound``
   (``not_found``, BR-LAZ-01), sin escribir nada.
2. **En una sola transacción** (BR-LAZ-146), tras la exclusión del sujeto: el estado vigente de la
   proyección y la transición de §4.1 (``lifecycle.transition``). Si no existe para la salida
   pedida, ``transition_not_allowed`` (BR-LAZ-36).
3. **Motivo**: categoría de la lista cerrada de la salida (DE §1) y texto de 20 a 1000 caracteres.
   Sin categoría o sin texto (ausente o en blanco), ``reason_required``; más corto,
   ``reason_too_short``; una categoría de otra lista o un texto más largo o con caracteres que la
   política base rechaza, ``invalid_request`` sin ``detail_code`` (esquema).
4. **Motivo reforzado**: toda reclasificación (``sequence_in_subject > 1``) exige
   ``reinforced_reason = true``; si no, ``reason_required`` (BR-LAZ-23). El registro guarda
   ``reinforced_reason`` verdadero exactamente en las reclasificaciones: en la primera
   clasificación no hay motivo que reforzar y la declaración se ignora.
5. El contenido ``classification`` (DE §5.2) con ``effect_on_loop`` de la transición y los eventos
   de su fila de §4.1 (``finding_confirmed``, ``finding_discarded``, ``finding_reclassified``,
   ``loop_suspended`` o ``loop_resumed``), que citan el ``record_id`` del registro.
6. ``LoopProjectionStore.write_and_apply``: ``EscritorExpediente`` aplica el esquema estricto y la
   política de texto libre **con los dos validadores de U-04** antes del hash (un rechazo es
   ``blocked_vocabulary_term`` o ``person_attribution_detected``), proyecta **exactamente una**
   ``Label`` (``label_rule`` de DE §5.3) y publica el evento; en la misma transacción se aplica la
   transición a ``loop.finding_state``.
7. La fila de ``loop.classification`` (DE §2.3, el historial; ninguna fila se edita) con la
   ``Label`` proyectada. Si cualquier paso falla, la transacción se revierte: ni registro, ni
   etiqueta, ni evento, ni proyección.

Reclasificar **nunca toca** el registro anterior (P4, BR-LAZ-20): es otro registro con la secuencia
siguiente. Con el lazo abierto lo suspende (T15) y al volver a confirmado lo reanuda (T16); sobre un
cerrado (T19) o un incerrable (T20) el cierre o la declaración quedan intactos y la proyección
marca ``closed_with_later_different_classification`` (BR-LAZ-25).

**Orden de candados** (el de TASK-311: el hallazgo, después la cadena de la planta; AGENTS.md):

1. la exclusión del sujeto (``pg_advisory_xact_lock`` sobre
   ``loop_subject|<organización>|<sujeto>``), antes de leer su estado;
2. la cadena de la planta (la toma el disparador del expediente al anexar);
3. el control de la proyección de la planta y las filas del sujeto (``LoopProjectionStore``).

Toda operación de U-04 que escriba sobre un sujeto toma la exclusión del sujeto **primero**; la vía
de eventos (sin persona) solo toma 3, después de nadie, así que no hay ciclo. Dos clasificaciones
simultáneas del mismo sujeto se ordenan: la segunda lee el estado que dejó la primera y escribe la
secuencia siguiente, o recibe ``transition_not_allowed``.

**Ocupación unipersonal** (BR-LAZ-06): el registro guarda la marca exacta; la que se devuelve se
agrega a la ventana de la zona (``loop.domain.time_aggregation``).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
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
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    EffectOnLoop,
    FindingLifecycleState,
    ReasonCategoryAuthorized,
    ReasonCategoryConfirmed,
    ReasonCategoryFalsePositive,
)
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    Classify,
    LifecycleCode,
    LifecycleSnapshot,
    Rejected,
    transition,
)
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLASSIFICATION",
    "EFFECT_BY_TRANSITION",
    "REASONS_BY_OUTCOME",
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "SUBJECT_RESOURCE_KIND",
    "ClassificationRejected",
    "ClassificationRequestInvalid",
    "ClassificationResult",
    "ClassificationService",
    "ClassificationUnavailable",
]

CLASSIFICATION: Final = "classification"
REASON_MIN_CHARS: Final = 20
REASON_MAX_CHARS: Final = 1000
"""Límites de ``ReasonText`` (``loop.record_types``; DE §2.3)."""
SUBJECT_RESOURCE_KIND: Final = "finding"
"""``kind`` del recurso en la auditoría de la denegación: la fila de la bandeja."""

_REASON_FIELD: Final = FreeTextField(
    CLASSIFICATION, "/reason_text", REASON_MIN_CHARS, REASON_MAX_CHARS
)

REASONS_BY_OUTCOME: Final[Mapping[ClassificationOutcome, frozenset[str]]] = MappingProxyType(
    {
        ClassificationOutcome.CONFIRMED: frozenset(c.value for c in ReasonCategoryConfirmed),
        ClassificationOutcome.AUTHORIZED_OPERATION: frozenset(
            c.value for c in ReasonCategoryAuthorized
        ),
        ClassificationOutcome.FALSE_POSITIVE: frozenset(
            c.value for c in ReasonCategoryFalsePositive
        ),
    }
)
"""La lista cerrada de categorías de cada salida (DE §1; hipótesis U04-H-12)."""

EFFECT_BY_TRANSITION: Final[Mapping[str, EffectOnLoop]] = MappingProxyType(
    {
        "T03": EffectOnLoop.LOOP_OPENED,
        "T04": EffectOnLoop.NONE,
        "T15": EffectOnLoop.LOOP_SUSPENDED,
        "T16": EffectOnLoop.LOOP_RESUMED,
        "T17": EffectOnLoop.LOOP_OPENED,
        "T18": EffectOnLoop.NONE,
        "T19": EffectOnLoop.CLOSED_MARKED,
        "T20": EffectOnLoop.CLOSED_MARKED,
    }
)
"""``effect_on_loop`` de cada transición de una clasificación (BLM §1, paso 5)."""

_LOCK_SUBJECT: Final = text(
    "SELECT pg_advisory_xact_lock("
    "hashtextextended('loop_subject|' || :organization_id || '|' || :subject_id, 0))"
)
_SUBJECT: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, family, standard_id, standard_version, lifecycle_state,"
    " previous_lifecycle_state, current_classification_outcome, classification_count, due_at,"
    " observability_state_at_event, single_occupancy_zone, aggregation_window_minutes"
    " FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_LABEL: Final = text(
    "SELECT label_id FROM ledger.label"
    " WHERE organization_id = :organization_id AND source_record_id = :record_id"
)
_PREVIOUS: Final = text(
    "SELECT record_id FROM loop.classification"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_id = :subject_id AND sequence_in_subject = :sequence"
)
_INSERT: Final = text(
    "INSERT INTO loop.classification (classification_id, organization_id, plant_id, zone_id,"
    " subject_kind, subject_id, record_id, sequence_in_subject, outcome, reason_category,"
    " reason_text, is_reclassification, reinforced_reason, previous_outcome, effect_on_loop,"
    " classified_by, classified_at, label_id, correlation_id) VALUES (:classification_id,"
    " :organization_id, :plant_id, :zone_id, :subject_kind, :subject_id, :record_id, :sequence,"
    " :outcome, :reason_category, :reason_text, :is_reclassification, :reinforced_reason,"
    " :previous_outcome, :effect_on_loop, CAST(:classified_by AS jsonb), :classified_at,"
    " :label_id, :correlation_id)"
)

_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = MappingProxyType(
    {
        LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
        LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: (
            LoopDetailCode.PERSON_ATTRIBUTION_DETECTED
        ),
    }
)
"""Los ``detail_code`` de los dos validadores de U-04 que un rechazo del escritor conserva."""


class ClassificationRejected(Exception):
    """Un rechazo de negocio de la clasificación, con su ``detail_code`` (LoopDetailCode)."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class ClassificationRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class ClassificationUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class _Subject:
    """Lo que la clasificación necesita de la fila de la proyección."""

    finding_state_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    anchor_record_id: uuid.UUID
    family: str
    standard_id: uuid.UUID
    standard_version: int
    state: FindingLifecycleState
    previous_state: FindingLifecycleState | None
    current_outcome: ClassificationOutcome | None
    classification_count: int
    due_at: datetime | None
    observability_state_at_event: str
    single_occupancy_zone: bool
    aggregation_window_minutes: int

    @property
    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(self.state, self.previous_state, self.due_at)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """El recibo de la clasificación y el estado nuevo del sujeto (SUP-U05-03)."""

    finding_state_id: uuid.UUID
    classification_id: uuid.UUID
    record_id: uuid.UUID
    sequence_in_subject: int
    outcome: ClassificationOutcome
    reason_category: str
    is_reclassification: bool
    reinforced_reason: bool
    effect_on_loop: EffectOnLoop
    transition_id: str
    lifecycle_state: FindingLifecycleState
    classification_count: int
    closed_with_later_different_classification: bool
    classified_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    time_aggregated: bool


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _subject(row: Any) -> _Subject:
    data = row._mapping
    previous = data["previous_lifecycle_state"]
    outcome = data["current_classification_outcome"]
    return _Subject(
        finding_state_id=_uuid(data["finding_state_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        subject_kind=str(data["subject_kind"]),
        subject_id=_uuid(data["subject_id"]),
        anchor_record_id=_uuid(data["anchor_record_id"]),
        family=str(data["family"]),
        standard_id=_uuid(data["standard_id"]),
        standard_version=int(data["standard_version"]),
        state=FindingLifecycleState(data["lifecycle_state"]),
        previous_state=None if previous is None else FindingLifecycleState(previous),
        current_outcome=None if outcome is None else ClassificationOutcome(outcome),
        classification_count=int(data["classification_count"]),
        due_at=data["due_at"],
        observability_state_at_event=str(data["observability_state_at_event"]),
        single_occupancy_zone=bool(data["single_occupancy_zone"]),
        aggregation_window_minutes=int(data["aggregation_window_minutes"]),
    )


def _outcome(value: object) -> ClassificationOutcome:
    if not isinstance(value, str):
        raise ClassificationRequestInvalid("la salida es una cadena de la lista cerrada")
    try:
        return ClassificationOutcome(value)
    except ValueError:
        raise ClassificationRequestInvalid("salida fuera de la lista cerrada") from None


def _reason(
    outcome: ClassificationOutcome, reason_category: object, reason_text: object
) -> tuple[str, str]:
    """Paso 3: la categoría de la lista de la salida y el texto en NFC de 20 a 1000."""
    if reason_category is None or reason_text is None:
        raise ClassificationRejected(LoopDetailCode.REASON_REQUIRED)
    if not isinstance(reason_category, str) or not isinstance(reason_text, str):
        raise ClassificationRequestInvalid("la categoría y el motivo son cadenas")
    if not reason_category.strip() or not reason_text.strip():
        raise ClassificationRejected(LoopDetailCode.REASON_REQUIRED)
    if reason_category not in REASONS_BY_OUTCOME[outcome]:
        raise ClassificationRequestInvalid("categoría fuera de la lista de la salida")
    if len(reason_text.strip()) < REASON_MIN_CHARS:
        raise ClassificationRejected(LoopDetailCode.REASON_TOO_SHORT)
    try:
        normalized = apply_base_policy(reason_text, _REASON_FIELD)
    except FreeTextRejected as rejected:
        if rejected.reason == FreeTextRejection.TOO_SHORT:
            raise ClassificationRejected(LoopDetailCode.REASON_TOO_SHORT) from None
        raise ClassificationRequestInvalid("motivo rechazado por la política base") from None
    return reason_category, normalized


def _written(written: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(written.detail_code or "")
            if code is not None:
                raise ClassificationRejected(code)
            raise ClassificationRequestInvalid("texto libre rechazado por el expediente")
        if written.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise ClassificationUnavailable("ledger_chain")
        raise RuntimeError(f"classification rechazado: {written.code.value}")
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("classification con un identificador ya usado")
    return written


@repository
class ClassificationService:
    """``loop.classification``: ``clasificar`` (y reclasificar) un hallazgo o detección."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._store = store if store is not None else LoopProjectionStore()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ClassificationService()"

    async def classify(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        outcome: object,
        reason_category: object,
        reason_text: object,
        reinforced_reason: bool,
    ) -> ClassificationResult:
        """Clasifica (o reclasifica) el sujeto ``finding_state_id``; el recibo y el estado nuevo."""
        authorized, subject = await self._authorized(context, finding_state_id)
        chosen = _outcome(outcome)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await transaction.execute(
                _LOCK_SUBJECT,
                {
                    "organization_id": str(subject.organization_id),
                    "subject_id": str(subject.subject_id),
                },
            )
            current = await self._current(transaction, finding_state_id)
            if current is None or current.subject_id != subject.subject_id:
                raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
            at = to_millisecond(self._clock.now())
            if isinstance(transition(current.snapshot, Classify(chosen, True, at)), Rejected):
                raise ClassificationRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            category, reason = _reason(chosen, reason_category, reason_text)
            accepted = self._accepted(current, chosen, reinforced_reason, at)
            return await self._write(
                transaction, writer_context, current, accepted, chosen, category, reason, at
            )

    # --- pasos -----------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, _Subject]:
        """Paso 1: la fila de la organización y ``findings.classify`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _SUBJECT,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        subject = _subject(rows[0])
        resource = Resource(
            subject.organization_id,
            SUBJECT_RESOURCE_KIND,
            subject.finding_state_id,
            plant_id=subject.plant_id,
            zone_id=subject.zone_id,
        )
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FINDINGS_CLASSIFY, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.FINDINGS_CLASSIFY, resource)
        return authorized, subject

    @staticmethod
    async def _current(transaction: Transaction, finding_state_id: uuid.UUID) -> _Subject | None:
        result = await transaction.execute(
            _SUBJECT,
            {
                "organization_id": transaction.context.organization_id,
                "finding_state_id": finding_state_id,
            },
        )
        row = result.first()
        return None if row is None else _subject(row)

    @staticmethod
    def _accepted(
        current: _Subject, outcome: ClassificationOutcome, reinforced: bool, at: datetime
    ) -> Accepted:
        """Paso 4: con la transición ya comprobada, el motivo reforzado de la reclasificación."""
        result = transition(current.snapshot, Classify(outcome, reinforced, at))
        if isinstance(result, Rejected):
            if result.code is LifecycleCode.REASON_REQUIRED:
                raise ClassificationRejected(LoopDetailCode.REASON_REQUIRED)
            raise ClassificationRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)  # pragma: no cover
        return result

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: _Subject,
        accepted: Accepted,
        outcome: ClassificationOutcome,
        category: str,
        reason: str,
        at: datetime,
    ) -> ClassificationResult:
        """Pasos 5 a 7: contenido, escritura con su proyección y la fila del historial."""
        sequence = current.classification_count + 1
        is_reclassification = sequence > 1
        transition_id = accepted.transition.transition_id
        effect = EFFECT_BY_TRANSITION[transition_id]
        classification_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        actor = context.actor
        if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
            raise ResourceNotFound()
        signer = {
            "user_id": str(actor.id),
            "display_name": actor.display_name_snapshot,
            "role_in_use": actor.role_in_use.value,
        }
        previous_record = (
            await self._previous_record(transaction, current) if is_reclassification else None
        )
        content: dict[str, Any] = {
            "classification_id": str(classification_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "anchor_record_id": str(current.anchor_record_id),
            "sequence_in_subject": sequence,
            "outcome": outcome.value,
            "reason_category": category,
            "reason_text": reason,
            "is_reclassification": is_reclassification,
            "reinforced_reason": is_reclassification,
            "effect_on_loop": effect.value,
            "standard": {
                "standard_id": str(current.standard_id),
                "version": current.standard_version,
            },
            "family": current.family,
            "observability_state_at_event": current.observability_state_at_event,
            "zone_id": str(current.zone_id),
            "signer": signer,
            "classified_at": format_timestamp(at),
        }
        if is_reclassification and current.current_outcome is not None:
            content["previous_outcome"] = current.current_outcome.value
        if previous_record is not None:
            content["previous_classification_record_id"] = str(previous_record)
        events = self._events(accepted, current, outcome, classification_id, record_id)
        written, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            CLASSIFICATION,
            content,
            scope=RecordScope(plant_id=current.plant_id, zone_id=current.zone_id),
            events=events,
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(written)
        finding = step.finding
        if not step.applied or finding is None or step.transition_id != transition_id:
            raise RuntimeError("la proyección no aplicó la clasificación recién escrita")
        await self._history(transaction, context, current, content, receipt, at)
        return ClassificationResult(
            finding_state_id=current.finding_state_id,
            classification_id=classification_id,
            record_id=receipt.record_id,
            sequence_in_subject=sequence,
            outcome=outcome,
            reason_category=category,
            is_reclassification=is_reclassification,
            reinforced_reason=is_reclassification,
            effect_on_loop=effect,
            transition_id=transition_id,
            lifecycle_state=finding.state,
            classification_count=finding.classification_count,
            closed_with_later_different_classification=(
                finding.closed_with_later_different_classification
            ),
            classified_at=at,
            presented_at=presented(
                at,
                single_occupancy=current.single_occupancy_zone,
                window_minutes=current.aggregation_window_minutes,
            ),
            time_aggregated=current.single_occupancy_zone,
        )

    @staticmethod
    def _events(
        accepted: Accepted,
        current: _Subject,
        outcome: ClassificationOutcome,
        classification_id: uuid.UUID,
        record_id: uuid.UUID,
    ) -> tuple[NewEvent, ...]:
        """Los eventos de la fila de §4.1: solo identificadores y valores de listas cerradas."""
        base: dict[str, Any] = {
            "zone_id": str(current.zone_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "classification_id": str(classification_id),
            "record_id": str(record_id),
        }
        events: list[NewEvent] = []
        for name in accepted.transition.events:
            payload = dict(base)
            if name in ("finding_confirmed", "finding_discarded", "finding_reclassified"):
                payload["outcome"] = outcome.value
                payload["family"] = current.family
            if name == "finding_reclassified":
                if current.current_outcome is None:  # pragma: no cover - T18 a T20 ya clasificados
                    raise RuntimeError("una reclasificación sin salida anterior")
                payload["previous_outcome"] = current.current_outcome.value
            events.append(NewEvent(event_name=name, payload=payload))
        return tuple(events)

    @staticmethod
    async def _previous_record(transaction: Transaction, current: _Subject) -> uuid.UUID | None:
        """El registro de la clasificación anterior, si su fila de historial existe."""
        result = await transaction.execute(
            _PREVIOUS,
            {
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "subject_id": current.subject_id,
                "sequence": current.classification_count,
            },
        )
        row = result.first()
        return None if row is None else _uuid(row.record_id)

    @staticmethod
    async def _history(
        transaction: Transaction,
        context: ScopeContext,
        current: _Subject,
        content: Mapping[str, Any],
        receipt: Receipt,
        at: datetime,
    ) -> None:
        """La fila de ``loop.classification`` con la ``Label`` que proyectó el escritor."""
        labels = (
            await transaction.execute(
                _LABEL,
                {"organization_id": current.organization_id, "record_id": receipt.record_id},
            )
        ).all()
        if len(labels) != 1:
            raise RuntimeError("la clasificación no proyectó exactamente una etiqueta")
        await transaction.execute(
            _INSERT,
            {
                "classification_id": _uuid(content["classification_id"]),
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "subject_kind": current.subject_kind,
                "subject_id": current.subject_id,
                "record_id": receipt.record_id,
                "sequence": content["sequence_in_subject"],
                "outcome": content["outcome"],
                "reason_category": content["reason_category"],
                "reason_text": content["reason_text"],
                "is_reclassification": content["is_reclassification"],
                "reinforced_reason": content["reinforced_reason"],
                "previous_outcome": content.get("previous_outcome"),
                "effect_on_loop": content["effect_on_loop"],
                "classified_by": json.dumps(content["signer"]),
                "classified_at": at,
                "label_id": _uuid(labels[0].label_id),
                "correlation_id": context.correlation_id,
            },
        )
