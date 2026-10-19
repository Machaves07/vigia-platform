"""``loop.lifecycle`` (cierre, SUP-U05-11; LC-LAZ-09; VIG-233): el cierre firmado del hallazgo.

``close(context, finding_state_id, ...)`` es ``cerrar`` de business-logic-model.md §3 (T09 de
``lifecycle.transition``), en el orden de sus pasos:

1. **Alcance**: la fila de la proyección del hallazgo de la organización y
   ``findings.close_and_sign`` sobre **su zona** (BR-NUC-13). Inexistente, de otra organización,
   fuera de alcance o bajo concesión de proveedor: ``ResourceNotFound`` (``not_found``), sin
   escribir nada.
2. **Textos** (BR-LAZ-42): ``closure_description`` (20 a 2000) y ``verification_note``
   (opcional, hasta 1000) en NFC por la política base; los dos validadores de U-04 los aplica el
   escritor antes del hash.
3. **Firmante**: nombre de la instantánea de la sesión y ``professional_license`` declarada (un
   campo del perfil de U-02, **no verificado externamente**); ausente o en blanco,
   ``signer_license_missing`` (F04, BR-LAZ-43). ``closure_description`` y ``verification_note`` no
   dependen de que el almacén de adjuntos esté arriba: esta ruta no sube ni verifica ningún
   adjunto nuevo (FS-LAZ-03, NFR-LAZ-37); solo cita, por su suma, los que ya se verificaron al
   añadir evidencia (TASK-310).
4. **En una sola transacción** (BR-LAZ-146), tras los candados de ``loop.application.locks`` (el
   hallazgo y después sus acciones): el estado vigente, con la transición de §4.1
   (``lifecycle.transition``, ``Close``):

   - desde ``action_assigned`` u ``overdue``, sin ninguna entrada de evidencia de la acción
     vigente, ``closure_evidence_missing`` (F04); sin licencia, ``signer_license_missing`` (F04);
     con las dos, T09;
   - desde ``received``, ``in_review``, ``open`` o ``discarded``,
     ``closure_requires_assigned_action`` (F02, F03);
   - desde cualquier otro estado (``closed``, ``unclosable``, ``suspended_by_reclassification``),
     ``transition_not_allowed`` (BR-LAZ-36).

   Todas las entradas de evidencia de la acción vigente (hasta 20, DE §2.5) se citan en
   ``evidence_entry_ids``; las sumas de los adjuntos ya verificados del hallazgo, en
   ``attachment_digests``.
5. ``LoopProjectionStore.write_and_apply`` (registro ``closure``, su evento ``finding_closed`` y la
   proyección en la misma transacción). La fila de ``loop.corrective_action`` de la acción vigente
   no se toca (nunca se borra ni se reescribe): sigue ``assigned`` u ``overdue`` hasta que una
   reapertura la sustituya (``superseded``, TASK-343) por la segunda mitad de T13 o T14.

El cierre **nunca** se edita ni se retira (P4, F11): una reclasificación posterior sobre un cerrado
(T19, VIG-212) deja el registro intacto y solo marca
``closed_with_later_different_classification`` en la proyección (BR-LAZ-25); esta operación no la
implementa, solo la respeta (no toca ``closure_record_id`` ni ``closed_at``).
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
    LoopFinding,
    action_rows,
    finding_from,
    read_finding,
    written,
)
from vigia_platform.loop.application.locks import lock_actions, lock_finding
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import Accepted, Close, Rejected, transition
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLOSURE",
    "DESCRIPTION_MAX_CHARS",
    "DESCRIPTION_MIN_CHARS",
    "LICENSE_MAX_CHARS",
    "VERIFICATION_NOTE_MAX_CHARS",
    "ClosureRejected",
    "ClosureRequestInvalid",
    "ClosureResult",
    "ClosureService",
    "ClosureUnavailable",
]

CLOSURE: Final = "closure"
DESCRIPTION_MIN_CHARS: Final = 20
DESCRIPTION_MAX_CHARS: Final = 2000
VERIFICATION_NOTE_MAX_CHARS: Final = 1000
LICENSE_MAX_CHARS: Final = 64
"""Límites de ``Closure`` (``loop.record_types``; DE §2.6)."""

_DESCRIPTION_FIELD: Final = FreeTextField(
    CLOSURE, "/closure_description", DESCRIPTION_MIN_CHARS, DESCRIPTION_MAX_CHARS
)
_NOTE_FIELD: Final = FreeTextField(CLOSURE, "/verification_note", 1, VERIFICATION_NOTE_MAX_CHARS)
_LICENSE_FIELD: Final = FreeTextField(CLOSURE, "/signer/professional_license", 1, LICENSE_MAX_CHARS)

_ENTRIES_OF_ACTION: Final = text(
    "SELECT evidence_entry_id FROM loop.action_evidence_entry"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND action_id = :action_id ORDER BY added_at, evidence_entry_id"
)
_ATTACHMENT_DIGESTS: Final = text(
    "SELECT sha256 FROM loop.closure_attachment"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = :zone_id AND subject_id = :subject_id ORDER BY attachment_id"
)


class ClosureRejected(Exception):
    """Un rechazo de negocio del cierre, con su ``detail_code`` (LoopDetailCode)."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class ClosureRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class ClosureUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class ClosureResult:
    """El recibo del cierre y el estado nuevo del hallazgo (SUP-U05-11)."""

    finding_state_id: uuid.UUID
    closure_id: uuid.UUID
    record_id: uuid.UUID
    action_id: uuid.UUID
    evidence_entry_ids: tuple[uuid.UUID, ...]
    lifecycle_state: FindingLifecycleState
    signed_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    time_aggregated: bool


def _text(value: object, field: FreeTextField, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ActionRequestInvalid("los textos del cierre son cadenas")
    try:
        normalized = apply_base_policy(value, field)
    except FreeTextRejected:
        raise ActionRequestInvalid("texto rechazado por la política base") from None
    if len(normalized.strip()) < field.min_length:
        raise ActionRequestInvalid("texto más corto que su mínimo")
    return normalized


def _license(value: object) -> str | None:
    """La licencia del firmante por la política base; ausente o en blanco, ``None`` (sin ella)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ActionRequestInvalid("la licencia es una cadena")
    try:
        return apply_base_policy(value, _LICENSE_FIELD)
    except FreeTextRejected:
        raise ActionRequestInvalid("licencia rechazada por la política base") from None


def _rejected(result: Rejected) -> ClosureRejected:
    # ``LifecycleCode`` y ``LoopDetailCode`` comparten el nombre del miembro, no el valor (el
    # segundo lleva el prefijo ``loop_`` de A-68).
    return ClosureRejected(LoopDetailCode[result.code.name])


@repository
class ClosureService:
    """``loop.lifecycle``: cerrar (firmar) un hallazgo con acción y evidencia (T09)."""

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
        return "ClosureService()"

    async def close(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        closure_description: object,
        verification_note: object = None,
        professional_license: object = None,
    ) -> ClosureResult:
        """Cierra (firma) el hallazgo ``finding_state_id``."""
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
            # Paso 2 (probado primero): el estado, con evidencia y licencia ficticias que nunca
            # disparan sus propios códigos; un estado equivocado manda sobre un texto mal formado
            # (el mismo orden de ``classification`` y ``reopening``).
            probe = transition(current.snapshot, Close(1, True))
            if isinstance(probe, Rejected):
                raise _rejected(probe)
            description = _text(closure_description, _DESCRIPTION_FIELD, required=True)
            note = _text(verification_note, _NOTE_FIELD, required=False)
            license_ = _license(professional_license)
            if description is None:  # pragma: no cover - required
                raise ActionRequestInvalid("sin descripción de cierre")
            return await self._write(
                transaction, writer_context, current, actions, description, note, license_
            )

    async def _checked(self, transaction: Transaction) -> None:
        """Con los candados tomados y la transición ya comprobada, antes de escribir (gancho de
        las pruebas de concurrencia)."""

    async def _write(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: LoopFinding,
        actions: Mapping[uuid.UUID, ActionRow],
        description: str,
        note: str | None,
        license_: str | None,
    ) -> ClosureResult:
        action_id = current.current_action_id
        action = actions.get(action_id) if action_id is not None else None
        entry_count = 0
        entry_ids: tuple[uuid.UUID, ...] = ()
        if action is not None:
            rows = await transaction.execute(
                _ENTRIES_OF_ACTION,
                {
                    "organization_id": current.organization_id,
                    "plant_id": current.plant_id,
                    "action_id": action_id,
                },
            )
            entry_ids = tuple(uuid.UUID(str(row.evidence_entry_id)) for row in rows.all())
            entry_count = len(entry_ids)
        accepted = transition(current.snapshot, Close(entry_count, license_ is not None))
        if isinstance(accepted, Rejected):
            raise _rejected(accepted)
        assert isinstance(accepted, Accepted)  # noqa: S101 - narrows mypy after the Rejected check
        if action is None or action_id is None:  # pragma: no cover - T09 exige acción vigente
            raise RuntimeError("cierre aceptado sin acción vigente")
        digest_rows = await transaction.execute(
            _ATTACHMENT_DIGESTS,
            {
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "subject_id": current.subject_id,
            },
        )
        digests = tuple(str(row.sha256) for row in digest_rows.all())
        await self._checked(transaction)
        at = to_millisecond(self._clock.now())
        closure_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        actor = context.actor
        if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
            raise ResourceNotFound()
        if license_ is None:  # pragma: no cover - transition ya lo exige
            raise ClosureRejected(LoopDetailCode.SIGNER_LICENSE_MISSING)
        signer = {
            "user_id": str(actor.id),
            "display_name": actor.display_name_snapshot,
            "professional_license": license_,
        }
        content: dict[str, Any] = {
            "closure_id": str(closure_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "anchor_record_id": str(current.anchor_record_id),
            "action_id": str(action_id),
            "action_type": action.action_type.value,
            "closure_description": description,
            "evidence_entry_ids": [str(item) for item in entry_ids],
            "attachment_digests": list(digests),
            "referenced_clip_ids": [],
            "signer": signer,
            "signed_at": format_timestamp(at),
        }
        if note is not None:
            content["verification_note"] = note
        event = NewEvent(
            event_name="finding_closed",
            payload={
                "zone_id": str(current.zone_id),
                "subject_kind": current.subject_kind,
                "subject_id": str(current.subject_id),
                "action_id": str(action_id),
                "action_type": action.action_type.value,
                "closure_id": str(closure_id),
                "record_id": str(record_id),
            },
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            CLOSURE,
            content,
            scope=current.scope,
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = written(result, unavailable=ClosureUnavailable)
        finding = step.finding
        if not step.applied or finding is None or step.transition_id != "T09":
            raise RuntimeError("la proyección no aplicó el cierre recién escrito")
        return ClosureResult(
            finding_state_id=current.finding_state_id,
            closure_id=closure_id,
            record_id=receipt.record_id,
            action_id=action_id,
            evidence_entry_ids=entry_ids,
            lifecycle_state=finding.state,
            signed_at=at,
            presented_at=current.presented(at),
            time_aggregated=current.single_occupancy_zone,
        )

    # --- alcance ---------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding]:
        """Paso 1: la fila de la organización y ``findings.close_and_sign`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            text(
                "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind,"
                " subject_id, anchor_record_id, lifecycle_state, previous_lifecycle_state,"
                " due_at, current_action_id, single_occupancy_zone, aggregation_window_minutes"
                " FROM loop.finding_state"
                " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
            ),
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        place = finding_from(rows[0])
        resource = place.resource()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FINDINGS_CLOSE_AND_SIGN, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.FINDINGS_CLOSE_AND_SIGN, resource)
        return authorized, place
