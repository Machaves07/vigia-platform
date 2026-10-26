"""``loop.lifecycle`` (2 de 2): el estado SIN ALTERNATIVA SEGURA DISPONIBLE (LC-LAZ-09 y LC-LAZ-11;
SUP-U05-12 y 13; BR-LAZ-52 a 59; PR-LAZ-12; H-26; TASK-312).

El estado incerrable existe para **no tener que mentir** (H13): cuando no hay alternativa segura se
declara, con los cinco campos, bajo la política de planta firmada y con fecha de revisión. U-04 no
interpreta los criterios de la política (son texto del abogado, BR-LAZ-55): exige ``loaded = true``
y guarda ``policy_id``, ``version`` y ``document_sha256`` para que un tercero pueda comprobar bajo
qué política se declaró.

``declare(context, finding_state_id, ...)`` es ``declarar_incerrable`` (BLM §8.2), en el orden de
sus pasos:

1. **Alcance**: la fila de la proyección del hallazgo de la organización y
   ``findings.close_and_sign`` sobre **su zona** (BR-NUC-13). Inexistente, de otra organización,
   fuera de alcance o bajo concesión de proveedor: ``ResourceNotFound`` (``not_found``), sin
   escribir nada.
2. **Política de planta** (BR-LAZ-53, 54 y 56): ``GateQueryPort.plant_policy``. Sin respuesta del
   puerto: ``unclosable_policy_unavailable`` (fallo **cerrado**, RNF-SEG-15) y nada escrito; con
   ``loaded = false``: ``unclosable_policy_not_signed``, que **nombra la compuerta que falta**; con
   la política revocada: ``unclosable_policy_revoked`` (los estados ya declarados se conservan, P4).
3. **Los cinco campos** (BR-LAZ-52): alternativa evaluada, motivo de su descarte, responsable,
   fecha de la decisión y medida compensatoria. Falta cualquiera (o no cabe en su esquema):
   ``unclosable_fields_incomplete`` sin registro. El responsable es un usuario **activo** de la
   organización; si no lo es, ``not_found`` (BR-NUC-09).
4. **Fecha de revisión** (BR-LAZ-57): ``review_due_at`` entre el instante de la declaración y ese
   instante más ``unclosable_review_max_months`` de la configuración de la planta, con
   ``unclosable_review_default_months`` (6) por defecto; fuera de rango,
   ``unclosable_review_date_out_of_range``. Una sola marca: todo sale del ``Clock`` inyectado.
5. **Firma** (BR-LAZ-59): nombre de la sesión y licencia profesional en el cuerpo; sin licencia,
   ``signer_license_missing``.
6. **La transición T10** de la máquina (``lifecycle.transition``) con esos hechos; desde un estado
   que no sea ``open``, ``action_assigned`` u ``overdue``, ``transition_not_allowed`` (BR-LAZ-36).
7. ``LoopProjectionStore.write_and_apply``: el registro ``unclosable_state_declared``, su evento
   ``finding_unclosable`` y la proyección (estado ``unclosable``) en la misma transacción, más la
   fila de ``loop.unclosable_declaration``.

``review(context, declaration_id, ...)`` es ``revisar_incerrable`` (SUP-U05-13): la revisión es un
registro **nuevo** que o bien **renueva** la declaración con nota y fecha nueva (T11) o bien la
**convierte en acción** (T12). Exige firma con licencia, igual que el cierre (BR-LAZ-59).

- **T11** (``renewed``): ``new_review_due_at`` en rango y la declaración renueva su fecha; el
  hallazgo sigue ``unclosable`` y deja de estar pendiente de revisión.
- **T12** (``converted_to_action``): además de ``findings.close_and_sign`` exige ``actions.manage``
  sobre la zona (la fila T12 de §4.1) y la acción completa, que escribe el servicio de TASK-311
  (``ActionService.assign_in_transaction`` con ``pending_action_id``): el registro
  ``unclosable_review`` y el ``action_assigned`` van en la misma transacción, con su proyección y
  sus eventos, y la declaración queda ``converted``.
- La revisión se guarda además en ``loop.unclosable_review`` (§2.7), con la planta y la zona de su
  declaración y el firmante con licencia: es el historial operativo de las revisiones, como
  ``loop.classification`` lo es de las clasificaciones.
- La declaración anterior **permanece íntegra** (P4): ni se borra ni se reescribe; lo único que
  cambia es su ``state`` y, al renovar, su ``review_due_at`` (las dos columnas que ``laz_0029``
  concede a ``vigia_app``).

``mark_due(transaction)`` es la tarea **``unclosable_review_due``** (diaria, por organización;
LC-LAZ-11; BR-LAZ-58): para cada declaración vigente con la fecha pasada marca
``unclosable_pending_review`` en la proyección y ``pending_review`` en la declaración, publica
``unclosable_review_due`` y **no cambia el estado del ciclo de vida** (ningún hallazgo cambia de
estado solo). La marca es única por ``(declaration_id, review_due_at)``: la condición de la
sentencia de la proyección (``unclosable_pending_review = false``) es la garantía, así que la
segunda ejecución —también la de otro proceso que tomó la tarea con el arrendamiento vencido— no
marca ni publica dos veces.

**Orden de candados** (``loop.application.locks``; AGENTS.md): la declaración y la revisión toman
1 (la exclusión del hallazgo) y, la revisión que convierte en acción, 2 (las acciones del hallazgo)
**antes** de escribir; la tarea diaria no escribe el expediente, así que no toma 1 ni 2: toma el
control de cada planta y **todas** sus filas de una vez (``plant_id, finding_state_id``) antes de
escribir, en el orden documentado. Toda marca que se devuelve de una zona unipersonal se agrega a
su ventana (BR-LAZ-06); el expediente conserva la exacta.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.catalog.domain.ports import GateQueryPort, PlantPolicyState
from vigia_platform.identity.authz.authorize import Authorizer, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    Receipt,
)
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextHint,
    FreeTextRejected,
    apply_base_policy,
)
from vigia_platform.loop.application.actions import (
    ActionRejected,
    ActionRequestInvalid,
    ActionService,
    ActionUnavailable,
    AssignmentRequest,
    LoopFinding,
    action_rows,
    finding_from,
    read_finding,
    written,
)
from vigia_platform.loop.application.locks import lock_actions, lock_finding
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import FindingLifecycleState, UnclosableReviewOutcome
from vigia_platform.loop.domain.lifecycle import (
    Accepted,
    DeclareUnclosable,
    LifecycleCode,
    PolicyStatus,
    Rejected,
    ReviewUnclosable,
    transition,
)
from vigia_platform.loop.projection.store import LoopProjectionStore, ReviewDueRow
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "COMPENSATORY_MEASURE_FIELD",
    "EVALUATED_ALTERNATIVE_FIELD",
    "REJECTION_REASON_FIELD",
    "REVIEW_NOTE_FIELD",
    "SCHEDULE",
    "TASK_NAME",
    "DeclarationResult",
    "DeclarationRow",
    "FiveFields",
    "ReviewRequest",
    "ReviewResult",
    "UnclosableRejected",
    "UnclosableRequestInvalid",
    "UnclosableService",
    "UnclosableUnavailable",
    "add_months",
    "declaration_row",
    "five_fields",
    "policy_status",
    "register_unclosable_review_due",
    "review_date",
    "review_outcome",
]

UNCLOSABLE_STATE_DECLARED: Final = "unclosable_state_declared"
UNCLOSABLE_REVIEW: Final = "unclosable_review"
FINDING_UNCLOSABLE: Final = "finding_unclosable"
UNCLOSABLE_REVIEW_DUE: Final = "unclosable_review_due"
UNCLOSABLE_REVIEWED: Final = "unclosable_reviewed"
TASK_NAME: Final = "unclosable_review_due"
SCHEDULE: Final = Schedule.daily(hour=6)
"""Diaria (BLM §3 y LC-LAZ-11), por organización; la hora es ``[estimación propia]``."""
DECISION_DATE_PATTERN: Final = r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
"""``CalendarDate`` de ``loop.record_types`` (DE §2.7)."""
_LICENSE_MAX_CHARS: Final = 64

_log = get_logger("loop.unclosable")
_DECISION_DATE: Final = re.compile(DECISION_DATE_PATTERN)

EVALUATED_ALTERNATIVE_FIELD: Final = FreeTextField(
    UNCLOSABLE_STATE_DECLARED, "/evaluated_alternative", 20, 2000
)
REJECTION_REASON_FIELD: Final = FreeTextField(
    UNCLOSABLE_STATE_DECLARED, "/rejection_reason", 20, 2000
)
COMPENSATORY_MEASURE_FIELD: Final = FreeTextField(
    UNCLOSABLE_STATE_DECLARED, "/compensatory_measure", 20, 2000
)
REVIEW_NOTE_FIELD: Final = FreeTextField(UNCLOSABLE_REVIEW, "/review_note", 20, 2000)
"""Los cuatro textos libres de §5.1, con los límites de su esquema (``LongText``)."""

_FINDING_BY_ID: Final = text(
    "SELECT finding_state_id, organization_id, plant_id, zone_id, subject_kind, subject_id,"
    " anchor_record_id, lifecycle_state, previous_lifecycle_state, due_at, current_action_id,"
    " single_occupancy_zone, aggregation_window_minutes FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND finding_state_id = :finding_state_id"
)
_DECLARATION: Final = text(
    "SELECT d.declaration_id, d.organization_id, d.plant_id, d.zone_id, d.subject_kind,"
    " d.subject_id, d.review_due_at, d.state, d.declared_at, d.policy_id, d.policy_version,"
    " d.policy_document_sha256, f.finding_state_id"
    " FROM loop.unclosable_declaration AS d"
    " JOIN loop.finding_state AS f ON f.organization_id = d.organization_id"
    " AND f.plant_id = d.plant_id AND f.zone_id = d.zone_id"
    " AND f.subject_kind = d.subject_kind AND f.subject_id = d.subject_id"
    " WHERE d.organization_id = :organization_id AND d.declaration_id = :declaration_id"
)
_DECLARATION_OF_SUBJECT: Final = text(
    "SELECT declaration_id FROM loop.unclosable_declaration"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND zone_id = :zone_id"
    " AND subject_kind = :subject_kind AND subject_id = :subject_id"
    " AND declared_at = :declared_at AND review_due_at = :review_due_at"
    " AND state <> 'converted'"
    " ORDER BY declaration_id DESC LIMIT 1"
)
_RESPONSIBLE: Final = text(
    "SELECT user_id, display_name FROM identity.user_account"
    " WHERE organization_id = :organization_id AND user_id = :user_id AND status = 'active'"
)
_INSERT_DECLARATION: Final = text(
    "INSERT INTO loop.unclosable_declaration (declaration_id, organization_id, plant_id, zone_id,"
    " subject_kind, subject_id, record_id, evaluated_alternative, rejection_reason,"
    " responsible_user_id, decision_date, compensatory_measure, review_due_at, policy_id,"
    " policy_version, policy_document_sha256, signer, declared_at)"
    " VALUES (:declaration_id, :organization_id, :plant_id, :zone_id, :subject_kind, :subject_id,"
    " :record_id, :evaluated_alternative, :rejection_reason, :responsible_user_id, :decision_date,"
    " :compensatory_measure, :review_due_at, :policy_id, :policy_version,"
    " :policy_document_sha256, CAST(:signer AS jsonb), :declared_at)"
)
_MARK_DECLARATION: Final = text(
    "UPDATE loop.unclosable_declaration SET state = :state, review_due_at = :review_due_at"
    " WHERE organization_id = :organization_id AND declaration_id = :declaration_id"
    " AND state <> 'converted'"
)
_INSERT_REVIEW: Final = text(
    "INSERT INTO loop.unclosable_review (review_id, organization_id, plant_id, zone_id,"
    " declaration_id, record_id, outcome, review_note, new_review_due_at, new_action_id,"
    " reviewed_by, reviewed_at) VALUES (:review_id, :organization_id, :plant_id, :zone_id,"
    " :declaration_id, :record_id, :outcome, :review_note, :new_review_due_at, :new_action_id,"
    " CAST(:reviewed_by AS jsonb), :reviewed_at)"
)

_DECLARE_CODES: Final[Mapping[LifecycleCode, LoopDetailCode]] = {
    LifecycleCode.TRANSITION_NOT_ALLOWED: LoopDetailCode.TRANSITION_NOT_ALLOWED,
    LifecycleCode.UNCLOSABLE_FIELDS_INCOMPLETE: LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE,
    LifecycleCode.UNCLOSABLE_POLICY_NOT_SIGNED: LoopDetailCode.UNCLOSABLE_POLICY_NOT_SIGNED,
    LifecycleCode.UNCLOSABLE_POLICY_UNAVAILABLE: LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE,
    LifecycleCode.UNCLOSABLE_POLICY_REVOKED: LoopDetailCode.UNCLOSABLE_POLICY_REVOKED,
    LifecycleCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE: (
        LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE
    ),
    LifecycleCode.SIGNER_LICENSE_MISSING: LoopDetailCode.SIGNER_LICENSE_MISSING,
}


class UnclosableRejected(Exception):
    """Un rechazo de negocio del incerrable, con su ``detail_code`` (LoopDetailCode) y, si lo
    rechazó un validador de texto libre, su pista para quien escribió (``hint``, VIG-344)."""

    def __init__(self, detail_code: LoopDetailCode, hint: FreeTextHint | None = None) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code
        self.hint = hint


class UnclosableRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST


class UnclosableUnavailable(ExternalDependencyDown):
    """Transitorio: la cadena de la planta no se pudo tomar a tiempo."""


# --- piezas puras ---------------------------------------------------------------------------------


def add_months(moment: datetime, months: int) -> datetime:
    """``moment`` más ``months`` meses de calendario (el día se recorta al del mes destino).

    Pura y sobre una marca que ya viene del ``Clock``: ninguna hora sale del sistema."""
    if moment.utcoffset() is None:
        raise ValueError("la marca debe llevar zona horaria")
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, _last_day(year, month))
    return moment.replace(year=year, month=month, day=day)


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def policy_status(state: PlantPolicyState) -> PolicyStatus:
    """El estado de la política para la máquina (§2.7; BR-LAZ-53 y 56).

    ``GateQueryPort.plant_policy`` de U-03 (v1.5) expone ``loaded``: ``false`` es «la planta no
    tiene la política firmada» (``unclosable_policy_not_signed``, la compuerta que falta). Una
    marca de revocación, si el puerto la añade, se honra (``unclosable_policy_revoked``); U-04
    nunca interpreta los criterios por su cuenta (BR-LAZ-55)."""
    if getattr(state, "revoked", False):
        return PolicyStatus.REVOKED
    return PolicyStatus.LOADED if state.loaded else PolicyStatus.NOT_SIGNED


def review_outcome(value: object) -> UnclosableReviewOutcome:
    """``renewed`` o ``converted_to_action``; cualquier otro valor, ``invalid_request``."""
    if not isinstance(value, str):
        raise UnclosableRequestInvalid("el resultado de la revisión es una cadena")
    try:
        return UnclosableReviewOutcome(value)
    except ValueError:
        raise UnclosableRequestInvalid("resultado fuera de la lista cerrada") from None


def _instant(value: object, name: str) -> datetime:
    """Una marca ISO 8601 con zona (``Timestamp`` del contrato); sin zona, ``invalid_request``."""
    if not isinstance(value, str):
        raise UnclosableRequestInvalid(f"{name} es una marca ISO 8601")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise UnclosableRequestInvalid(f"{name} no es una marca ISO 8601") from None
    if moment.utcoffset() is None:
        raise UnclosableRequestInvalid(f"{name} debe llevar zona horaria")
    return to_millisecond(moment.astimezone(UTC))


def _text(value: object, field: FreeTextField) -> str:
    """Un texto del incerrable en NFC por la política base, dentro de los límites de su esquema."""
    if not isinstance(value, str):
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE)
    try:
        normalized = apply_base_policy(value, field)
    except FreeTextRejected:
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE) from None
    if len(normalized.strip()) < field.min_length:
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE)
    return normalized


def _decision_date(value: object) -> str:
    """La fecha de la decisión (campo 4): ``AAAA-MM-DD`` y un día que exista."""
    if not isinstance(value, str) or _DECISION_DATE.fullmatch(value) is None:
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE) from None
    return value


def _license(value: object) -> str | None:
    """La licencia profesional del firmante (§2.6): hasta 64 caracteres; sin ella no hay firma."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnclosableRequestInvalid("professional_license es una cadena")
    license_value = value.strip()
    if not license_value:
        return None
    if len(license_value) > _LICENSE_MAX_CHARS:
        raise UnclosableRequestInvalid("la licencia no cabe en 64 caracteres")
    return license_value


@dataclass(frozen=True, slots=True)
class FiveFields:
    """Los cinco campos de §2.7, ya validados (los textos en NFC)."""

    evaluated_alternative: str
    rejection_reason: str
    responsible_user_id: uuid.UUID
    decision_date: str
    compensatory_measure: str


def five_fields(
    *,
    evaluated_alternative: object,
    rejection_reason: object,
    responsible_user_id: object,
    decision_date: object,
    compensatory_measure: object,
) -> FiveFields:
    """Los cinco campos a la vez (BR-LAZ-52): falta cualquiera, ``unclosable_fields_incomplete``."""
    if type(responsible_user_id) is not uuid.UUID:
        raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_FIELDS_INCOMPLETE)
    return FiveFields(
        evaluated_alternative=_text(evaluated_alternative, EVALUATED_ALTERNATIVE_FIELD),
        rejection_reason=_text(rejection_reason, REJECTION_REASON_FIELD),
        responsible_user_id=responsible_user_id,
        decision_date=_decision_date(decision_date),
        compensatory_measure=_text(compensatory_measure, COMPENSATORY_MEASURE_FIELD),
    )


def review_date(
    value: object, at: datetime, *, default_months: int, max_months: int
) -> tuple[datetime, bool]:
    """La fecha de revisión y si está en rango (BR-LAZ-57): ``(at, at + max_months]``.

    Sin fecha, la de por defecto de la planta (6 meses). Todo sale del ``Clock``: ``at`` es el
    instante de la declaración o el de la revisión."""
    if value is None:
        return add_months(at, default_months), True
    moment = _instant(value, "review_due_at")
    return moment, at < moment <= add_months(at, max_months)


@dataclass(frozen=True, slots=True)
class DeclarationRow:
    """La fila de ``loop.unclosable_declaration`` que la revisión necesita."""

    declaration_id: uuid.UUID
    finding_state_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    review_due_at: datetime
    state: str
    declared_at: datetime
    policy_id: uuid.UUID
    policy_version: int
    policy_document_sha256: str


def declaration_row(row: Any) -> DeclarationRow:
    data = row._mapping
    return DeclarationRow(
        declaration_id=uuid.UUID(str(data["declaration_id"])),
        finding_state_id=uuid.UUID(str(data["finding_state_id"])),
        organization_id=uuid.UUID(str(data["organization_id"])),
        plant_id=uuid.UUID(str(data["plant_id"])),
        zone_id=uuid.UUID(str(data["zone_id"])),
        subject_kind=str(data["subject_kind"]),
        subject_id=uuid.UUID(str(data["subject_id"])),
        review_due_at=data["review_due_at"],
        state=str(data["state"]),
        declared_at=data["declared_at"],
        policy_id=uuid.UUID(str(data["policy_id"])),
        # La columna es texto (``plant_policy.version`` cabe en 32) y el registro de la revisión
        # cita la versión como entero (``PolicyVersion`` de §5.2): se convierte aquí.
        policy_version=int(data["policy_version"]),
        policy_document_sha256=str(data["policy_document_sha256"]),
    )


@dataclass(frozen=True, slots=True)
class _LoadedPolicy:
    """La política vigente tal como se copia al registro (§2.7; BR-LAZ-55)."""

    policy_id: uuid.UUID
    version: int
    signed_at: datetime
    document_sha256: str
    status: PolicyStatus

    @classmethod
    def of(cls, state: PlantPolicyState) -> _LoadedPolicy:
        """La política ``loaded`` completa; si el puerto la deja a medias, fallo cerrado."""
        if (
            state.policy_id is None
            or state.version is None
            or state.signed_at is None
            or state.document_sha256 is None
        ):
            raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE)
        return cls(
            policy_id=state.policy_id,
            version=state.version,
            signed_at=to_millisecond(state.signed_at),
            document_sha256=state.document_sha256,
            status=PolicyStatus.LOADED,
        )


# --- resultados -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclarationResult:
    """El recibo de la declaración y el estado nuevo del hallazgo (SUP-U05-12)."""

    finding_state_id: uuid.UUID
    declaration_id: uuid.UUID
    record_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    zone_id: uuid.UUID
    review_due_at: datetime
    policy_id: uuid.UUID
    policy_version: int
    policy_document_sha256: str
    transition_id: str
    lifecycle_state: FindingLifecycleState
    declared_at: datetime
    """La marca **exacta** del registro; a una persona se le devuelve ``presented_at``."""
    presented_at: datetime
    time_aggregated: bool


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """Lo que pide quien revisa, tal cual llega (se valida en ``review``)."""

    outcome: UnclosableReviewOutcome
    review_note: object
    new_review_due_at: object
    action: Mapping[str, Any] | None
    professional_license: object


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """El recibo de la revisión: renovada (T11) o convertida en acción (T12)."""

    finding_state_id: uuid.UUID
    declaration_id: uuid.UUID
    review_id: uuid.UUID
    record_id: uuid.UUID
    outcome: UnclosableReviewOutcome
    transition_id: str
    lifecycle_state: FindingLifecycleState
    new_review_due_at: datetime | None
    new_action_id: uuid.UUID | None
    new_action_record_id: uuid.UUID | None
    reviewed_at: datetime
    presented_at: datetime
    time_aggregated: bool


# --- el servicio ----------------------------------------------------------------------------------


@repository
class UnclosableService:
    """``loop.lifecycle``: declarar y revisar el estado incerrable, y su tarea diaria."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        clock: Clock,
        gates: GateQueryPort,
        actions: ActionService | None = None,
        configuration: PostgresLoopConfigurationRepository | None = None,
        outbox: OutboxPort | None = None,
        store: LoopProjectionStore | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._clock = clock
        self._gates = gates
        self._actions = actions if actions is not None else self._default_actions()
        self._configuration = (
            configuration
            if configuration is not None
            else PostgresLoopConfigurationRepository(database)
        )
        self._outbox = outbox
        self._store = store if store is not None else LoopProjectionStore()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "UnclosableService()"

    def _default_actions(self) -> ActionService:
        """El servicio de acciones con la misma infraestructura (la segunda mitad de T12)."""
        return ActionService(
            database=self._database,
            writer=self._writer,
            authorizer=self._authorizer,
            clock=self._clock,
        )

    # --- declarar ---------------------------------------------------------------------------------

    async def declare(
        self,
        context: ScopeContext,
        finding_state_id: uuid.UUID,
        *,
        evaluated_alternative: object,
        rejection_reason: object,
        responsible_user_id: object,
        decision_date: object,
        compensatory_measure: object,
        review_due_at: object = None,
        professional_license: object = None,
    ) -> DeclarationResult:
        """Declara SIN ALTERNATIVA SEGURA DISPONIBLE el hallazgo ``finding_state_id``."""
        authorized, place = await self._authorized(context, finding_state_id)
        # El orden de los pasos es el de ``business-logic-model.md`` §8.2: la política (paso 2)
        # antes que los cinco campos (paso 3). Así el fallo cerrado del puerto (BR-LAZ-54) es
        # incondicional: sin respuesta del puerto no hay declaración, ni con el cuerpo a medias.
        policy = await self._policy(authorized, place)
        fields = five_fields(
            evaluated_alternative=evaluated_alternative,
            rejection_reason=rejection_reason,
            responsible_user_id=responsible_user_id,
            decision_date=decision_date,
            compensatory_measure=compensatory_measure,
        )
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, place.organization_id, place.subject_id)
            current = await read_finding(transaction, finding_state_id)
            if current is None or current.subject_id != place.subject_id:
                raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
            responsible = await self._responsible(transaction, current, fields.responsible_user_id)
            at = to_millisecond(self._clock.now())
            settings, _version = await self._configuration.settings_in(
                transaction, current.plant_id
            )
            due_at, in_range = review_date(
                review_due_at,
                at,
                default_months=settings["unclosable_review_default_months"],
                max_months=settings["unclosable_review_max_months"],
            )
            license_value = _license(professional_license)
            accepted = self._declare_transition(
                current, policy, in_range=in_range, license=license_value
            )
            return await self._write_declaration(
                transaction,
                writer_context,
                current,
                fields,
                responsible,
                policy,
                due_at,
                license_value,
                accepted,
                at,
            )

    @staticmethod
    def _declare_transition(
        current: LoopFinding, policy: _LoadedPolicy, *, in_range: bool, license: str | None
    ) -> Accepted:
        """Paso 6: la fila T10 de §4.1 con los hechos ya comprobados."""
        result = transition(
            current.snapshot,
            DeclareUnclosable(
                fields_complete=True,
                policy=policy.status,
                review_date_in_range=in_range,
                signer_has_license=license is not None,
            ),
        )
        if isinstance(result, Rejected):
            raise UnclosableRejected(_DECLARE_CODES[result.code])
        return result

    async def _write_declaration(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: LoopFinding,
        fields: FiveFields,
        responsible: tuple[uuid.UUID, str],
        policy: _LoadedPolicy,
        due_at: datetime,
        license_value: str | None,
        accepted: Accepted,
        at: datetime,
    ) -> DeclarationResult:
        """Paso 7: el registro, su evento, la proyección y la fila de la declaración."""
        declaration_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        signer = _signer_snapshot(context, license_value)
        content: dict[str, Any] = {
            "declaration_id": str(declaration_id),
            "subject_kind": current.subject_kind,
            "subject_id": str(current.subject_id),
            "anchor_record_id": str(current.anchor_record_id),
            "evaluated_alternative": fields.evaluated_alternative,
            "rejection_reason": fields.rejection_reason,
            "responsible_user_id": str(responsible[0]),
            "responsible_display_name": responsible[1],
            "decision_date": fields.decision_date,
            "compensatory_measure": fields.compensatory_measure,
            "review_due_at": format_timestamp(due_at),
            "policy": {
                "policy_id": str(policy.policy_id),
                "version": policy.version,
                "signed_at": format_timestamp(policy.signed_at),
                "document_sha256": policy.document_sha256,
            },
            "signer": signer,
            "declared_at": format_timestamp(at),
        }
        event = NewEvent(
            event_name=FINDING_UNCLOSABLE,
            payload={
                "zone_id": str(current.zone_id),
                "subject_kind": current.subject_kind,
                "subject_id": str(current.subject_id),
                "declaration_id": str(declaration_id),
                "record_id": str(record_id),
                "review_due_at": format_timestamp(due_at),
            },
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            UNCLOSABLE_STATE_DECLARED,
            content,
            scope=current.scope,
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(result)
        finding = step.finding
        if (
            not step.applied
            or finding is None
            or step.transition_id != accepted.transition.transition_id
        ):
            raise RuntimeError("la proyección no aplicó la declaración recién escrita")
        await transaction.execute(
            _INSERT_DECLARATION,
            {
                "declaration_id": declaration_id,
                "organization_id": current.organization_id,
                "plant_id": current.plant_id,
                "zone_id": current.zone_id,
                "subject_kind": current.subject_kind,
                "subject_id": current.subject_id,
                "record_id": receipt.record_id,
                "evaluated_alternative": fields.evaluated_alternative,
                "rejection_reason": fields.rejection_reason,
                "responsible_user_id": responsible[0],
                "decision_date": date.fromisoformat(fields.decision_date),
                "compensatory_measure": fields.compensatory_measure,
                "review_due_at": due_at,
                "policy_id": policy.policy_id,
                "policy_version": str(policy.version),
                "policy_document_sha256": policy.document_sha256,
                "signer": json.dumps(signer, ensure_ascii=False, allow_nan=False),
                "declared_at": at,
            },
        )
        return DeclarationResult(
            finding_state_id=current.finding_state_id,
            declaration_id=declaration_id,
            record_id=receipt.record_id,
            subject_kind=current.subject_kind,
            subject_id=current.subject_id,
            zone_id=current.zone_id,
            review_due_at=due_at,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            policy_document_sha256=policy.document_sha256,
            transition_id=accepted.transition.transition_id,
            lifecycle_state=finding.state,
            declared_at=at,
            presented_at=current.presented(at),
            time_aggregated=current.single_occupancy_zone,
        )

    # --- revisar ----------------------------------------------------------------------------------

    async def review(
        self,
        context: ScopeContext,
        declaration_id: uuid.UUID,
        *,
        outcome: object,
        review_note: object,
        new_review_due_at: object = None,
        action: Mapping[str, Any] | None = None,
        professional_license: object = None,
    ) -> ReviewResult:
        """Revisa la declaración ``declaration_id``: la renueva (T11) o la convierte en acción."""
        chosen = review_outcome(outcome)
        request = ReviewRequest(
            chosen, review_note, new_review_due_at, action, professional_license
        )
        authorized, place, row = await self._authorized_declaration(context, declaration_id)
        if chosen is UnclosableReviewOutcome.CONVERTED_TO_ACTION:
            await self._authorize_actions(authorized, place)
        note = _text(review_note, REVIEW_NOTE_FIELD)
        license_value = _license(professional_license)
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await lock_finding(transaction, row.organization_id, row.subject_id)
            locked = action_rows(
                await lock_actions(transaction, row.organization_id, row.plant_id, row.subject_id)
            )
            current = await self._current_finding(transaction, row)
            at = to_millisecond(self._clock.now())
            accepted = self._review_transition(current, chosen)
            settings, _version = await self._configuration.settings_in(transaction, row.plant_id)
            due_at = self._review_due(request, at, settings)
            if license_value is None:
                raise UnclosableRejected(LoopDetailCode.SIGNER_LICENSE_MISSING)
            return await self._write_review(
                transaction,
                writer_context,
                current,
                row,
                request,
                note,
                license_value,
                due_at,
                locked,
                accepted,
                at,
            )

    def _review_due(self, request: ReviewRequest, at: datetime, settings: Any) -> datetime | None:
        """La fecha nueva de la renovación, en rango (BR-LAZ-57); en una conversión no hay fecha."""
        if request.outcome is UnclosableReviewOutcome.CONVERTED_TO_ACTION:
            if request.new_review_due_at is not None:
                raise UnclosableRequestInvalid("new_review_due_at solo va con renewed")
            return None
        due_at, in_range = review_date(
            request.new_review_due_at,
            at,
            default_months=settings["unclosable_review_default_months"],
            max_months=settings["unclosable_review_max_months"],
        )
        if not in_range:
            raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE)
        return due_at

    @staticmethod
    def _review_transition(current: LoopFinding, outcome: UnclosableReviewOutcome) -> Accepted:
        """La fila T11 o T12 de §4.1; sin una declaración vigente, ``transition_not_allowed``."""
        result = transition(current.snapshot, ReviewUnclosable(outcome))
        if isinstance(result, Rejected):
            raise UnclosableRejected(LoopDetailCode.TRANSITION_NOT_ALLOWED)
        return result

    async def _write_review(
        self,
        transaction: Transaction,
        context: ScopeContext,
        current: LoopFinding,
        row: DeclarationRow,
        request: ReviewRequest,
        note: str,
        license_value: str,
        due_at: datetime | None,
        locked: Mapping[uuid.UUID, Any],
        accepted: Accepted,
        at: datetime,
    ) -> ReviewResult:
        """El registro ``unclosable_review``, su evento, la proyección y —en T12— la acción."""
        review_id = uuid7(self._clock, self._random_bytes)
        record_id = uuid7(self._clock, self._random_bytes)
        action_id = uuid7(self._clock, self._random_bytes)
        signer = _signer_snapshot(context, license_value)
        content: dict[str, Any] = {
            "review_id": str(review_id),
            "declaration_id": str(row.declaration_id),
            "subject_id": str(current.subject_id),
            "outcome": request.outcome.value,
            "review_note": note,
            "policy": {
                "policy_id": str(row.policy_id),
                "version": row.policy_version,
                "document_sha256": row.policy_document_sha256,
            },
            "reviewer": signer,
            "reviewed_at": format_timestamp(at),
        }
        if due_at is not None:
            content["new_review_due_at"] = format_timestamp(due_at)
        if request.outcome is UnclosableReviewOutcome.CONVERTED_TO_ACTION:
            content["new_action_id"] = str(action_id)
        event = NewEvent(
            event_name=UNCLOSABLE_REVIEWED,
            payload={
                "zone_id": str(current.zone_id),
                "subject_id": str(current.subject_id),
                "declaration_id": str(row.declaration_id),
                "review_id": str(review_id),
                "record_id": str(record_id),
                "outcome": request.outcome.value,
            },
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            context,
            UNCLOSABLE_REVIEW,
            content,
            scope=current.scope,
            events=(event,),
            occurred_at=at,
            record_id=record_id,
        )
        receipt = _written(result)
        finding = step.finding
        if (
            not step.applied
            or finding is None
            or step.transition_id != accepted.transition.transition_id
        ):
            raise RuntimeError("la proyección no aplicó la revisión recién escrita")
        await transaction.execute(
            _MARK_DECLARATION,
            {
                "organization_id": row.organization_id,
                "declaration_id": row.declaration_id,
                "state": "renewed"
                if request.outcome is UnclosableReviewOutcome.RENEWED
                else "converted",
                "review_due_at": due_at if due_at is not None else row.review_due_at,
            },
        )
        action_record_id: uuid.UUID | None = None
        lifecycle = finding.state
        if request.outcome is UnclosableReviewOutcome.CONVERTED_TO_ACTION:
            action_record_id, lifecycle = await self._convert(
                transaction, context, row, request, action_id, locked
            )
        await transaction.execute(
            _INSERT_REVIEW,
            {
                "review_id": review_id,
                "organization_id": row.organization_id,
                "plant_id": row.plant_id,
                "zone_id": row.zone_id,
                "declaration_id": row.declaration_id,
                "record_id": receipt.record_id,
                "outcome": request.outcome.value,
                "review_note": note,
                "new_review_due_at": due_at,
                "new_action_id": (
                    action_id
                    if request.outcome is UnclosableReviewOutcome.CONVERTED_TO_ACTION
                    else None
                ),
                "reviewed_by": json.dumps(signer, ensure_ascii=False, allow_nan=False),
                "reviewed_at": at,
            },
        )
        return ReviewResult(
            finding_state_id=current.finding_state_id,
            declaration_id=row.declaration_id,
            review_id=review_id,
            record_id=receipt.record_id,
            outcome=request.outcome,
            transition_id=accepted.transition.transition_id,
            lifecycle_state=lifecycle,
            new_review_due_at=due_at,
            new_action_id=action_id if action_record_id is not None else None,
            new_action_record_id=action_record_id,
            reviewed_at=at,
            presented_at=current.presented(at),
            time_aggregated=current.single_occupancy_zone,
        )

    async def _convert(
        self,
        transaction: Transaction,
        context: ScopeContext,
        row: DeclarationRow,
        request: ReviewRequest,
        action_id: uuid.UUID,
        locked: Mapping[uuid.UUID, Any],
    ) -> tuple[uuid.UUID, FindingLifecycleState]:
        """La segunda mitad de T12: el ``action_assigned`` por el servicio de TASK-311.

        El hallazgo ya está ``action_assigned`` con ``current_action_id`` y sin plazo (la
        proyección de la revisión), así que la asignación **completa** el registro con
        ``pending_action_id``: sin otra transición."""
        payload = request.action
        if payload is None:
            raise UnclosableRequestInvalid("converted_to_action exige la acción")
        updated = await read_finding(transaction, row.finding_state_id)
        if updated is None:  # pragma: no cover - la proyección no borra filas
            raise ResourceNotFound()
        assignment = AssignmentRequest(
            action_type=payload.get("action_type"),
            action_description=payload.get("action_description"),
            expected_outcome=payload.get("expected_outcome"),
            assignee_user_id=payload.get("assignee_user_id"),
            deadline_days=payload.get("deadline_days"),
            supersedes_action_id=None,
        )
        try:
            result = await self._actions.assign_in_transaction(
                transaction, context, updated, locked, assignment, pending_action_id=action_id
            )
        except ActionRejected as rejected:
            raise UnclosableRejected(rejected.detail_code, rejected.hint) from None
        except ActionUnavailable:
            raise UnclosableUnavailable("ledger_chain") from None
        return result.record_id, result.lifecycle_state

    # --- la tarea diaria --------------------------------------------------------------------------

    async def mark_due(self, transaction: Transaction) -> int:
        """Marca ``pending_review`` las declaraciones vencidas de la organización de la transacción.

        Devuelve cuántas marcó (0 si ninguna o si ya estaban marcadas: la segunda ejecución no
        marca ni publica dos veces). No cambia el estado del ciclo de vida (BR-LAZ-58)."""
        at = to_millisecond(self._clock.now())
        marked = await self._store.mark_unclosable_pending_review(transaction, at=at)
        if not marked:
            return 0
        outbox = self._outbox
        if outbox is None:
            raise RuntimeError("la tarea unclosable_review_due necesita la bandeja de salida")
        for due in marked:
            declaration_id = await self._declaration_of(transaction, due)
            await transaction.execute(
                _MARK_DECLARATION,
                {
                    "organization_id": transaction.context.organization_id,
                    "declaration_id": declaration_id,
                    "state": "pending_review",
                    "review_due_at": due.unclosable_review_due_at,
                },
            )
            await outbox.publish(
                transaction,
                NewEvent(
                    event_name=UNCLOSABLE_REVIEW_DUE,
                    plant_id=due.plant_id,
                    payload={
                        "zone_id": str(due.zone_id),
                        "subject_id": str(due.subject_id),
                        "declaration_id": str(declaration_id),
                        "review_due_at": format_timestamp(due.unclosable_review_due_at),
                    },
                ),
            )
        return len(marked)

    @staticmethod
    async def _declaration_of(transaction: Transaction, due: ReviewDueRow) -> uuid.UUID:
        """La declaración vigente de la fila vencida, por su sujeto y sus dos marcas."""
        row = (
            await transaction.execute(
                _DECLARATION_OF_SUBJECT,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": due.plant_id,
                    "zone_id": due.zone_id,
                    "subject_kind": due.subject_kind,
                    "subject_id": due.subject_id,
                    "declared_at": due.unclosable_since,
                    "review_due_at": due.unclosable_review_due_at,
                },
            )
        ).first()
        if row is None:  # pragma: no cover - el pliegue declara solo con su registro
            raise RuntimeError("un hallazgo unclosable sin su declaración vigente")
        return uuid.UUID(str(row.declaration_id))

    # --- pasos comunes ----------------------------------------------------------------------------

    async def _authorized(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding]:
        """Paso 1: la fila de la organización y ``findings.close_and_sign`` sobre su zona."""
        if not isinstance(context, ScopeContext) or type(finding_state_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _FINDING_BY_ID,
            {"organization_id": context.organization_id, "finding_state_id": finding_state_id},
        )
        if not rows:
            raise ResourceNotFound()
        place = finding_from(rows[0])
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FINDINGS_CLOSE_AND_SIGN, place.resource()
        )
        return authorized, place

    async def _authorized_declaration(
        self, context: ScopeContext, declaration_id: uuid.UUID
    ) -> tuple[ScopeContext, LoopFinding, DeclarationRow]:
        """El alcance de la revisión: la declaración y el hallazgo de su sujeto."""
        if not isinstance(context, ScopeContext) or type(declaration_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(
            context,
            _DECLARATION,
            {"organization_id": context.organization_id, "declaration_id": declaration_id},
        )
        if not rows:
            raise ResourceNotFound()
        row = declaration_row(rows[0])
        place = await self._place(context, row)
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FINDINGS_CLOSE_AND_SIGN, place.resource()
        )
        return authorized, place, row

    async def _place(self, context: ScopeContext, row: DeclarationRow) -> LoopFinding:
        rows = await self._database.read(
            context,
            _FINDING_BY_ID,
            {"organization_id": row.organization_id, "finding_state_id": row.finding_state_id},
        )
        if not rows:  # pragma: no cover - una declaración siempre tiene su fila
            raise ResourceNotFound()
        return finding_from(rows[0])

    async def _authorize_actions(self, authorized: ScopeContext, place: LoopFinding) -> None:
        """T12 exige además ``actions.manage`` sobre la zona (la fila T12 de §4.1)."""
        allowed = await self._authorizer.authorize(
            authorized, PermissionKey.ACTIONS_MANAGE, place.resource()
        )
        if allowed.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(allowed, PermissionKey.ACTIONS_MANAGE, place.resource())

    async def _current_finding(self, transaction: Transaction, row: DeclarationRow) -> LoopFinding:
        current = await read_finding(transaction, row.finding_state_id)
        if current is None or current.subject_id != row.subject_id:
            raise ResourceNotFound()  # pragma: no cover - la proyección no borra filas
        return current

    async def _responsible(
        self, transaction: Transaction, current: LoopFinding, user_id: uuid.UUID
    ) -> tuple[uuid.UUID, str]:
        """El responsable (campo 3): un usuario activo de la organización; si no, ``not_found``."""
        row = (
            await transaction.execute(
                _RESPONSIBLE,
                {"organization_id": current.organization_id, "user_id": user_id},
            )
        ).first()
        if row is None:
            raise ResourceNotFound()
        return uuid.UUID(str(row.user_id)), str(row.display_name)

    async def _policy(self, context: ScopeContext, place: LoopFinding) -> _LoadedPolicy:
        """Paso 3: la política de la planta, con fallo **cerrado** si el puerto no responde."""
        try:
            state = await self._gates.plant_policy(context, place.plant_id)
        except Exception as error:
            _log.warning(
                "política de planta sin respuesta: la declaración se rechaza",
                plant_id=str(place.plant_id),
                error=type(error).__name__,
            )
            raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_POLICY_UNAVAILABLE) from None
        status = policy_status(state)
        if status is PolicyStatus.NOT_SIGNED:
            raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_POLICY_NOT_SIGNED)
        if status is PolicyStatus.REVOKED:
            raise UnclosableRejected(LoopDetailCode.UNCLOSABLE_POLICY_REVOKED)
        return _LoadedPolicy.of(state)


def _written(result: Receipt | LedgerRejection) -> Receipt:
    """El recibo de la escritura o el rechazo traducido al vocabulario del incerrable.

    Los dos textos libres de §5.1 y la licencia del firmante los comprueba el expediente al
    escribir: un rechazo de texto libre conserva su ``detail_code`` (los dos validadores de U-04)
    y una cadena que no se pudo tomar a tiempo sale como ``UnclosableUnavailable`` (503)."""
    try:
        return written(result, UnclosableUnavailable)
    except ActionRejected as rejected:
        raise UnclosableRejected(rejected.detail_code, rejected.hint) from None
    except ActionRequestInvalid:
        raise UnclosableRequestInvalid("texto libre rechazado por el expediente") from None


def _signer_snapshot(context: ScopeContext, license_value: str | None) -> dict[str, Any]:
    """``LicensedSigner``: usuario, nombre en el instante y licencia (BR-LAZ-59)."""
    actor = context.actor
    if actor.role_in_use is None:  # pragma: no cover - authorize fija el rol en uso
        raise ResourceNotFound()
    return {
        "user_id": str(actor.id),
        "display_name": actor.display_name_snapshot,
        "professional_license": license_value,
    }


def register_unclosable_review_due(
    registry: PeriodicTaskRegistry, service: UnclosableService
) -> PeriodicTask:
    """Registra ``unclosable_review_due`` (diaria, por organización) en el planificador."""

    async def handler(transaction: Transaction) -> None:
        await service.mark_due(transaction)

    return registry.register(
        TASK_NAME,
        SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )
