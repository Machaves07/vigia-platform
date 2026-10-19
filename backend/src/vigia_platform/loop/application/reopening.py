"""``loop.lifecycle`` (reapertura, SUP-U05-14; LC-LAZ-09; VIG-233): reabrir un cerrado o un
incerrable con una acción correctiva nueva (T13 y T14 de ``lifecycle.transition``).

``reopen(context, finding_state_id, ...)`` es ``reabrir`` de business-logic-model.md §3, en el
orden de sus pasos:

1. **Alcance**: la fila de la proyección del hallazgo de la organización y ``actions.manage``
   sobre **su zona** (BR-NUC-13, la misma clave que asignar la acción, SUP-U05-07). Inexistente,
   de otra organización, fuera de alcance o bajo concesión de proveedor: ``ResourceNotFound``
   (``not_found``), sin escribir nada.
2. **En una sola transacción** (BR-LAZ-146), tras los candados de ``loop.application.locks`` (el
   hallazgo y después sus acciones): solo desde ``closed`` (T13) o ``unclosable`` (T14); desde
   cualquier otro estado, ``transition_not_allowed`` (BR-LAZ-36).
3. **Motivo**: ``reason_text`` de 20 a 2000 caracteres (``LongText``) en NFC por la política base;
   ausente o en blanco, ``reopening_reason_required`` (BR-LAZ-146bis, DE §2.8).
4. ``reopening_id`` nuevo y el enlace con lo que se reabre, íntegro (P4, BR-LAZ-50): el ``closure``
   anterior (``previous_closure_id``) o la declaración de incerrable anterior
   (``previous_declaration_id``), según ``from_state``; ninguno de los dos se edita ni se retira.
5. ``LoopProjectionStore.write_and_apply`` escribe el registro ``reopening`` y su evento
   ``finding_reopened``; la proyección mueve el hallazgo a ``action_assigned`` con
   ``current_action_id`` fijado al ``new_action_id`` de este registro y ``due_at`` todavía sin
   completar (la segunda mitad de T13 o T14, BLM §3).
6. **La misma acción correctiva** que ``POST /loop/findings/{id}/actions`` (SUP-U05-07): tipo,
   descripción, responsable y plazo, validados y escritos por
   ``ActionService.assign_in_transaction`` con ``pending_action_id=new_action_id`` (el registro
   ``action_assigned`` y su evento, en la misma transacción). ``supersedes_action_id`` es la
   acción que estaba vigente antes del cierre o de la declaración, si la había (``if any``): la
   fila de ``loop.corrective_action`` de esa acción queda ``superseded`` (nunca se borra), igual
   que en una reasignación (BR-LAZ-49).

El cierre o la declaración anteriores **no se tocan**: ``closure_record_id`` y ``closed_at`` (o
``unclosable_since`` y ``unclosable_review_due_at``) de la proyección se limpian porque el
hallazgo vuelve a tener un lazo activo, pero el registro del expediente que los originó es
exactamente el mismo byte a byte (P4, BR-LAZ-50): esta operación nunca lo escribe ni lo toca.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Authorizer, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerDatabase
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected, apply_base_policy
from vigia_platform.loop.application.actions import (
    ActionRequestInvalid,
    ActionRow,
    ActionService,
    AssignmentRequest,
    AssignmentResult,
    LoopFinding,
    action_rows,
    actor_snapshot,
    finding_from,
    read_finding,
    written,
)
from vigia_platform.loop.application.locks import lock_actions, lock_finding
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import Accepted, Rejected, Reopen, transition
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "REOPENING",
    "ReopeningRejected",
    "ReopeningRequestInvalid",
    "ReopeningResult",
    "ReopeningService",
    "ReopeningUnavailable",
]

REOPENING: Final = "reopening"
REASON_MIN_CHARS: Final = 20
REASON_MAX_CHARS: Final = 2000
"""Límites de ``Reopening.reason_text`` (``LongText``; ``loop.record_types``; DE §2.8)."""

S = FindingLifecycleState

_REASON_FIELD: Final = FreeTextField(REOPENING, "/reason_text", REASON_MIN_CHARS, REASON_MAX_CHARS)

_SUBJECT: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, lifecycle_state, previous_lifecycle_state, due_at, current_action_id,"
    " closure_record_id, single_occupancy_zone, aggregation_window_minutes"
    " FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_LATEST_DECLARATION: Final = text(
    "SELECT ledger.vigia_bytes_to_jsonb(content) ->> 'declaration_id' AS declaration_id"
    " FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND record_type = 'unclosable_state_declared'"
    " AND ledger.vigia_bytes_to_jsonb(content) ->> 'subject_id' = :subject_id"
    " ORDER BY chain_sequence DESC LIMIT 1"
)


class ReopeningRejected(Exception):
    """Un rechazo de negocio de la reapertura, con su ``detail_code`` (LoopDetailCode)."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class ReopeningRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class ReopeningUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class ReopeningResult:
    """El recibo de la reapertura y de la acción nueva que la completa (SUP-U05-14)."""

    finding_state_id: uuid.UUID
    reopening_id: uuid.UUID
    record_id: uuid.UUID
    from_state: FindingLifecycleState
    previous_closure_id: uuid.UUID | None
    previous_declaration_id: uuid.UUID | None
    supersedes_action_id: uuid.UUID | None
    """La acción vigente antes del cierre o de la declaración, si la había."""
    reopened_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    time_aggregated: bool
    action: AssignmentResult
    """El recibo de la acción nueva (``action_id`` es ``new_action_id`` de este registro)."""


def _reason(value: object) -> str | None:
    """Paso 3: el motivo en NFC de 20 a 2000; ausente o en blanco, ``None`` (sin motivo)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ActionRequestInvalid("el motivo de la reapertura es una cadena")
    try:
        normalized = apply_base_policy(value, _REASON_FIELD)
    except FreeTextRejected:
        raise ActionRequestInvalid("motivo rechazado por la política base") from None
    if len(normalized.strip()) < _REASON_FIELD.min_length:
        raise ActionRequestInvalid("motivo más corto que su mínimo")
    return normalized


def _rejected(result: Rejected) -> ReopeningRejected:
    # ``LifecycleCode`` y ``LoopDetailCode`` comparten el nombre del miembro, no el valor (el
    # segundo lleva el prefijo ``loop_`` de A-68).
    return ReopeningRejected(LoopDetailCode[result.code.name])


@repository
class ReopeningService:
    """``loop.lifecycle``: reabrir (T13, T14) con una acción correctiva nueva."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        actions: ActionService,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._actions = actions
        self._store = store if store is not None else LoopProjectionStore()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ReopeningService()"

    async def reopen(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        reason_text: object,
        action_type: object,
        action_description: object,
        expected_outcome: object = None,
        assignee_user_id: object,
        deadline_days: object = None,
    ) -> ReopeningResult:
        """Reabre el hallazgo ``finding_state_id`` con la acción correctiva nueva."""
        authorized, place = await self._authorized(context, finding_state_id)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, place.organization_id, place.subject_id)
            actions = action_rows(
                await lock_actions(
                    transaction, place.organization_id, place.plant_id, place.subject_id
                )
            )
            current = await read_finding(transaction, finding_state_id)
            if current is None or current.subject_id != place.subject_id:
                raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
            # Paso 2: el estado, con un motivo ficticio que nunca dispara REOPENING_REASON_REQUIRED
            # (un estado equivocado manda sobre un motivo mal formado, como en ``classification``).
            probe = transition(current.snapshot, Reopen(reason_given=True))
            if isinstance(probe, Rejected):
                raise _rejected(probe)
            # Paso 3: el motivo, ya con el estado comprobado.
            reason = _reason(reason_text)
            accepted = transition(current.snapshot, Reopen(reason_given=reason is not None))
            if isinstance(accepted, Rejected):
                raise _rejected(accepted)
            assert isinstance(accepted, Accepted)  # noqa: S101 - narrows mypy
            if reason is None:  # pragma: no cover - transition ya lo exige
                raise ReopeningRejected(LoopDetailCode.REOPENING_REASON_REQUIRED)
            return await self._write(
                transaction,
                writer_context,
                current,
                actions,
                accepted,
                reason,
                AssignmentRequest(
                    action_type,
                    action_description,
                    expected_outcome,
                    assignee_user_id,
                    deadline_days,
                    None,
                ),
            )

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: LoopFinding,
        actions: Mapping[uuid.UUID, ActionRow],
        accepted: Accepted,
        reason: str,
        request: AssignmentRequest,
    ) -> ReopeningResult:
        from_state = current.state
        previous_action_id = current.current_action_id
        previous_closure_id, previous_declaration_id = await self._previous(transaction, current)
        new_action_id = uuid7(self._clock, self._random_bytes)
        reopening_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        at = to_millisecond(self._clock.now())
        signer = actor_snapshot(context)
        content: dict[str, Any] = {
            "reopening_id": str(reopening_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "from_state": from_state.value,
            "reason_text": reason,
            "new_action_id": str(new_action_id),
            "reopened_by": signer,
            "reopened_at": format_timestamp(at),
        }
        if previous_closure_id is not None:
            content["previous_closure_id"] = str(previous_closure_id)
        if previous_declaration_id is not None:
            content["previous_declaration_id"] = str(previous_declaration_id)
        event = NewEvent(
            event_name="finding_reopened",
            payload={
                "zone_id": str(current.zone_id),
                "subject_kind": current.subject_kind,
                "subject_id": str(current.subject_id),
                "reopening_id": str(reopening_id),
                "record_id": str(record_id),
                "from_state": from_state.value,
                "new_action_id": str(new_action_id),
            },
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            REOPENING,
            content,
            scope=current.scope,
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = written(result, unavailable=ReopeningUnavailable)
        finding = step.finding
        if (
            not step.applied
            or finding is None
            or step.transition_id != accepted.transition.transition_id
        ):
            raise RuntimeError("la proyección no aplicó la reapertura recién escrita")
        updated = await read_finding(transaction, current.finding_state_id)
        if updated is None:  # pragma: no cover - la proyección acaba de escribir la fila
            raise RuntimeError("el hallazgo reabierto no se encuentra en la proyección")
        completed = AssignmentRequest(
            request.action_type,
            request.action_description,
            request.expected_outcome,
            request.assignee_user_id,
            request.deadline_days,
            previous_action_id,
        )
        assignment = await self._actions.assign_in_transaction(
            transaction, context, updated, actions, completed, pending_action_id=new_action_id
        )
        return ReopeningResult(
            finding_state_id=current.finding_state_id,
            reopening_id=reopening_id,
            record_id=receipt.record_id,
            from_state=from_state,
            previous_closure_id=previous_closure_id,
            previous_declaration_id=previous_declaration_id,
            supersedes_action_id=previous_action_id,
            reopened_at=at,
            presented_at=current.presented(at),
            time_aggregated=current.single_occupancy_zone,
            action=assignment,
        )

    async def _previous(
        self, transaction: Transaction, current: LoopFinding
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """El enlace íntegro con lo que se reabre (P4, BR-LAZ-50): nunca se edita ni se retira."""
        if current.state is S.CLOSED:
            row = (
                await transaction.execute(
                    _SUBJECT,
                    {
                        "organization_id": current.organization_id,
                        "finding_state_id": current.finding_state_id,
                    },
                )
            ).first()
            closure_id = None if row is None else row._mapping["closure_record_id"]
            return (None if closure_id is None else uuid.UUID(str(closure_id))), None
        row = (
            await transaction.execute(
                _LATEST_DECLARATION,
                {
                    "organization_id": current.organization_id,
                    "plant_id": current.plant_id,
                    "subject_id": str(current.subject_id),
                },
            )
        ).first()
        declaration = None if row is None or row.declaration_id is None else row.declaration_id
        return None, (None if declaration is None else uuid.UUID(str(declaration)))

    # --- alcance ---------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding]:
        """Paso 1: la fila de la organización y ``actions.manage`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _SUBJECT,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        place = finding_from(rows[0])
        resource = place.resource()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.ACTIONS_MANAGE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.ACTIONS_MANAGE, resource)
        return authorized, place
