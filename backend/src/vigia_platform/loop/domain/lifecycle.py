"""Máquina de estados del hallazgo (domain-entities.md de U-04, §4.1, §4.2 y §4.3; BR-LAZ-36).

Módulo **puro** (PAT-LAZ-SEG-09): sin FastAPI, sin SQLAlchemy, sin reloj y sin E/S. Lo usan la
clasificación, la cola, el ciclo de vida y el pliegue de la proyección (``loop.projection.fold``):
la proyección **es** esta tabla aplicada a los registros del expediente, y es el modelo que
PR-LAZ-10 (TASK-314) contrasta con el sistema.

- ``TRANSITIONS``: las 21 filas de §4.1, cada una con su origen, su destino, sus claves de
  permiso, los registros que escribe y los eventos que publica.
- ``FORBIDDEN_TRANSITIONS``: las filas de §4.2 (transiciones que no existen), con el código que
  la tabla fija o ``transition_not_allowed``.
- ``transition(snapshot, command)``: el estado nuevo (``Accepted``, con la fila aplicada) o el
  rechazo (``Rejected``) **sin efecto**. Una transición que no figura en §4.1 se rechaza con
  ``transition_not_allowed`` (BR-LAZ-36) salvo donde §1.2 y §4.2 fijan un código de negocio
  (``closure_requires_assigned_action``, ``closure_evidence_missing``, ``signer_license_missing``,
  los de incerrable, ``reopening_reason_required`` y ``reason_required``).

Las comprobaciones que dependen de datos que no son el estado (motivo validado, responsable con
alcance, plazo en rango, cinco campos, política) las hace la operación que ejecuta la transición;
aquí llegan como **hechos** del comando (``fields_complete``, ``policy``…), para que la tabla
diga qué código corresponde a cada uno. Las claves de permiso y el alcance sobre la zona los
exige la ruta (BR-NUC-13); la tabla los declara para que la metapropiedad de §4.2 (ninguna
transición del mando de línea, del administrador ni del proveedor) se compruebe contra la matriz.

Los códigos (``LifecycleCode``) son los nombres de ``loop_error_code`` de §1.2 **sin prefijo**:
``loop.detail_codes`` (que importa la capa web) los publica como ``loop_<código>``.

T16 (``suspended_by_reclassification`` → estado anterior): con ``previous_state`` ``open`` se
vuelve a ``open``; con ``action_assigned`` u ``overdue`` el vencimiento se **recalcula** por
``due_at`` contra el instante de la reclasificación (``Classify.at``): ``overdue`` si el plazo
ya pasó, ``action_assigned`` si no.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    FindingLifecycleState,
    ReviewResolutionOutcome,
    UnclosableReviewOutcome,
)

__all__ = [
    "ACTIVE_LOOP_STATES",
    "FORBIDDEN_TRANSITIONS",
    "TRANSITIONS",
    "Accepted",
    "AssignAction",
    "AttachLateSegment",
    "Classify",
    "Close",
    "Command",
    "DeclareOverdue",
    "DeclareUnclosable",
    "Destination",
    "ForbiddenTransition",
    "LifecycleCode",
    "LifecycleSnapshot",
    "PolicyStatus",
    "ReceiveDetection",
    "ReceiveFinding",
    "Rejected",
    "Reopen",
    "ResolveReview",
    "Result",
    "ReviewUnclosable",
    "Transition",
    "transition",
]

S = FindingLifecycleState


class LifecycleCode(enum.StrEnum):
    """Códigos de rechazo de la máquina (``loop_error_code`` de §1.2, sin prefijo)."""

    TRANSITION_NOT_ALLOWED = "transition_not_allowed"
    REASON_REQUIRED = "reason_required"
    CLOSURE_REQUIRES_ASSIGNED_ACTION = "closure_requires_assigned_action"
    CLOSURE_EVIDENCE_MISSING = "closure_evidence_missing"
    SIGNER_LICENSE_MISSING = "signer_license_missing"
    UNCLOSABLE_FIELDS_INCOMPLETE = "unclosable_fields_incomplete"
    UNCLOSABLE_POLICY_NOT_SIGNED = "unclosable_policy_not_signed"
    UNCLOSABLE_POLICY_UNAVAILABLE = "unclosable_policy_unavailable"
    UNCLOSABLE_POLICY_REVOKED = "unclosable_policy_revoked"
    UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE = "unclosable_review_date_out_of_range"
    REOPENING_REASON_REQUIRED = "reopening_reason_required"


class Destination(enum.StrEnum):
    """Destinos que no son un estado fijo: el mismo (T11, T18 a T21) o el anterior (T16)."""

    SAME = "same"
    PREVIOUS = "previous"


class PolicyStatus(enum.StrEnum):
    """La política de incerrables de la planta según ``GateQueryPort.plant_policy`` (§2.7)."""

    LOADED = "loaded"
    NOT_SIGNED = "not_signed"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"
    """El puerto no respondió: fallo cerrado (RNF-SEG-15)."""


ACTIVE_LOOP_STATES: Final = frozenset({S.OPEN, S.ACTION_ASSIGNED, S.OVERDUE})
"""Lazo abierto: lo que una reclasificación suspende (T15) y desde donde se declara incerrable."""


# --- comandos -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiveFinding:
    """Evento ``finding_received`` de U-03 (T01)."""


@dataclass(frozen=True, slots=True)
class ReceiveDetection:
    """Evento ``detection_for_review_received`` de U-03 (T02)."""


@dataclass(frozen=True, slots=True)
class Classify:
    """Clasificación o reclasificación (T03, T04, T15 a T20).

    ``reinforced_reason`` es obligatorio en toda reclasificación (§2.3); ``at`` es el instante
    de la clasificación (``classified_at``), con el que T16 recalcula el vencimiento."""

    outcome: ClassificationOutcome
    reinforced_reason: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class ResolveReview:
    """Resolución de un elemento de la cola de revisión (T05, T06)."""

    outcome: ReviewResolutionOutcome


@dataclass(frozen=True, slots=True)
class AssignAction:
    """Asignación de una acción con responsable y plazo (T07)."""


@dataclass(frozen=True, slots=True)
class DeclareOverdue:
    """La tarea ``evaluate_deadlines`` declara el plazo vencido (T08): declara un hecho."""


@dataclass(frozen=True, slots=True)
class Close:
    """Cierre firmado (T09): al menos una entrada de evidencia y firmante con licencia."""

    evidence_entry_count: int
    signer_has_license: bool


@dataclass(frozen=True, slots=True)
class DeclareUnclosable:
    """SIN ALTERNATIVA SEGURA DISPONIBLE (T10): cinco campos, política y fecha en rango."""

    fields_complete: bool
    policy: PolicyStatus
    review_date_in_range: bool
    signer_has_license: bool


@dataclass(frozen=True, slots=True)
class ReviewUnclosable:
    """Revisión de una declaración vigente (T11 renueva, T12 convierte en acción)."""

    outcome: UnclosableReviewOutcome


@dataclass(frozen=True, slots=True)
class Reopen:
    """Reapertura de un cerrado (T13) o de un incerrable (T14) con motivo obligatorio."""

    reason_given: bool


@dataclass(frozen=True, slots=True)
class AttachLateSegment:
    """Llega un tramo tardío del mismo episodio (T21): no reabre el lazo."""


type Command = (
    ReceiveFinding
    | ReceiveDetection
    | Classify
    | ResolveReview
    | AssignAction
    | DeclareOverdue
    | Close
    | DeclareUnclosable
    | ReviewUnclosable
    | Reopen
    | AttachLateSegment
)
"""Lista cerrada de comandos: no existe comando de borrar, archivar, ocultar, caducar ni
reevaluar con otra versión del estándar (§4.2)."""


# --- la tabla de §4.1 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Transition:
    """Una fila de §4.1."""

    transition_id: str
    origins: frozenset[FindingLifecycleState | None]
    """``None`` es «—»: el sujeto aún no existe en la proyección (T01, T02)."""
    destination: FindingLifecycleState | Destination
    permission_keys: tuple[PermissionKey, ...]
    """Vacía: la ejecuta un evento de U-03 o la tarea periódica, no una persona."""
    record_types: tuple[str, ...]
    """Registros que escribe; vacía si el registro lo escribió U-03."""
    events: tuple[str, ...]
    """Eventos que publica."""


_K = PermissionKey
_ALL_STATES: Final = frozenset(FindingLifecycleState)


def _row(
    transition_id: str,
    origins: frozenset[FindingLifecycleState | None] | set[FindingLifecycleState | None],
    destination: FindingLifecycleState | Destination,
    permission_keys: tuple[PermissionKey, ...],
    record_types: tuple[str, ...],
    events: tuple[str, ...],
) -> tuple[str, Transition]:
    return transition_id, Transition(
        transition_id, frozenset(origins), destination, permission_keys, record_types, events
    )


TRANSITIONS: Final[Mapping[str, Transition]] = MappingProxyType(
    dict(
        (
            _row("T01", {None}, S.RECEIVED, (), (), ()),
            _row("T02", {None}, S.IN_REVIEW, (), (), ()),
            _row(
                "T03",
                {S.RECEIVED},
                S.OPEN,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_confirmed",),
            ),
            _row(
                "T04",
                {S.RECEIVED},
                S.DISCARDED,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_discarded",),
            ),
            _row(
                "T05",
                {S.IN_REVIEW},
                S.OPEN,
                (_K.REVIEW_QUEUE_RESOLVE,),
                ("review_resolution",),
                ("review_confirmed",),
            ),
            _row(
                "T06",
                {S.IN_REVIEW},
                S.DISCARDED,
                (_K.REVIEW_QUEUE_RESOLVE,),
                ("review_resolution",),
                ("review_discarded",),
            ),
            _row(
                "T07",
                {S.OPEN},
                S.ACTION_ASSIGNED,
                (_K.ACTIONS_MANAGE,),
                ("action_assigned",),
                ("action_assigned",),
            ),
            _row(
                "T08",
                {S.ACTION_ASSIGNED},
                S.OVERDUE,
                (),
                ("action_overdue",),
                ("finding_overdue",),
            ),
            _row(
                "T09",
                {S.ACTION_ASSIGNED, S.OVERDUE},
                S.CLOSED,
                (_K.FINDINGS_CLOSE_AND_SIGN,),
                ("closure",),
                ("finding_closed",),
            ),
            _row(
                "T10",
                {S.OPEN, S.ACTION_ASSIGNED, S.OVERDUE},
                S.UNCLOSABLE,
                (_K.FINDINGS_CLOSE_AND_SIGN,),
                ("unclosable_state_declared",),
                ("finding_unclosable",),
            ),
            _row(
                "T11",
                {S.UNCLOSABLE},
                Destination.SAME,
                (_K.FINDINGS_CLOSE_AND_SIGN,),
                ("unclosable_review",),
                ("unclosable_reviewed",),
            ),
            _row(
                "T12",
                {S.UNCLOSABLE},
                S.ACTION_ASSIGNED,
                (_K.FINDINGS_CLOSE_AND_SIGN, _K.ACTIONS_MANAGE),
                ("unclosable_review", "action_assigned"),
                ("unclosable_reviewed", "action_assigned"),
            ),
            _row(
                "T13",
                {S.CLOSED},
                S.ACTION_ASSIGNED,
                (_K.ACTIONS_MANAGE,),
                ("reopening", "action_assigned"),
                ("finding_reopened", "action_assigned"),
            ),
            _row(
                "T14",
                {S.UNCLOSABLE},
                S.ACTION_ASSIGNED,
                (_K.ACTIONS_MANAGE,),
                ("reopening", "action_assigned"),
                ("finding_reopened", "action_assigned"),
            ),
            _row(
                "T15",
                set(ACTIVE_LOOP_STATES),
                S.SUSPENDED_BY_RECLASSIFICATION,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("loop_suspended",),
            ),
            _row(
                "T16",
                {S.SUSPENDED_BY_RECLASSIFICATION},
                Destination.PREVIOUS,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("loop_resumed",),
            ),
            _row(
                "T17",
                {S.DISCARDED},
                S.OPEN,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_confirmed",),
            ),
            _row(
                "T18",
                {S.DISCARDED},
                Destination.SAME,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_reclassified",),
            ),
            _row(
                "T19",
                {S.CLOSED},
                Destination.SAME,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_reclassified",),
            ),
            _row(
                "T20",
                {S.UNCLOSABLE},
                Destination.SAME,
                (_K.FINDINGS_CLASSIFY,),
                ("classification",),
                ("finding_reclassified",),
            ),
            _row(
                "T21",
                set(_ALL_STATES),
                Destination.SAME,
                (),
                ("late_segment_attached",),
                ("late_segment_attached",),
            ),
        )
    )
)
"""Las 21 transiciones que existen (§4.1), por identificador."""


# --- la tabla de §4.2 -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ForbiddenTransition:
    """Una fila de §4.2: lo que no existe y el código con el que se rechaza, si lo tiene."""

    forbidden_id: str
    description_es: str
    source: str
    code: LifecycleCode | None
    """Código que fija la tabla; ``None`` si la prohibición no es un comando que exista (no hay
    comando de borrar, ni de caducar, ni clave que la conceda)."""


FORBIDDEN_TRANSITIONS: Final[tuple[ForbiddenTransition, ...]] = (
    ForbiddenTransition(
        "F01", "Cualquier estado a borrado, archivado u ocultamiento", "P4, RF-PLA-08, H-22", None
    ),
    ForbiddenTransition(
        "F02",
        "received a closed (cerrar sin clasificar)",
        "RF-PLA-02, P5",
        LifecycleCode.CLOSURE_REQUIRES_ASSIGNED_ACTION,
    ),
    ForbiddenTransition(
        "F03",
        "open a closed (cerrar sin acción asignada)",
        "H-25",
        LifecycleCode.CLOSURE_REQUIRES_ASSIGNED_ACTION,
    ),
    ForbiddenTransition(
        "F04",
        "action_assigned a closed sin evidencia o sin licencia del firmante",
        "H-25, RF-PLA-02",
        LifecycleCode.CLOSURE_EVIDENCE_MISSING,
    ),
    ForbiddenTransition(
        "F05",
        "in_review a discarded por antigüedad, sin decisión humana",
        "RF-PLA-03, H-05, H-23",
        None,
    ),
    ForbiddenTransition("F06", "received a discarded por antigüedad", "RF-PLA-01, H-21", None),
    ForbiddenTransition(
        "F07", "overdue a cualquier estado que lo oculte, archive o cierre solo", "P5, H-27", None
    ),
    ForbiddenTransition(
        "F08",
        "unclosable a open automática al llegar la fecha de revisión",
        "RF-PLA-02, P5, respuesta 7",
        None,
    ),
    ForbiddenTransition(
        "F09",
        "Cualquier estado a unclosable sin política firmada o con ella revocada",
        "H-26, H13",
        LifecycleCode.UNCLOSABLE_POLICY_NOT_SIGNED,
    ),
    ForbiddenTransition(
        "F10",
        "Cualquier estado a unclosable con el puerto de política caído",
        "RNF-SEG-15",
        LifecycleCode.UNCLOSABLE_POLICY_UNAVAILABLE,
    ),
    ForbiddenTransition(
        "F11",
        "Edición o retirada de un cierre, una declaración o una clasificación anteriores",
        "P4, H-22",
        None,
    ),
    ForbiddenTransition(
        "F12", "Cualquier transición ejecutada por el mando de línea", "H12, H-54", None
    ),
    ForbiddenTransition(
        "F13", "Cualquier transición ejecutada por el administrador", "RF-PLA-11", None
    ),
    ForbiddenTransition(
        "F14", "Cualquier transición ejecutada por el proveedor bajo concesión", "BR-NUC-37", None
    ),
    ForbiddenTransition(
        "F15",
        "Reevaluación retroactiva con una versión posterior del estándar",
        "H-16, BR-CTR-08",
        None,
    ),
    ForbiddenTransition(
        "F16",
        "Asignación de una acción a una persona identificada como causante",
        "H-24, P7, H11",
        None,
    ),
)
"""Las filas de §4.2. F04 también se rechaza con ``signer_license_missing`` (sin licencia) y F09
con ``unclosable_policy_revoked`` (revocada); F16 la rechaza el validador de texto de la acción
(``action_on_person_forbidden``, TASK-311), no la máquina: ``AssignAction`` no tiene ningún campo
que pueda nombrar a una persona."""


# --- resultado ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleSnapshot:
    """Lo que la máquina necesita del sujeto: estado, estado anterior (solo suspendido) y plazo.

    ``state`` ``None`` significa que el sujeto aún no existe en la proyección."""

    state: FindingLifecycleState | None
    previous_state: FindingLifecycleState | None = None
    due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Accepted:
    """La transición existe: el estado nuevo y la fila de §4.1 aplicada."""

    transition: Transition
    state: FindingLifecycleState
    previous_state: FindingLifecycleState | None
    """Solo con ``suspended_by_reclassification``: el estado que T16 reanudará."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """La transición no existe o le falta lo que la tabla exige: sin efecto y sin registro."""

    code: LifecycleCode


type Result = Accepted | Rejected

_NOT_ALLOWED: Final = Rejected(LifecycleCode.TRANSITION_NOT_ALLOWED)


def _accept(transition_id: str, snapshot: LifecycleSnapshot) -> Accepted:
    row = TRANSITIONS[transition_id]
    destination = row.destination
    if destination is Destination.SAME:
        if snapshot.state is None:  # pragma: no cover - ninguna fila «mismo» parte de «—»
            raise ValueError("una transición al mismo estado exige un estado de origen")
        return Accepted(row, snapshot.state, snapshot.previous_state)
    if isinstance(destination, Destination):  # pragma: no cover - PREVIOUS se resuelve aparte
        raise ValueError("el estado anterior lo resuelve T16")
    previous = snapshot.state if destination is S.SUSPENDED_BY_RECLASSIFICATION else None
    return Accepted(row, destination, previous)


def _classify(snapshot: LifecycleSnapshot, command: Classify) -> Result:
    state = snapshot.state
    confirmed = command.outcome is ClassificationOutcome.CONFIRMED
    if state is S.RECEIVED:
        return _accept("T03" if confirmed else "T04", snapshot)
    # Desde aquí toda clasificación es una reclasificación: exige motivo reforzado (§2.3).
    candidate: str | None
    if state in ACTIVE_LOOP_STATES:
        candidate = None if confirmed else "T15"
    elif state is S.SUSPENDED_BY_RECLASSIFICATION:
        candidate = "T16" if confirmed else None
    elif state is S.DISCARDED:
        candidate = "T17" if confirmed else "T18"
    elif state is S.CLOSED:
        candidate = "T19"
    elif state is S.UNCLOSABLE:
        candidate = "T20"
    else:
        candidate = None
    if candidate is None:
        return _NOT_ALLOWED
    if not command.reinforced_reason:
        return Rejected(LifecycleCode.REASON_REQUIRED)
    if candidate == "T16":
        return Accepted(TRANSITIONS["T16"], _resumed(snapshot, command.at), None)
    return _accept(candidate, snapshot)


def _resumed(snapshot: LifecycleSnapshot, at: datetime) -> FindingLifecycleState:
    """T16: el estado anterior, con el vencimiento recalculado por ``due_at``."""
    previous = snapshot.previous_state
    if previous is S.OPEN:
        return S.OPEN
    if previous in (S.ACTION_ASSIGNED, S.OVERDUE):
        if snapshot.due_at is not None and snapshot.due_at <= at:
            return S.OVERDUE
        return S.ACTION_ASSIGNED
    raise ValueError("un suspendido conserva open, action_assigned u overdue como anterior")


def _close(snapshot: LifecycleSnapshot, command: Close) -> Result:
    state = snapshot.state
    if state in (S.ACTION_ASSIGNED, S.OVERDUE):
        if command.evidence_entry_count < 1:
            return Rejected(LifecycleCode.CLOSURE_EVIDENCE_MISSING)
        if not command.signer_has_license:
            return Rejected(LifecycleCode.SIGNER_LICENSE_MISSING)
        return _accept("T09", snapshot)
    if state in (S.RECEIVED, S.IN_REVIEW, S.OPEN, S.DISCARDED):
        # Sin acción asignada no hay cierre (F02, F03).
        return Rejected(LifecycleCode.CLOSURE_REQUIRES_ASSIGNED_ACTION)
    return _NOT_ALLOWED


_POLICY_CODES: Final[Mapping[PolicyStatus, LifecycleCode]] = MappingProxyType(
    {
        PolicyStatus.UNAVAILABLE: LifecycleCode.UNCLOSABLE_POLICY_UNAVAILABLE,
        PolicyStatus.NOT_SIGNED: LifecycleCode.UNCLOSABLE_POLICY_NOT_SIGNED,
        PolicyStatus.REVOKED: LifecycleCode.UNCLOSABLE_POLICY_REVOKED,
    }
)


def _declare_unclosable(snapshot: LifecycleSnapshot, command: DeclareUnclosable) -> Result:
    if snapshot.state not in ACTIVE_LOOP_STATES:
        return _NOT_ALLOWED
    if not command.fields_complete:
        return Rejected(LifecycleCode.UNCLOSABLE_FIELDS_INCOMPLETE)
    if command.policy is not PolicyStatus.LOADED:
        return Rejected(_POLICY_CODES[command.policy])
    if not command.review_date_in_range:
        return Rejected(LifecycleCode.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE)
    if not command.signer_has_license:
        return Rejected(LifecycleCode.SIGNER_LICENSE_MISSING)
    return _accept("T10", snapshot)


def transition(snapshot: LifecycleSnapshot, command: Command) -> Result:
    """Aplica ``command`` al sujeto en ``snapshot``: el estado nuevo o el rechazo, sin efecto."""
    state = snapshot.state
    match command:
        case ReceiveFinding():
            return _accept("T01", snapshot) if state is None else _NOT_ALLOWED
        case ReceiveDetection():
            return _accept("T02", snapshot) if state is None else _NOT_ALLOWED
        case AttachLateSegment():
            return _NOT_ALLOWED if state is None else _accept("T21", snapshot)
        case _ if state is None:
            return _NOT_ALLOWED
        case Classify():
            return _classify(snapshot, command)
        case ResolveReview(outcome=outcome):
            if state is not S.IN_REVIEW:
                return _NOT_ALLOWED
            confirmed = outcome is ReviewResolutionOutcome.REVIEW_CONFIRMED
            return _accept("T05" if confirmed else "T06", snapshot)
        case AssignAction():
            return _accept("T07", snapshot) if state is S.OPEN else _NOT_ALLOWED
        case DeclareOverdue():
            return _accept("T08", snapshot) if state is S.ACTION_ASSIGNED else _NOT_ALLOWED
        case Close():
            return _close(snapshot, command)
        case DeclareUnclosable():
            return _declare_unclosable(snapshot, command)
        case ReviewUnclosable(outcome=outcome):
            if state is not S.UNCLOSABLE:
                return _NOT_ALLOWED
            renewed = outcome is UnclosableReviewOutcome.RENEWED
            return _accept("T11" if renewed else "T12", snapshot)
        case Reopen(reason_given=reason_given):
            if state not in (S.CLOSED, S.UNCLOSABLE):
                return _NOT_ALLOWED
            if not reason_given:
                return Rejected(LifecycleCode.REOPENING_REASON_REQUIRED)
            return _accept("T13" if state is S.CLOSED else "T14", snapshot)
    raise TypeError(f"comando desconocido: {type(command).__name__}")  # pragma: no cover
