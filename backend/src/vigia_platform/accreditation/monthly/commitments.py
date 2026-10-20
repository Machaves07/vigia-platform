"""Compromisos del mando de línea (TASK-347; SUP-U05-27, 28 y 29; BR-LAZ-109; H12, H-31).

El mando de línea **registra compromisos y nada más** (BR-LAZ-109): estas operaciones solo
escriben registros ``commitments_recorded`` y filas de ``accreditation.commitment``; no leen ni
escriben ningún hallazgo, clasificación, acción, cierre ni etiqueta. Su clave,
``commitments.write``, la tienen el mando de línea y el coordinador SST, y en la matriz el mando
de línea **no tiene ninguna clave del lazo** (``findings.*``, ``actions.manage``,
``review_queue.resolve``, ``root_cause.analyze``): la ausencia es estructural.

**Alcance** (BR-NUC-09, como ``export.request``): un paquete o un compromiso inexistente, de otra
organización o que el actor no puede ver responde ``not_found``, nunca ``forbidden``.
``commitment_out_of_scope`` (400, A-68) es el rechazo de lo que el actor **sí ve** pero queda
fuera del alcance en el que registra compromisos, sin revelar nada que no vea ya:

- un paquete que ve con ``review_package.read`` y cuya zona no cubre su ``commitments.write`` (p.
  ej. el mando de línea de una zona que además es gerente de la planta);
- un responsable que no es un usuario activo de la organización con un rol vigente sobre la
  organización, la planta o la zona del paquete (el patrón de A-58 de ``loop.actions``).

``CommitmentService``:

- ``record`` (SUP-U05-27): de 1 a 20 compromisos, cada uno con texto de 20 a 1000 caracteres,
  responsable y fecha ``AAAA-MM-DD``, sobre un paquete **sellado** (si no,
  ``monthly_package_not_available``, 409). Un registro ``commitments_recorded`` (versión 2,
  ``state = open``) con el evento ``commitments_recorded`` y una fila ``open`` por compromiso.
- ``update`` (SUP-U05-28): estado (``open``, ``done`` o ``dropped``) y nota de cierre opcional de
  hasta 500 caracteres, solo con ``done`` o ``dropped``. **Un registro nuevo**
  ``commitments_recorded`` (versión 2) con el compromiso tal como estaba más su estado y su nota,
  sin evento (no se registran compromisos nuevos); la fila cambia solo ``state``, ``closed_note`` y
  ``closed_at`` (las columnas que ``laz_0031`` concede a ``vigia_app``): ``record_id`` sigue
  siendo el registro del alta.
- ``of_zone`` (SUP-U05-29): los compromisos de los paquetes de una zona y un mes, completos
  (``review_package.read`` sobre la zona).

Los textos pasan la política base aquí (forma y longitud) y, al escribir, los dos validadores de
U-04 (``blocked_vocabulary_term`` o ``person_attribution_detected``).

**Orden de candados**: la cadena de la planta (el disparador del expediente) y después el control
de la proyección (``LoopProjectionStore.write_and_apply``); las filas de ``commitment`` se
escriben al final. Dos actualizaciones simultáneas del mismo compromiso se ordenan en la cadena de
la planta (que se retiene hasta el ``COMMIT``), así que la fila acaba con el estado del último
registro de la cadena. No se toma ningún candado propio.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.monthly.repository import date_to_period, period_to_date
from vigia_platform.accreditation.record_types import MAX_COMMITMENTS
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound, decide
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
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected, apply_base_policy
from vigia_platform.loop.detail_codes import LoopDetailCode, U04DetailCode
from vigia_platform.loop.domain.enums import CommitmentState
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLOSED_NOTE_MAX_CHARS",
    "COMMITMENTS_RECORDED",
    "COMMITMENT_RESOURCE_KIND",
    "TEXT_MAX_CHARS",
    "TEXT_MIN_CHARS",
    "CommitmentDraft",
    "CommitmentRejected",
    "CommitmentRequestInvalid",
    "CommitmentRow",
    "CommitmentService",
    "CommitmentUnavailable",
    "CommitmentUpdateResult",
    "CommitmentsRecordedResult",
]

COMMITMENTS_RECORDED: Final = "commitments_recorded"
TEXT_MIN_CHARS: Final = 20
TEXT_MAX_CHARS: Final = 1000
"""Texto de un compromiso (DE §3.3: 20 a 1000)."""
CLOSED_NOTE_MAX_CHARS: Final = 500
"""Nota de cierre (DE §3.3: 0 a 500)."""
COMMITMENT_RESOURCE_KIND: Final = "commitment"
_PACKAGE_RESOURCE_KIND: Final = "monthly_package"

_TEXT_FIELD: Final = FreeTextField(
    COMMITMENTS_RECORDED, "/commitments[*]/text", TEXT_MIN_CHARS, TEXT_MAX_CHARS
)
_NOTE_FIELD: Final = FreeTextField(
    COMMITMENTS_RECORDED, "/commitments[*]/closed_note", 1, CLOSED_NOTE_MAX_CHARS
)
_CLOSED_STATES: Final = frozenset({CommitmentState.DONE, CommitmentState.DROPPED})

_PACKAGE: Final = text(
    "SELECT package_id, plant_id, zone_id, period, state FROM accreditation.monthly_package"
    " WHERE organization_id = :organization_id AND package_id = :package_id"
)
_ZONE: Final = text(
    "SELECT plant_id FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_RESPONSIBLE: Final = text(
    # Un usuario activo de la organización con un rol vigente sobre la organización, la planta del
    # paquete o su zona (el patrón de A-58, como ``loop.actions._ASSIGNEE``).
    "SELECT u.user_id FROM identity.user_account AS u"
    " WHERE u.organization_id = :organization_id AND u.user_id = :user_id"
    " AND u.status = 'active' AND EXISTS (SELECT 1 FROM identity.role_assignment AS r"
    " WHERE r.organization_id = :organization_id AND r.user_id = u.user_id"
    " AND r.removed_at IS NULL AND (r.scope_level = 'organization'"
    " OR (r.scope_level = 'plant' AND r.scope_id = :plant_id)"
    " OR (r.scope_level = 'zone' AND r.scope_id = :zone_id)))"
)
_COMMITMENT: Final = text(
    "SELECT c.commitment_id, c.organization_id, c.plant_id, c.zone_id, c.package_id, p.period,"
    " c.text, c.responsible_user_id, c.due_date, c.state, c.recorded_at, c.record_id,"
    " c.closed_note, c.closed_at FROM accreditation.commitment AS c"
    " JOIN accreditation.monthly_package AS p"
    " ON p.organization_id = c.organization_id AND p.package_id = c.package_id"
    " WHERE c.organization_id = :organization_id AND c.commitment_id = :commitment_id"
)
_OF_ZONE_AND_PERIOD: Final = text(
    "SELECT c.commitment_id, c.organization_id, c.plant_id, c.zone_id, c.package_id, p.period,"
    " c.text, c.responsible_user_id, c.due_date, c.state, c.recorded_at, c.record_id,"
    " c.closed_note, c.closed_at FROM accreditation.commitment AS c"
    " JOIN accreditation.monthly_package AS p"
    " ON p.organization_id = c.organization_id AND p.package_id = c.package_id"
    " WHERE c.organization_id = :organization_id AND c.zone_id = :zone_id"
    " AND p.zone_id = :zone_id AND p.period = :period"
    " ORDER BY c.recorded_at, c.commitment_id"
)
_INSERT: Final = text(
    "INSERT INTO accreditation.commitment (commitment_id, organization_id, plant_id, zone_id,"
    " package_id, text, responsible_user_id, due_date, state, recorded_by, recorded_at,"
    " record_id) VALUES (:commitment_id, :organization_id, :plant_id, :zone_id, :package_id,"
    " :text, :responsible_user_id, :due_date, 'open', CAST(:recorded_by AS jsonb),"
    " :recorded_at, :record_id)"
)
_UPDATE: Final = text(
    "UPDATE accreditation.commitment SET state = :state, closed_note = :closed_note,"
    " closed_at = :closed_at"
    " WHERE organization_id = :organization_id AND commitment_id = :commitment_id"
    " RETURNING commitment_id"
)

_VALIDATOR_CODES: Final[Mapping[str, LoopDetailCode]] = {
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value: LoopDetailCode.BLOCKED_VOCABULARY_TERM,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value: LoopDetailCode.PERSON_ATTRIBUTION_DETECTED,
}


class CommitmentRejected(Exception):
    """Un rechazo de negocio, con su ``detail_code`` (acreditación o validadores del lazo)."""

    def __init__(self, detail_code: U04DetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class CommitmentRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class CommitmentUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


@dataclass(frozen=True, slots=True)
class CommitmentDraft:
    """Un compromiso tal como llega en el cuerpo (sin validar)."""

    text: object
    responsible_user_id: object
    due_date: object


@dataclass(frozen=True, slots=True)
class CommitmentRow:
    """Una fila de ``accreditation.commitment`` con el periodo de su paquete."""

    commitment_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    package_id: uuid.UUID
    period: str
    text: str
    responsible_user_id: uuid.UUID
    due_date: date
    state: CommitmentState
    recorded_at: datetime
    record_id: uuid.UUID
    closed_note: str | None
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CommitmentsRecordedResult:
    """El recibo del alta (SUP-U05-27)."""

    commitment_batch_id: uuid.UUID
    record_id: uuid.UUID
    package_id: uuid.UUID
    zone_id: uuid.UUID
    commitments: tuple[CommitmentRow, ...]


@dataclass(frozen=True, slots=True)
class CommitmentUpdateResult:
    """El recibo de la actualización (SUP-U05-28): la fila nueva y su registro."""

    commitment: CommitmentRow
    record_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Package:
    package_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    sealed: bool


@dataclass(frozen=True, slots=True)
class _Valid:
    text: str
    responsible_user_id: uuid.UUID
    due_date: date


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _row(row: Any) -> CommitmentRow:
    data = row._mapping
    return CommitmentRow(
        commitment_id=_uuid(data["commitment_id"]),
        organization_id=_uuid(data["organization_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        package_id=_uuid(data["package_id"]),
        period=date_to_period(data["period"]),
        text=str(data["text"]),
        responsible_user_id=_uuid(data["responsible_user_id"]),
        due_date=data["due_date"],
        state=CommitmentState(data["state"]),
        recorded_at=data["recorded_at"],
        record_id=_uuid(data["record_id"]),
        closed_note=data["closed_note"],
        closed_at=data["closed_at"],
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise CommitmentRequestInvalid("el texto del compromiso es una cadena")
    try:
        normalized = apply_base_policy(value, _TEXT_FIELD)
    except FreeTextRejected:
        raise CommitmentRequestInvalid("texto rechazado por la política base") from None
    if len(normalized.strip()) < TEXT_MIN_CHARS:
        raise CommitmentRequestInvalid("texto más corto que su mínimo")
    return normalized


def _responsible(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise CommitmentRequestInvalid("el responsable es un identificador")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise CommitmentRequestInvalid("el responsable es un identificador") from None


def _due_date(value: object) -> date:
    """``AAAA-MM-DD`` exacta: nada de horas, zonas ni formas abreviadas."""
    if not isinstance(value, str) or len(value) != 10:
        raise CommitmentRequestInvalid("la fecha es AAAA-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise CommitmentRequestInvalid("la fecha es AAAA-MM-DD") from None
    if parsed.isoformat() != value:
        raise CommitmentRequestInvalid("la fecha es AAAA-MM-DD")
    return parsed


def _state(value: object) -> CommitmentState:
    if not isinstance(value, str):
        raise CommitmentRequestInvalid("el estado es una cadena de commitment_state")
    try:
        return CommitmentState(value)
    except ValueError:
        raise CommitmentRequestInvalid("estado fuera de commitment_state") from None


def _note(value: object, state: CommitmentState) -> str | None:
    """La nota de cierre en NFC de hasta 500; solo con ``done`` o ``dropped``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise CommitmentRequestInvalid("la nota es una cadena")
    if state not in _CLOSED_STATES:
        raise CommitmentRequestInvalid("la nota de cierre acompaña a done o dropped")
    try:
        return apply_base_policy(value, _NOTE_FIELD)
    except FreeTextRejected:
        raise CommitmentRequestInvalid("nota rechazada por la política base") from None


def _written(result: Receipt | LedgerRejection) -> Receipt:
    """Un rechazo o un duplicado revierten la transacción (nada queda escrito)."""
    if isinstance(result, LedgerRejection):
        if result.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            code = _VALIDATOR_CODES.get(result.detail_code or "")
            if code is not None:
                raise CommitmentRejected(code)
            raise CommitmentRequestInvalid("texto libre rechazado por el expediente")
        if result.code is LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT:
            raise CommitmentUnavailable("ledger_chain")
        raise RuntimeError(f"commitments_recorded rechazado: {result.code.value}")
    if result.status is not AcceptanceStatus.ACCEPTED:
        raise RuntimeError("commitments_recorded con un identificador ya usado")
    return result


def _actor(context: ScopeContext) -> dict[str, Any]:
    actor = context.actor
    if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
        raise ResourceNotFound()
    return {
        "user_id": str(actor.id),
        "display_name": actor.display_name_snapshot,
        "role_in_use": actor.role_in_use.value,
    }


@repository
class CommitmentService:
    """``accreditation.monthly``: los compromisos que registra el mando de línea."""

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
        return "CommitmentService()"

    # --- SUP-U05-27 ------------------------------------------------------------------------------

    async def record(
        self,
        context: ScopeContext,
        package_id: uuid.UUID,
        drafts: Sequence[CommitmentDraft],
    ) -> CommitmentsRecordedResult:
        """Registra de 1 a 20 compromisos del paquete ``package_id``."""
        if not isinstance(context, ScopeContext) or type(package_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _PACKAGE,
            {"organization_id": context.organization_id, "package_id": package_id},
        )
        if not rows:
            raise ResourceNotFound()
        data = rows[0]._mapping
        package = _Package(
            package_id=_uuid(data["package_id"]),
            plant_id=_uuid(data["plant_id"]),
            zone_id=_uuid(data["zone_id"]),
            period=date_to_period(data["period"]),
            sealed=data["state"] == "sealed",
        )
        authorized = await self._writable(
            context,
            Resource(
                context.organization_id,
                _PACKAGE_RESOURCE_KIND,
                package.package_id,
                plant_id=package.plant_id,
                zone_id=package.zone_id,
            ),
        )
        if not 1 <= len(drafts) <= MAX_COMMITMENTS:
            raise CommitmentRequestInvalid("de 1 a 20 compromisos")
        valid = [
            _Valid(
                _text(draft.text),
                _responsible(draft.responsible_user_id),
                _due_date(draft.due_date),
            )
            for draft in drafts
        ]
        if not package.sealed:
            raise CommitmentRejected(AccreditationDetailCode.MONTHLY_PACKAGE_NOT_AVAILABLE)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            for entry in valid:
                await self._check_responsible(transaction, package, entry.responsible_user_id)
            return await self._write_batch(transaction, writer_context, package, valid)

    async def _write_batch(
        self,
        transaction: Transaction,
        context: ScopeContext,
        package: _Package,
        valid: Sequence[_Valid],
    ) -> CommitmentsRecordedResult:
        at = to_millisecond(self._clock.now())
        batch_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        ids = [uuid7(self._clock, self._random_bytes) for _ in valid]
        recorder = _actor(context)
        content: dict[str, Any] = {
            "commitment_batch_id": str(batch_id),
            "package_id": str(package.package_id),
            "zone_id": str(package.zone_id),
            "commitments": [
                {
                    "commitment_id": str(commitment_id),
                    "text": entry.text,
                    "responsible_user_id": str(entry.responsible_user_id),
                    "due_date": entry.due_date.isoformat(),
                    "state": CommitmentState.OPEN.value,
                }
                for commitment_id, entry in zip(ids, valid, strict=True)
            ],
            "recorded_by": recorder,
            "recorded_at": format_timestamp(at),
        }
        event = NewEvent(
            event_name=COMMITMENTS_RECORDED,
            payload={
                "commitment_batch_id": str(batch_id),
                "record_id": str(record_id),
                "package_id": str(package.package_id),
                "zone_id": str(package.zone_id),
                "commitment_count": len(valid),
            },
        )
        result, _step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            COMMITMENTS_RECORDED,
            content,
            scope=RecordScope(plant_id=package.plant_id, zone_id=package.zone_id),
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(result)
        organization_id = context.organization_id
        recorded_by = json.dumps(recorder, ensure_ascii=False)
        created: list[CommitmentRow] = []
        for commitment_id, entry in zip(ids, valid, strict=True):
            await transaction.execute(
                _INSERT,
                {
                    "commitment_id": commitment_id,
                    "organization_id": organization_id,
                    "plant_id": package.plant_id,
                    "zone_id": package.zone_id,
                    "package_id": package.package_id,
                    "text": entry.text,
                    "responsible_user_id": entry.responsible_user_id,
                    "due_date": entry.due_date,
                    "recorded_by": recorded_by,
                    "recorded_at": at,
                    "record_id": receipt.record_id,
                },
            )
            created.append(
                CommitmentRow(
                    commitment_id=commitment_id,
                    organization_id=organization_id,
                    plant_id=package.plant_id,
                    zone_id=package.zone_id,
                    package_id=package.package_id,
                    period=package.period,
                    text=entry.text,
                    responsible_user_id=entry.responsible_user_id,
                    due_date=entry.due_date,
                    state=CommitmentState.OPEN,
                    recorded_at=at,
                    record_id=receipt.record_id,
                    closed_note=None,
                    closed_at=None,
                )
            )
        return CommitmentsRecordedResult(
            commitment_batch_id=batch_id,
            record_id=receipt.record_id,
            package_id=package.package_id,
            zone_id=package.zone_id,
            commitments=tuple(created),
        )

    # --- SUP-U05-28 ------------------------------------------------------------------------------

    async def update(
        self,
        context: ScopeContext,
        commitment_id: uuid.UUID,
        *,
        state: object,
        note: object = None,
    ) -> CommitmentUpdateResult:
        """Cambia el estado (y la nota de cierre) del compromiso ``commitment_id``."""
        if not isinstance(context, ScopeContext) or type(commitment_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _COMMITMENT,
            {"organization_id": context.organization_id, "commitment_id": commitment_id},
        )
        if not rows:
            raise ResourceNotFound()
        current = _row(rows[0])
        authorized = await self._writable(
            context,
            Resource(
                current.organization_id,
                COMMITMENT_RESOURCE_KIND,
                current.commitment_id,
                plant_id=current.plant_id,
                zone_id=current.zone_id,
            ),
        )
        chosen = _state(state)
        closed_note = _note(note, chosen)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            return await self._write_update(
                transaction, writer_context, current, chosen, closed_note
            )

    async def _write_update(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: CommitmentRow,
        state: CommitmentState,
        closed_note: str | None,
    ) -> CommitmentUpdateResult:
        at = to_millisecond(self._clock.now())
        entry: dict[str, Any] = {
            "commitment_id": str(current.commitment_id),
            "text": current.text,
            "responsible_user_id": str(current.responsible_user_id),
            "due_date": current.due_date.isoformat(),
            "state": state.value,
        }
        if closed_note is not None:
            entry["closed_note"] = closed_note
        content: dict[str, Any] = {
            "commitment_batch_id": str(uuid7(self._clock, self._random_bytes)),
            "package_id": str(current.package_id),
            "zone_id": str(current.zone_id),
            "commitments": [entry],
            "recorded_by": _actor(context),
            "recorded_at": format_timestamp(at),
        }
        result, _step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            COMMITMENTS_RECORDED,
            content,
            scope=RecordScope(plant_id=current.plant_id, zone_id=current.zone_id),
            occurred_at=at,
            record_id=uuid7(self._clock, self._random_bytes),
        )
        receipt = _written(result)
        closed_at = at if state in _CLOSED_STATES else None
        updated = (
            await transaction.execute(
                _UPDATE,
                {
                    "organization_id": current.organization_id,
                    "commitment_id": current.commitment_id,
                    "state": state.value,
                    "closed_note": closed_note,
                    "closed_at": closed_at,
                },
            )
        ).first()
        if updated is None:  # pragma: no cover - la fila no se borra
            raise ResourceNotFound()
        return CommitmentUpdateResult(
            commitment=CommitmentRow(
                commitment_id=current.commitment_id,
                organization_id=current.organization_id,
                plant_id=current.plant_id,
                zone_id=current.zone_id,
                package_id=current.package_id,
                period=current.period,
                text=current.text,
                responsible_user_id=current.responsible_user_id,
                due_date=current.due_date,
                state=state,
                recorded_at=current.recorded_at,
                record_id=current.record_id,
                closed_note=closed_note,
                closed_at=closed_at,
            ),
            record_id=receipt.record_id,
        )

    # --- SUP-U05-29 ------------------------------------------------------------------------------

    async def of_zone(
        self, context: ScopeContext, *, zone_id: uuid.UUID, period: str
    ) -> tuple[CommitmentRow, ...]:
        """Los compromisos de los paquetes de ``(zone_id, period)``; ``not_found`` si la zona no
        existe o está fuera del alcance de ``review_package.read``."""
        if not isinstance(context, ScopeContext) or type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        month = period_to_date(period)
        rows = await self._database.read(
            context, _ZONE, {"organization_id": context.organization_id, "zone_id": zone_id}
        )
        if not rows:
            raise ResourceNotFound()
        plant_id = _uuid(rows[0]._mapping["plant_id"])
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.REVIEW_PACKAGE_READ,
            Resource.zone(context.organization_id, plant_id, zone_id),
        )
        found = await self._database.read(
            authorized,
            _OF_ZONE_AND_PERIOD,
            {"organization_id": context.organization_id, "zone_id": zone_id, "period": month},
        )
        return tuple(_row(row) for row in found)

    # --- alcance ---------------------------------------------------------------------------------

    async def _writable(self, context: ScopeContext, resource: Resource) -> ScopeContext:
        """``commitments.write`` sobre la zona del recurso.

        Si no la tiene pero **ve** el recurso (``review_package.read``), ``commitment_out_of_scope``
        tras auditar la denegación; si no lo ve, ``not_found`` (``Authorizer.authorize``)."""
        provider = self._authorizer.provider_organization_id
        writes = decide(
            context, PermissionKey.COMMITMENTS_WRITE, resource, provider_organization_id=provider
        )
        if not writes.granted:
            reads = decide(
                context,
                PermissionKey.REVIEW_PACKAGE_READ,
                resource,
                provider_organization_id=provider,
            )
            if reads.granted:
                try:
                    await self._authorizer.deny(context, PermissionKey.COMMITMENTS_WRITE, resource)
                except ResourceNotFound:
                    raise CommitmentRejected(
                        AccreditationDetailCode.COMMITMENT_OUT_OF_SCOPE
                    ) from None
        authorized = await self._authorizer.authorize(
            context, PermissionKey.COMMITMENTS_WRITE, resource
        )
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, PermissionKey.COMMITMENTS_WRITE, resource)
        return authorized

    @staticmethod
    async def _check_responsible(
        transaction: Transaction, package: _Package, user_id: uuid.UUID
    ) -> None:
        row = (
            await transaction.execute(
                _RESPONSIBLE,
                {
                    "organization_id": transaction.context.organization_id,
                    "user_id": user_id,
                    "plant_id": package.plant_id,
                    "zone_id": package.zone_id,
                },
            )
        ).first()
        if row is None:
            raise CommitmentRejected(AccreditationDetailCode.COMMITMENT_OUT_OF_SCOPE)
