"""``loop.lifecycle`` (1 de 2): la entrada de evidencia de una acción (LC-LAZ-09; SUP-U05-08).

``add(context, action_id, ...)`` es ``anadir_evidencia_de_accion`` (BLM §3), en el orden de sus
pasos:

1. **Alcance**: la acción de la organización, el hallazgo de su sujeto y ``actions.manage`` sobre
   **su zona**. Inexistente, de otra organización, fuera de alcance o bajo concesión de
   proveedor: ``ResourceNotFound`` (``not_found``), sin escribir nada.
2. **Descripción estructurada obligatoria** (BR-LAZ-42, DE §6.3): ``description`` (20 a 2000),
   ``performed_at`` (marca ISO 8601 UTC con milisegundos, no posterior a la entrada),
   ``performed_by_role`` (un cargo de 1 a 64, jamás un nombre) y ``verification_note`` (opcional,
   hasta 1000), en NFC por la política base. Lo que no tiene esa forma es ``invalid_request`` del
   esquema estricto; los dos validadores de U-04 los aplica el escritor antes del hash.
3. **Adjuntos opcionales** ya subidos con la concesión de TASK-310: ``ClosureAttachmentPort.verify``
   con el orden fijo de PAT-LAZ-SEG-05 (tipo, firma de bytes, metadatos de posición, 5 por entrada
   y 20 por hallazgo contando los ya aceptados, ``sha256`` y tamaño **contra el objeto
   almacenado**, declaración firmada) **antes** de abrir la transacción: cualquier fallo rechaza la
   entrada completa (``attachment_hash_mismatch``, ``attachment_declaration_missing``…) y **no se
   escribe nada**. Sin adjuntos no se toca el almacén: con el almacén caído la entrada se escribe
   igual (FS-LAZ-03); con adjuntos y el almacén caído, ``storage_unavailable``.
4. **En una sola transacción**, tras los candados de ``loop.application.locks`` (el hallazgo y
   después sus acciones): la acción es la **vigente** del hallazgo, viva (``assigned`` u
   ``overdue``) y el hallazgo está en ``action_assigned`` u ``overdue``; si no,
   ``transition_not_allowed`` (una acción sustituida, cerrada o de un lazo suspendido no recibe
   evidencia). Con los candados tomados se vuelven a contar las entradas de la acción (como mucho
   ``MAX_ENTRIES_PER_ACTION``, DE §2.5) y los adjuntos del hallazgo (como mucho 20,
   ``attachment_too_large``): dos entradas simultáneas nunca pasan juntas de los topes.
5. ``LoopProjectionStore.write_and_apply``: el registro ``action_evidence_added`` y su evento; el
   registro no cambia el estado del hallazgo (solo avanza el control de la proyección). Después,
   las filas de ``loop.closure_attachment`` (cada adjunto con su clave, su suma y la declaración)
   y la de ``loop.action_evidence_entry``. Si algo falla, no queda nada.

La entrada **no** cambia el estado del ciclo de vida: no hay transición de la tabla de §4.1 para
ella. Toda marca devuelta de una zona unipersonal se agrega a su ventana (BR-LAZ-06).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerDatabase
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected, apply_base_policy
from vigia_platform.loop.adapters.attachments.rules import (
    MAX_ATTACHMENTS_PER_FINDING,
    AttachmentRejected,
)
from vigia_platform.loop.adapters.attachments.verify import (
    AttachmentClaim,
    AttachmentSubject,
    VerifiedAttachment,
)
from vigia_platform.loop.application.actions import (
    LIVE_ACTION_STATES,
    ActionRejected,
    ActionRequestInvalid,
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
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "ACTION_EVIDENCE_ADDED",
    "ACTION_RESOURCE_KIND",
    "DESCRIPTION_MAX_CHARS",
    "DESCRIPTION_MIN_CHARS",
    "MAX_ENTRIES_PER_ACTION",
    "ROLE_MAX_CHARS",
    "VERIFICATION_NOTE_MAX_CHARS",
    "AttachmentVerifier",
    "EvidenceResult",
    "EvidenceService",
]

ACTION_EVIDENCE_ADDED: Final = "action_evidence_added"
ACTION_RESOURCE_KIND: Final = "corrective_action"
"""``kind`` del recurso en la auditoría de la denegación (el mismo de TASK-310)."""
DESCRIPTION_MIN_CHARS: Final = 20
DESCRIPTION_MAX_CHARS: Final = 2000
VERIFICATION_NOTE_MAX_CHARS: Final = 1000
ROLE_MAX_CHARS: Final = 64
"""Límites de ``ActionEvidenceAdded`` (``loop.record_types``; DE §2.6)."""
MAX_ENTRIES_PER_ACTION: Final = 20
"""Entradas de evidencia de una acción (DE §2.5: 0 a 20 ``[estimación propia]``), las que un cierre
puede citar en ``evidence_entry_ids``."""

S = FindingLifecycleState

_DESCRIPTION: Final = FreeTextField(
    ACTION_EVIDENCE_ADDED, "/description", DESCRIPTION_MIN_CHARS, DESCRIPTION_MAX_CHARS
)
_NOTE: Final = FreeTextField(
    ACTION_EVIDENCE_ADDED, "/verification_note", 1, VERIFICATION_NOTE_MAX_CHARS
)
_ROLE: Final = FreeTextField(ACTION_EVIDENCE_ADDED, "/performed_by_role", 1, ROLE_MAX_CHARS)
_TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
"""La marca del contrato: ISO 8601 en UTC con milisegundos y ``Z``."""
_WITH_EVIDENCE: Final = frozenset({S.ACTION_ASSIGNED, S.OVERDUE})
_UUID_VERSION: Final = 7

_ACTION: Final = text(
    "SELECT a.action_id, f.finding_state_id, f.organization_id, f.plant_id, f.zone_id,"
    " f.subject_kind, f.subject_id, f.anchor_record_id, f.lifecycle_state,"
    " f.previous_lifecycle_state, f.due_at, f.current_action_id, f.single_occupancy_zone,"
    " f.aggregation_window_minutes FROM loop.corrective_action AS a"
    " JOIN loop.finding_state AS f ON f.organization_id = a.organization_id"
    " AND f.plant_id = a.plant_id AND f.subject_id = a.subject_id"
    " WHERE a.organization_id = :organization_id AND a.action_id = :action_id"
)
_ATTACHMENTS_OF_SUBJECT: Final = text(
    "SELECT count(*) AS n FROM loop.closure_attachment"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = :zone_id AND subject_id = :subject_id"
)
_ENTRIES_OF_ACTION: Final = text(
    "SELECT count(*) AS n FROM loop.action_evidence_entry"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND action_id = :action_id"
)
_USED_ATTACHMENTS: Final = text(
    "SELECT count(*) AS n FROM loop.closure_attachment"
    " WHERE organization_id = :organization_id AND attachment_id = ANY(:attachment_ids)"
)
_INSERT_ATTACHMENT: Final = text(
    "INSERT INTO loop.closure_attachment (attachment_id, organization_id, plant_id, zone_id,"
    " subject_id, content_type, size_bytes, sha256, filename, storage_key,"
    " no_identifiable_person_declared, declared_by, uploaded_at, verified_at)"
    " VALUES (:attachment_id, :organization_id, :plant_id, :zone_id, :subject_id,"
    " :content_type, :size_bytes, :sha256, :filename, :storage_key, true,"
    " CAST(:declared_by AS jsonb), :at, :at)"
)
_INSERT_ENTRY: Final = text(
    "INSERT INTO loop.action_evidence_entry (evidence_entry_id, organization_id, plant_id,"
    " zone_id, action_id, record_id, description, verification_note, performed_at,"
    " performed_by_role, attachment_ids, added_by, added_at) VALUES (:evidence_entry_id,"
    " :organization_id, :plant_id, :zone_id, :action_id, :record_id, :description,"
    " :verification_note, :performed_at, :performed_by_role, :attachment_ids,"
    " CAST(:added_by AS jsonb), :added_at)"
)


class AttachmentVerifier(Protocol):
    """``ClosureAttachmentPort.verify`` (TASK-310): el orden fijo de PAT-LAZ-SEG-05."""

    async def verify(
        self, subject: AttachmentSubject, claims: object, *, already_accepted: object = 0
    ) -> tuple[VerifiedAttachment, ...]: ...


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    """El recibo de la entrada de evidencia (SUP-U05-08)."""

    finding_state_id: uuid.UUID
    action_id: uuid.UUID
    evidence_entry_id: uuid.UUID
    record_id: uuid.UUID
    attachment_ids: tuple[uuid.UUID, ...]
    lifecycle_state: FindingLifecycleState
    added_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    performed_at: datetime
    presented_performed_at: datetime
    time_aggregated: bool


def _text(value: object, field: FreeTextField, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ActionRequestInvalid("los textos de la evidencia son cadenas")
    try:
        normalized = apply_base_policy(value, field)
    except FreeTextRejected:
        raise ActionRequestInvalid("texto rechazado por la política base") from None
    if len(normalized.strip()) < field.min_length:
        raise ActionRequestInvalid("texto más corto que su mínimo")
    return normalized


def _performed_at(value: object, at: datetime) -> datetime:
    """La marca del contrato y no posterior al instante de la entrada."""
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ActionRequestInvalid("performed_at es una marca ISO 8601 UTC con milisegundos")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise ActionRequestInvalid("performed_at no existe") from None
    if moment > at:
        raise ActionRequestInvalid("performed_at es posterior a la entrada")
    return moment


@repository
class EvidenceService:
    """``loop.lifecycle``: añadir una entrada de evidencia a la acción vigente de un hallazgo."""

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
        return "EvidenceService()"

    async def add(
        self,
        context: ScopeContext,
        action_id: uuid.UUID,
        *,
        description: object,
        performed_at: object,
        performed_by_role: object,
        verification_note: object = None,
        attachments: Sequence[AttachmentClaim] = (),
        verifier: AttachmentVerifier | None = None,
    ) -> EvidenceResult:
        """Añade la entrada; ``verifier`` es el puerto de adjuntos (solo con adjuntos)."""
        authorized, place = await self._authorized(context, action_id)
        at = to_millisecond(self._clock.now())
        entry_text = _text(description, _DESCRIPTION, required=True)
        note = _text(verification_note, _NOTE, required=False)
        role = _text(performed_by_role, _ROLE, required=True)
        performed = _performed_at(performed_at, at)
        if entry_text is None or role is None:  # pragma: no cover - required
            raise ActionRequestInvalid("descripción incompleta")
        verified = await self._verified(authorized, place, attachments, verifier)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, place.organization_id, place.subject_id)
            actions = action_rows(
                await lock_actions(
                    transaction, place.organization_id, place.plant_id, place.subject_id
                )
            )
            current = await read_finding(transaction, place.finding_state_id)
            action = actions.get(action_id)
            if (
                current is None
                or action is None
                or current.state not in _WITH_EVIDENCE
                or current.current_action_id != action_id
                or action.state not in LIVE_ACTION_STATES
            ):
                raise ActionRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
            await self._within_limits(transaction, current, action_id, verified)
            await self._checked(transaction)
            entry_id = uuid7(self._clock, self._random_bytes)
            record_id = uuid7(self._clock, self._random_bytes)
            signer = actor_snapshot(writer_context)
            content: dict[str, Any] = {
                "evidence_entry_id": str(entry_id),
                "action_id": str(action_id),
                "subject_id": str(current.subject_id),
                "description": entry_text,
                "performed_at": format_timestamp(performed),
                "performed_by_role": role,
                "attachments": [
                    {
                        "attachment_id": str(item.attachment_id),
                        "sha256": item.sha256,
                        "content_type": item.content_type.value,
                        "size_bytes": item.size_bytes,
                        "filename": item.filename,
                        "no_identifiable_person_declared": True,
                    }
                    for item in verified
                ],
                "added_by": signer,
                "added_at": format_timestamp(at),
            }
            if note is not None:
                content["verification_note"] = note
            event = NewEvent(
                event_name=ACTION_EVIDENCE_ADDED,
                payload={
                    "zone_id": str(current.zone_id),
                    "subject_id": str(current.subject_id),
                    "action_id": str(action_id),
                    "evidence_entry_id": str(entry_id),
                    "record_id": str(record_id),
                    "attachment_count": len(verified),
                },
            )
            result, _step = await self._store.write_and_apply(
                transaction,
                self._writer,
                writer_context,
                ACTION_EVIDENCE_ADDED,
                content,
                scope=current.scope,
                events=(event,),
                occurred_at=at,
                record_id=record_id,
            )
            receipt = written(result)
            for item in verified:
                await transaction.execute(
                    _INSERT_ATTACHMENT,
                    {
                        "attachment_id": item.attachment_id,
                        "organization_id": current.organization_id,
                        "plant_id": current.plant_id,
                        "zone_id": current.zone_id,
                        "subject_id": current.subject_id,
                        "content_type": item.content_type.value,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "filename": item.filename,
                        "storage_key": item.storage_key,
                        "declared_by": json.dumps(signer),
                        "at": at,
                    },
                )
            await transaction.execute(
                _INSERT_ENTRY,
                {
                    "evidence_entry_id": entry_id,
                    "organization_id": current.organization_id,
                    "plant_id": current.plant_id,
                    "zone_id": current.zone_id,
                    "action_id": action_id,
                    "record_id": receipt.record_id,
                    "description": entry_text,
                    "verification_note": note,
                    "performed_at": performed,
                    "performed_by_role": role,
                    "attachment_ids": [item.attachment_id for item in verified],
                    "added_by": json.dumps(signer),
                    "added_at": at,
                },
            )
        return EvidenceResult(
            finding_state_id=current.finding_state_id,
            action_id=action_id,
            evidence_entry_id=entry_id,
            record_id=receipt.record_id,
            attachment_ids=tuple(item.attachment_id for item in verified),
            lifecycle_state=current.state,
            added_at=at,
            presented_at=current.presented(at),
            performed_at=performed,
            presented_performed_at=current.presented(performed),
            time_aggregated=current.single_occupancy_zone,
        )

    async def _checked(self, transaction: Transaction) -> None:
        """Con los candados tomados y el estado y los topes ya comprobados, antes de escribir
        (gancho de las pruebas de concurrencia)."""

    # --- pasos -----------------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, action_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding]:
        """Paso 1: la acción y su hallazgo, y ``actions.manage`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(action_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context, _ACTION, {"organization_id": context.organization_id, "action_id": action_id}
        )
        if not rows:
            raise ResourceNotFound()
        place = finding_from(rows[0])
        resource = Resource(
            place.organization_id,
            ACTION_RESOURCE_KIND,
            action_id,
            plant_id=place.plant_id,
            zone_id=place.zone_id,
        )
        authorized = await self._authorizer.authorize(
            context, PermissionKey.ACTIONS_MANAGE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.ACTIONS_MANAGE, resource)
        return authorized, place

    async def _verified(
        self,
        context: ScopeContext,
        place: LoopFinding,
        attachments: Sequence[AttachmentClaim],
        verifier: AttachmentVerifier | None,
    ) -> tuple[VerifiedAttachment, ...]:
        """Paso 3: los adjuntos contra el almacén, fuera de la transacción; sin ellos, nada."""
        if not attachments:
            return ()
        if verifier is None:
            raise RuntimeError("una entrada con adjuntos exige ClosureAttachmentPort")
        if any(
            not isinstance(claim, AttachmentClaim)
            or not isinstance(claim.attachment_id, uuid.UUID)
            or claim.attachment_id.version != _UUID_VERSION
            for claim in attachments
        ):
            # La concesión emite UUID v7 (ClosureAttachment.attachment_id del registro).
            raise ActionRequestInvalid("attachment_id es el UUID v7 de la concesión")
        rows = await self._database.read(context, _ATTACHMENTS_OF_SUBJECT, _subject_key(place))
        already = int(rows[0].n)
        subject = AttachmentSubject(
            organization_id=place.organization_id,
            plant_id=place.plant_id,
            zone_id=place.zone_id,
            subject_id=place.subject_id,
        )
        return await verifier.verify(subject, list(attachments), already_accepted=already)

    @staticmethod
    async def _within_limits(
        transaction: Transaction,
        current: LoopFinding,
        action_id: uuid.UUID,
        verified: Sequence[VerifiedAttachment],
    ) -> None:
        """Paso 4: los topes recontados con los candados tomados."""
        entries = (
            await transaction.execute(
                _ENTRIES_OF_ACTION,
                {
                    "organization_id": current.organization_id,
                    "plant_id": current.plant_id,
                    "action_id": action_id,
                },
            )
        ).one()
        if int(entries.n) >= MAX_ENTRIES_PER_ACTION:
            raise ActionRequestInvalid("la acción ya tiene todas las entradas que admite")
        if not verified:
            return
        accepted = (await transaction.execute(_ATTACHMENTS_OF_SUBJECT, _subject_key(current))).one()
        if int(accepted.n) + len(verified) > MAX_ATTACHMENTS_PER_FINDING:
            raise AttachmentRejected(LoopDetailCode.ATTACHMENT_TOO_LARGE)
        used = (
            await transaction.execute(
                _USED_ATTACHMENTS,
                {
                    "organization_id": current.organization_id,
                    "attachment_ids": [item.attachment_id for item in verified],
                },
            )
        ).one()
        if int(used.n):
            raise ActionRequestInvalid("un adjunto ya forma parte de otra entrada")


def _subject_key(place: LoopFinding) -> dict[str, Any]:
    return {
        "organization_id": place.organization_id,
        "plant_id": place.plant_id,
        "zone_id": place.zone_id,
        "subject_id": place.subject_id,
    }
