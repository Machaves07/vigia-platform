"""Composición pura de la línea de tiempo de cobertura (LC-NUC-17; business-logic-model §7).

Sin entrada ni salida: recibe las entradas ya cargadas (``CoverageInputs``) y devuelve la línea de
tiempo de una zona en un periodo ``[a, b)`` o su estado en un instante. Así las propiedades
PR-NUC-25 a 29 y 51 corren sin base; ``ledger.application.coverage`` carga las entradas con
alcance y audita la consulta.

**Dos capas** (BR-NUC-69, domain-entities §3.8):

- ``node_report``, lo que el nodo informa, por sujeto (``subject``), con su reloj
  (``clock_basis = node``, BR-NUC-73). Cada apertura (``phase = opened``) inicia un tramo con su
  ``state`` y sus ``causes``; el cierre que la referencia (``opened_event_id``) lo termina en su
  ``ended_at``. Un **cierre huérfano** (su apertura no está entre las entradas; BR-CTR-11) es el
  tramo ``[started_at, ended_at)`` con el estado y las causas del cierre; si el cierre restaura
  (``state = observable``), la condición que cerró no se conoce y el tramo queda
  ``not_observable`` sin causas: nunca se presenta como observado lo que no consta (N-13, P6).
  Una **apertura sin cierre** sigue abierta hasta el siguiente tramo del mismo sujeto o hasta
  ``b``. Cada tramo termina donde empieza el siguiente del mismo sujeto (el nodo cierra el
  anterior al abrir uno nuevo, BR-BOR-52), así que dentro de un sujeto no hay solapes; con dos
  inicios en el mismo milisegundo manda el de ``event_id`` mayor.
- ``platform_communication``, lo que la plataforma supo, con su reloj (``clock_basis =
  platform``). Cada ``node_communication_state_changed`` declara ``state`` desde ``since``; un
  ``mute`` empieza en ``last_heartbeat_at`` y **no** en la declaración. Las declaraciones se
  aplican en orden de ``(since, record_id)`` y cada una manda desde su inicio efectivo: en ``t``
  vale la última declaración cuyo inicio efectivo es ``≤ t``. Antes de la primera, ``unknown``.

El **modo** de la zona en ``t`` es el ``resulting_mode`` del último ``gate_state_changed`` con
marca ``≤ t`` (orden ``(at, record_id)``); sin ninguno, ``no_capture``. El **nodo asignado** en
``t`` es el de la asignación ``[assigned_at, unassigned_at)`` que contiene ``t`` (con solapes,
la de ``(assigned_at, assignment_id)`` mayor).

**Compuesto** en ``t`` (BR-NUC-70), en este orden: sin nodo asignado o comunicación ``unknown`` →
``not_observable``/``never_reported``; modo distinto de ``productive`` →
``not_observable``/``zone_not_active``; comunicación ``mute`` → ``not_observable``/
``no_communication``; estado del nodo desconocido → ``not_observable``/``never_reported``; en otro
caso, el estado y las causas del tramo ``zone`` del nodo que contiene ``t``. Sin tramo ``zone``,
el nodo cuenta como ``observable`` solo si la zona tiene algún evento con ``started_at ≤ t``
(``first_report_at``); si no, su estado es desconocido. ``observable`` exige, pues, comunicación
``reachable``, modo ``productive`` y estado ``observable`` del nodo (BR-NUC-72).

**Partición exacta** (BR-NUC-71): la línea de tiempo son los tramos maximales de compuesto
constante (estado, causas y capa), ordenados, contiguos y sin solapes, que cubren ``[a, b)``;
``summary`` reparte cada milisegundo en exactamente un contador y suma ``b - a``. No existe
estado ni campo que signifique "despejada" o "segura" (P2).

**Tiempo.** Todo se calcula en milisegundos enteros desde la época (UTC): las marcas de entrada se
truncan al milisegundo; los extremos del periodo deben venir ya en milisegundos. El periodo
tiene como mucho 31 días (``PeriodTooLong``, PAT-NUC-REN-04): quien necesite más pide tramos y
los concatena (PR-NUC-51).

Hay dos caminos independientes: ``compose_timeline`` construye cada capa pintando funciones
escalonadas y las superpone; ``state_at`` evalúa cada capa directamente en el instante. PR-NUC-26
comprueba que coinciden.
"""

from __future__ import annotations

import bisect
import enum
import itertools
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from vigia_contracts.models.enumerations import (
    DegradationCause,
    ObservabilitySubjectKind,
)

__all__ = [
    "LABELS_ES",
    "MAX_PERIOD",
    "SUMMARY_LABELS_ES",
    "AssignmentInput",
    "ClockBasis",
    "CommunicationInput",
    "CommunicationState",
    "Composition",
    "CoverageInputInvalid",
    "CoverageInputs",
    "CoverageInterval",
    "CoverageLayer",
    "CoveragePeriod",
    "CoverageState",
    "CoverageStatus",
    "CoverageSummary",
    "EventPhase",
    "GateInput",
    "ObservabilityInput",
    "PeriodTooLong",
    "PlatformCause",
    "Subject",
    "ZoneMode",
    "check_period",
    "compose_timeline",
    "state_at",
]

MAX_PERIOD: Final = timedelta(days=31)
"""Tope de una consulta de línea de tiempo ``[objetivo propio]`` (PAT-NUC-REN-04)."""

_MS: Final = timedelta(milliseconds=1)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_PERIOD_MS: Final = MAX_PERIOD // _MS
_MAX_CAUSES: Final = 10
_DEGRADATION_CAUSES: Final = frozenset(cause.value for cause in DegradationCause)
_SUBJECT_KINDS: Final = frozenset(kind.value for kind in ObservabilitySubjectKind)


# --- Listas cerradas (domain-entities §1) ------------------------------------------------------


class CoverageState(enum.StrEnum):
    """``coverage_state``: la misma lista que ``observability_state`` del contrato."""

    OBSERVABLE = "observable"
    DEGRADED = "degraded"
    NOT_OBSERVABLE = "not_observable"


class CoverageLayer(enum.StrEnum):
    NODE_REPORT = "node_report"
    PLATFORM_COMMUNICATION = "platform_communication"


class PlatformCause(enum.StrEnum):
    """Causas de la plataforma; las del nodo son las ``degradation_cause`` del contrato."""

    NO_COMMUNICATION = "no_communication"
    NEVER_REPORTED = "never_reported"
    ZONE_NOT_ACTIVE = "zone_not_active"


class CommunicationState(enum.StrEnum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    MUTE = "mute"


class ZoneMode(enum.StrEnum):
    NO_CAPTURE = "no_capture"
    COMMISSIONING = "commissioning"
    PRODUCTIVE = "productive"


class ClockBasis(enum.StrEnum):
    PLATFORM = "platform"
    NODE = "node"


class EventPhase(enum.StrEnum):
    OPENED = "opened"
    CLOSED = "closed"


LABELS_ES: Final[Mapping[str, str]] = {
    CoverageState.OBSERVABLE: "observable",
    CoverageState.DEGRADED: "degradada",
    CoverageState.NOT_OBSERVABLE: "no observable: no se observó",
    PlatformCause.NO_COMMUNICATION: "sin comunicación con el nodo: no se observó",
    PlatformCause.NEVER_REPORTED: "nunca reportó: no se observó",
    PlatformCause.ZONE_NOT_ACTIVE: "zona no activa: no se observó",
    CoverageLayer.NODE_REPORT: "lo que el nodo informa",
    CoverageLayer.PLATFORM_COMMUNICATION: "lo que la plataforma supo",
}
"""Etiquetas de los valores de cobertura (domain-entities §1, BR-NUC-72): dicen "no se
observó", nunca "no ocurrió", "despejada" ni "segura", y no atribuyen intención (RNF-OBS-03)."""

SUMMARY_LABELS_ES: Final[Mapping[str, str]] = {
    "observable_ms": "tiempo observable",
    "degraded_ms": "tiempo degradado",
    "not_observable_ms": "tiempo no observable según el nodo: no se observó",
    "no_communication_ms": "tiempo sin comunicación con el nodo: no se observó",
    "never_reported_ms": "tiempo sin reporte del nodo: no se observó",
    "zone_not_active_ms": "tiempo con la zona no activa: no se observó",
}
"""Etiquetas de los contadores de ``CoverageSummary``, con la misma regla que ``LABELS_ES``."""


# --- Errores ----------------------------------------------------------------------------------


class CoverageInputInvalid(ValueError):
    """Entrada o periodo inválidos: no se compone nada."""

    code: Final = "query_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"consulta de cobertura inválida: {detail}")


class PeriodTooLong(ValueError):
    """El periodo supera el tope de 31 días (``period_too_long``, PAT-NUC-REN-04)."""

    code: Final = "period_too_long"

    def __init__(self) -> None:
        super().__init__(
            f"el periodo supera el tope de {MAX_PERIOD.days} días: pídelo por tramos (PR-NUC-51)"
        )


# --- Entradas ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    """Sujeto de un evento de observabilidad (``ObservabilitySubject`` del contrato)."""

    kind: str
    camera_id: uuid.UUID | None = None
    signal_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ObservabilityInput:
    """Un ``observability_event_received``: el evento del nodo y el registro que lo guarda."""

    record_id: uuid.UUID
    event_id: uuid.UUID
    subject: Subject
    phase: EventPhase
    state: CoverageState
    causes: tuple[str, ...]
    started_at: datetime
    ended_at: datetime | None = None
    opened_event_id: uuid.UUID | None = None
    clock_offset_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CommunicationInput:
    """Un ``node_communication_state_changed`` (``{node_id, state, since, last_heartbeat_at}``)."""

    record_id: uuid.UUID
    node_id: uuid.UUID
    state: CommunicationState
    since: datetime
    last_heartbeat_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GateInput:
    """Un ``gate_state_changed``: el modo resultante de la zona desde ``at``."""

    record_id: uuid.UUID
    at: datetime
    resulting_mode: ZoneMode


@dataclass(frozen=True, slots=True)
class AssignmentInput:
    """Una ``ZoneNodeAssignment`` de la zona: ``[assigned_at, unassigned_at)``."""

    assignment_id: uuid.UUID
    node_id: uuid.UUID
    assigned_at: datetime
    unassigned_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CoverageInputs:
    """Todo lo que la composición necesita de una zona.

    ``first_report_at`` es el ``started_at`` más antiguo de los eventos de la zona si la carga no
    trae todos (la composición toma el mínimo con los que sí trae).
    """

    observability: tuple[ObservabilityInput, ...] = ()
    communication: tuple[CommunicationInput, ...] = ()
    gates: tuple[GateInput, ...] = ()
    assignments: tuple[AssignmentInput, ...] = ()
    first_report_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CoveragePeriod:
    """``[start, end)`` (``period.from`` y ``period.to`` del diseño), en milisegundos exactos."""

    start: datetime
    end: datetime


# --- Salidas ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageInterval:
    """``CoverageInterval`` (domain-entities §3.8).

    ``clock_offset_ms`` es la desviación de reloj que el nodo declaró en el evento (solo en los
    tramos de ``node_intervals``, BR-NUC-73).
    """

    starts_at: datetime
    ends_at: datetime
    layer: CoverageLayer
    state: CoverageState
    causes: tuple[str, ...]
    subject: Subject | None
    source_record_ids: tuple[uuid.UUID, ...]
    clock_basis: ClockBasis
    clock_offset_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        return (self.ends_at - self.starts_at) // _MS


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Duración por estado y causa principal; cada milisegundo cuenta una sola vez.

    ``not_observable_ms`` es lo que el nodo informó como no observable; las tres causas de la
    plataforma tienen su propio contador.
    """

    observable_ms: int = 0
    degraded_ms: int = 0
    not_observable_ms: int = 0
    no_communication_ms: int = 0
    never_reported_ms: int = 0
    zone_not_active_ms: int = 0

    @property
    def total_ms(self) -> int:
        return (
            self.observable_ms
            + self.degraded_ms
            + self.not_observable_ms
            + self.no_communication_ms
            + self.never_reported_ms
            + self.zone_not_active_ms
        )


@dataclass(frozen=True, slots=True)
class Composition:
    """La línea de tiempo compuesta, la capa del nodo sin componer y el resumen."""

    period: CoveragePeriod
    intervals: tuple[CoverageInterval, ...]
    node_intervals: tuple[CoverageInterval, ...]
    summary: CoverageSummary


@dataclass(frozen=True, slots=True)
class CoverageStatus:
    """``estado_en``: el compuesto en un instante (BR-NUC-74)."""

    instant: datetime
    state: CoverageState
    causes: tuple[str, ...]
    layer: CoverageLayer
    clock_basis: ClockBasis
    source_record_ids: tuple[uuid.UUID, ...]


# --- Validación y normalización a milisegundos ---------------------------------------------------


def _ms(moment: object, name: str) -> int:
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise CoverageInputInvalid(f"{name} debe ser una marca con zona horaria")
    try:
        return (moment - _EPOCH) // _MS
    except OverflowError:  # pragma: no cover - datetime acota el año a 1..9999
        raise CoverageInputInvalid(f"{name} fuera de rango") from None


def _at(ms: int) -> datetime:
    return _EPOCH + ms * _MS


def _uuid(value: object, name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise CoverageInputInvalid(f"{name} debe ser uuid.UUID")
    return value


def _member[E: enum.StrEnum](value: object, kind: type[E], name: str) -> E:
    if type(value) is not str and not isinstance(value, kind):
        raise CoverageInputInvalid(f"{name} debe ser un valor de {kind.__name__}")
    try:
        return kind(value)
    except ValueError:
        raise CoverageInputInvalid(f"{name} no es un valor de {kind.__name__}") from None


def check_period(period: object) -> tuple[int, int]:
    """``(a, b)`` en milisegundos; ``CoverageInputInvalid`` o ``PeriodTooLong`` si no vale."""
    if not isinstance(period, CoveragePeriod):
        raise CoverageInputInvalid("period debe ser CoveragePeriod")
    for name, moment in (("period.start", period.start), ("period.end", period.end)):
        _ms(moment, name)
        if moment.microsecond % 1000:
            raise CoverageInputInvalid(f"{name} debe tener precisión de milisegundos")
    a, b = _ms(period.start, "period.start"), _ms(period.end, "period.end")
    if a >= b:
        raise CoverageInputInvalid("el inicio del periodo debe ser anterior al fin")
    if b - a > _MAX_PERIOD_MS:
        raise PeriodTooLong()
    return a, b


@dataclass(frozen=True, slots=True)
class _Obs:
    record_id: uuid.UUID
    event_id: uuid.UUID
    subject: Subject
    phase: EventPhase
    state: CoverageState
    causes: tuple[str, ...]
    start: int
    end: int | None
    opened_event_id: uuid.UUID | None
    clock_offset_ms: int | None


@dataclass(frozen=True, slots=True)
class _Comm:
    record_id: uuid.UUID
    node_id: uuid.UUID
    state: CommunicationState
    since: int
    effective: int

    @property
    def order(self) -> tuple[int, int]:
        return (self.since, self.record_id.int)


@dataclass(frozen=True, slots=True)
class _Gate:
    record_id: uuid.UUID
    at: int
    mode: ZoneMode

    @property
    def order(self) -> tuple[int, int]:
        return (self.at, self.record_id.int)


@dataclass(frozen=True, slots=True)
class _Assignment:
    assignment_id: uuid.UUID
    node_id: uuid.UUID
    start: int
    end: int | None

    @property
    def order(self) -> tuple[int, int]:
        return (self.start, self.assignment_id.int)


def _subject(value: object) -> Subject:
    if not isinstance(value, Subject):
        raise CoverageInputInvalid("subject debe ser Subject")
    if type(value.kind) is not str or value.kind not in _SUBJECT_KINDS:
        raise CoverageInputInvalid("subject.kind no es un valor de observability_subject_kind")
    if (value.kind == ObservabilitySubjectKind.CAMERA.value) != (value.camera_id is not None):
        raise CoverageInputInvalid("subject.camera_id va si y solo si kind = camera")
    if value.signal_id is not None and value.kind != ObservabilitySubjectKind.SIGNAL_READER.value:
        raise CoverageInputInvalid("subject.signal_id solo va con kind = signal_reader")
    for name, identifier in (("camera_id", value.camera_id), ("signal_id", value.signal_id)):
        if identifier is not None:
            _uuid(identifier, f"subject.{name}")
    return value


def _causes(value: object, state: CoverageState) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise CoverageInputInvalid("causes debe ser una tupla")
    if any(type(cause) is not str or cause not in _DEGRADATION_CAUSES for cause in value):
        raise CoverageInputInvalid("causes solo admite valores de degradation_cause")
    if len(set(value)) != len(value):
        raise CoverageInputInvalid("causes no admite repetidos")
    if state is CoverageState.OBSERVABLE and value:
        raise CoverageInputInvalid("un evento observable no lleva causas")
    if state is not CoverageState.OBSERVABLE and not 1 <= len(value) <= _MAX_CAUSES:
        raise CoverageInputInvalid(f"un evento no observable lleva de 1 a {_MAX_CAUSES} causas")
    return value


def _observability(event: object) -> _Obs:
    if not isinstance(event, ObservabilityInput):
        raise CoverageInputInvalid("observability solo admite ObservabilityInput")
    phase = _member(event.phase, EventPhase, "phase")
    state = _member(event.state, CoverageState, "state")
    start = _ms(event.started_at, "started_at")
    end: int | None = None
    opened: uuid.UUID | None = None
    if phase is EventPhase.CLOSED:
        if event.ended_at is None or event.opened_event_id is None:
            raise CoverageInputInvalid("un cierre exige ended_at y opened_event_id (BR-CTR-11)")
        end = _ms(event.ended_at, "ended_at")
        if event.ended_at < event.started_at:
            raise CoverageInputInvalid("ended_at debe ser mayor o igual que started_at")
        opened = _uuid(event.opened_event_id, "opened_event_id")
    elif event.ended_at is not None or event.opened_event_id is not None:
        raise CoverageInputInvalid("una apertura no lleva ended_at ni opened_event_id (BR-CTR-11)")
    offset = event.clock_offset_ms
    if offset is not None and type(offset) is not int:
        raise CoverageInputInvalid("clock_offset_ms debe ser un entero")
    return _Obs(
        record_id=_uuid(event.record_id, "record_id"),
        event_id=_uuid(event.event_id, "event_id"),
        subject=_subject(event.subject),
        phase=phase,
        state=state,
        causes=_causes(event.causes, state),
        start=start,
        end=end,
        opened_event_id=opened,
        clock_offset_ms=offset,
    )


def _communication(change: object) -> _Comm:
    if not isinstance(change, CommunicationInput):
        raise CoverageInputInvalid("communication solo admite CommunicationInput")
    state = _member(change.state, CommunicationState, "communication.state")
    since = _ms(change.since, "since")
    effective = since
    if change.last_heartbeat_at is not None:
        heartbeat = _ms(change.last_heartbeat_at, "last_heartbeat_at")
        if state is CommunicationState.MUTE:
            # El silencio empieza en el último latido aceptado, no en la declaración (BR-NUC-69).
            effective = heartbeat
    return _Comm(
        record_id=_uuid(change.record_id, "communication.record_id"),
        node_id=_uuid(change.node_id, "communication.node_id"),
        state=state,
        since=since,
        effective=effective,
    )


def _gate(change: object) -> _Gate:
    if not isinstance(change, GateInput):
        raise CoverageInputInvalid("gates solo admite GateInput")
    return _Gate(
        record_id=_uuid(change.record_id, "gate.record_id"),
        at=_ms(change.at, "gate.at"),
        mode=_member(change.resulting_mode, ZoneMode, "resulting_mode"),
    )


def _assignment(row: object) -> _Assignment:
    if not isinstance(row, AssignmentInput):
        raise CoverageInputInvalid("assignments solo admite AssignmentInput")
    start = _ms(row.assigned_at, "assigned_at")
    end: int | None = None
    if row.unassigned_at is not None:
        end = _ms(row.unassigned_at, "unassigned_at")
        if row.unassigned_at <= row.assigned_at:
            raise CoverageInputInvalid("unassigned_at debe ser posterior a assigned_at")
    return _Assignment(
        assignment_id=_uuid(row.assignment_id, "assignment_id"),
        node_id=_uuid(row.node_id, "assignment.node_id"),
        start=start,
        end=end,
    )


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise CoverageInputInvalid(f"{name} debe ser una tupla")
    return value


@dataclass(frozen=True, slots=True)
class _Normalized:
    observability: tuple[_Obs, ...]
    communication: tuple[_Comm, ...]
    gates: tuple[_Gate, ...]
    assignments: tuple[_Assignment, ...]
    first_report: int | None


def _normalize(inputs: object) -> _Normalized:
    if not isinstance(inputs, CoverageInputs):
        raise CoverageInputInvalid("inputs debe ser CoverageInputs")
    observability = tuple(
        _observability(e) for e in _sequence(inputs.observability, "observability")
    )
    event_ids = [event.event_id for event in observability]
    if len(set(event_ids)) != len(event_ids):
        raise CoverageInputInvalid("event_id repetido entre los eventos de observabilidad")
    starts = [event.start for event in observability]
    if inputs.first_report_at is not None:
        starts.append(_ms(inputs.first_report_at, "first_report_at"))
    return _Normalized(
        observability=observability,
        communication=tuple(
            _communication(c) for c in _sequence(inputs.communication, "communication")
        ),
        gates=tuple(_gate(g) for g in _sequence(inputs.gates, "gates")),
        assignments=tuple(_assignment(a) for a in _sequence(inputs.assignments, "assignments")),
        first_report=min(starts, default=None),
    )


# --- Capa del nodo: tramos por sujeto --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Starter:
    """Un inicio de tramo: una apertura o un cierre huérfano."""

    start: int
    own_end: int | None
    """Fin propio: el ``ended_at`` del cierre (el más temprano si hay varios) o ``None``."""
    event_id: uuid.UUID
    subject: Subject
    state: CoverageState
    causes: tuple[str, ...]
    record_ids: tuple[uuid.UUID, ...]
    clock_offset_ms: int | None

    @property
    def order(self) -> tuple[int, int]:
        return (self.start, self.event_id.int)


def _starters(events: Iterable[_Obs]) -> dict[Subject, list[_Starter]]:
    """Inicios de tramo por sujeto, ordenados por ``(start, event_id)``."""
    events = tuple(events)
    opens = {e.event_id: e for e in events if e.phase is EventPhase.OPENED}
    closes: dict[uuid.UUID, _Obs] = {}
    orphans: list[_Obs] = []
    for event in sorted(events, key=_close_order):
        if event.phase is not EventPhase.CLOSED or event.opened_event_id is None:
            continue
        if event.opened_event_id not in opens:
            orphans.append(event)
        else:
            # Con varios cierres de la misma apertura manda el más temprano.
            closes.setdefault(event.opened_event_id, event)
    by_subject: dict[Subject, list[_Starter]] = {}
    for opened in opens.values():
        close = closes.get(opened.event_id)
        by_subject.setdefault(opened.subject, []).append(
            _Starter(
                start=opened.start,
                own_end=None if close is None else close.end,
                event_id=opened.event_id,
                subject=opened.subject,
                state=opened.state,
                causes=opened.causes,
                record_ids=_ids(
                    (opened.record_id,) if close is None else (opened.record_id, close.record_id)
                ),
                clock_offset_ms=opened.clock_offset_ms,
            )
        )
    for orphan in orphans:
        restored = orphan.state is CoverageState.OBSERVABLE
        by_subject.setdefault(orphan.subject, []).append(
            _Starter(
                start=orphan.start,
                own_end=orphan.end,
                event_id=orphan.event_id,
                subject=orphan.subject,
                state=CoverageState.NOT_OBSERVABLE if restored else orphan.state,
                causes=orphan.causes,
                record_ids=(orphan.record_id,),
                clock_offset_ms=orphan.clock_offset_ms,
            )
        )
    for starters in by_subject.values():
        starters.sort(key=lambda s: s.order)
    return by_subject


def _close_order(event: _Obs) -> tuple[int, int]:
    return (event.end if event.end is not None else event.start, event.event_id.int)


@dataclass(frozen=True, slots=True)
class _Extent:
    start: int
    end: int | None
    starter: _Starter


def _extents(starters: Sequence[_Starter]) -> list[_Extent]:
    """Tramos de un sujeto: cada inicio dura hasta su fin propio o hasta el inicio siguiente."""
    extents: list[_Extent] = []
    for index, starter in enumerate(starters):
        ends = [starter.own_end] if starter.own_end is not None else []
        if index + 1 < len(starters):
            ends.append(starters[index + 1].start)
        end = min(ends) if ends else None
        if end is None or end > starter.start:
            extents.append(_Extent(starter.start, end, starter))
    return extents


# --- Funciones escalonadas (la construcción por pintado) --------------------------------------


class _Steps[V]:
    """Función escalonada ``int → V | None``: ``points[i]`` vale desde su inicio hasta el
    siguiente. Pintar sobrescribe un rango; lo último pintado manda."""

    __slots__ = ("points",)

    def __init__(self) -> None:
        self.points: list[tuple[int, V | None]] = []

    def value_at(self, t: int) -> V | None:
        index = bisect.bisect_right(self.points, t, key=lambda point: point[0]) - 1
        return None if index < 0 else self.points[index][1]

    def paint_from(self, start: int, value: V) -> None:
        """Pinta ``[start, ∞)``."""
        while self.points and self.points[-1][0] >= start:
            self.points.pop()
        self.points.append((start, value))

    def paint(self, start: int, end: int, value: V) -> None:
        """Pinta ``[start, end)`` y deja lo que había a partir de ``end``."""
        if end <= start:
            return
        resumed = self.value_at(end)
        left = [point for point in self.points if point[0] < start]
        right = [point for point in self.points if point[0] > end]
        self.points = [*left, (start, value), (end, resumed), *right]

    def breakpoints(self) -> list[int]:
        return [start for start, _ in self.points]


@dataclass(frozen=True, slots=True)
class _Composite:
    state: CoverageState
    causes: tuple[str, ...]
    layer: CoverageLayer
    clock_basis: ClockBasis
    record_ids: tuple[uuid.UUID, ...]

    @property
    def key(self) -> tuple[CoverageState, tuple[str, ...], CoverageLayer]:
        return (self.state, self.causes, self.layer)


def _platform(cause: PlatformCause, record_ids: Iterable[uuid.UUID] = ()) -> _Composite:
    return _Composite(
        CoverageState.NOT_OBSERVABLE,
        (cause.value,),
        CoverageLayer.PLATFORM_COMMUNICATION,
        ClockBasis.PLATFORM,
        _ids(record_ids),
    )


_OBSERVABLE_GAP: Final = _Composite(
    CoverageState.OBSERVABLE, (), CoverageLayer.NODE_REPORT, ClockBasis.NODE, ()
)
"""Sin tramo ``zone`` abierto pero con reportes previos: el nodo no informa degradación."""


def _compose(
    node_id: uuid.UUID | None,
    communication: _Comm | None,
    gate: _Gate | None,
    zone: _Starter | None,
    reported: bool,
) -> _Composite:
    """``compuesto(t)`` (business-logic-model §7, BR-NUC-70)."""
    if node_id is None:
        return _platform(PlatformCause.NEVER_REPORTED)
    if communication is None or communication.state is CommunicationState.UNKNOWN:
        return _platform(
            PlatformCause.NEVER_REPORTED,
            () if communication is None else (communication.record_id,),
        )
    if gate is None or gate.mode is not ZoneMode.PRODUCTIVE:
        return _platform(PlatformCause.ZONE_NOT_ACTIVE, () if gate is None else (gate.record_id,))
    if communication.state is CommunicationState.MUTE:
        return _platform(PlatformCause.NO_COMMUNICATION, (communication.record_id,))
    if zone is not None:
        return _Composite(
            zone.state, zone.causes, CoverageLayer.NODE_REPORT, ClockBasis.NODE, zone.record_ids
        )
    if reported:
        return _OBSERVABLE_GAP
    return _platform(PlatformCause.NEVER_REPORTED)


def _ids(values: Iterable[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    return tuple(sorted(set(values)))


def _zone_subject(subject: Subject) -> bool:
    return subject.kind == ObservabilitySubjectKind.ZONE.value


# --- Línea de tiempo ----------------------------------------------------------------------------


def compose_timeline(inputs: CoverageInputs, period: CoveragePeriod) -> Composition:
    """La línea de tiempo de ``[a, b)``: partición exacta, capa del nodo y resumen."""
    a, b = check_period(period)
    data = _normalize(inputs)

    assignments: _Steps[_Assignment] = _Steps()
    for row in sorted(data.assignments, key=lambda r: r.order):
        if row.end is None:
            assignments.paint_from(row.start, row)
        else:
            assignments.paint(row.start, row.end, row)

    communication: dict[uuid.UUID, _Steps[_Comm]] = {}
    for change in sorted(data.communication, key=lambda c: c.order):
        communication.setdefault(change.node_id, _Steps()).paint_from(change.effective, change)

    gates: _Steps[_Gate] = _Steps()
    for gate in sorted(data.gates, key=lambda g: g.order):
        gates.paint_from(gate.at, gate)

    by_subject = _starters(data.observability)
    zone: _Steps[_Starter] = _Steps()
    node_intervals: list[CoverageInterval] = []
    for subject, starters in sorted(by_subject.items(), key=lambda item: _subject_key(item[0])):
        for extent in _extents(starters):
            if _zone_subject(subject):
                if extent.end is None:
                    zone.paint_from(extent.start, extent.starter)
                else:
                    zone.paint(extent.start, extent.end, extent.starter)
            clipped = _clip(extent.start, extent.end, a, b)
            if clipped is not None:
                node_intervals.append(_node_interval(extent.starter, *clipped))

    points = {a, b}
    points.update(assignments.breakpoints())
    points.update(gates.breakpoints())
    points.update(zone.breakpoints())
    for steps in communication.values():
        points.update(steps.breakpoints())
    if data.first_report is not None:
        points.add(data.first_report)
    cuts = sorted(point for point in points if a <= point <= b)

    intervals: list[tuple[int, int, _Composite]] = []
    for start, end in itertools.pairwise(cuts):
        assignment = assignments.value_at(start)
        node_id = None if assignment is None else assignment.node_id
        node_steps = None if node_id is None else communication.get(node_id)
        composite = _compose(
            node_id,
            None if node_steps is None else node_steps.value_at(start),
            gates.value_at(start),
            zone.value_at(start),
            data.first_report is not None and data.first_report <= start,
        )
        if intervals and intervals[-1][2].key == composite.key:
            previous = intervals[-1][2]
            merged = _Composite(
                composite.state,
                composite.causes,
                composite.layer,
                composite.clock_basis,
                _ids((*previous.record_ids, *composite.record_ids)),
            )
            intervals[-1] = (intervals[-1][0], end, merged)
        else:
            intervals.append((start, end, composite))

    timeline = tuple(
        CoverageInterval(
            starts_at=_at(start),
            ends_at=_at(end),
            layer=composite.layer,
            state=composite.state,
            causes=composite.causes,
            subject=None,
            source_record_ids=composite.record_ids,
            clock_basis=composite.clock_basis,
        )
        for start, end, composite in intervals
    )
    node_intervals.sort(key=_node_interval_key)
    return Composition(
        period=CoveragePeriod(_at(a), _at(b)),
        intervals=timeline,
        node_intervals=tuple(node_intervals),
        summary=_summary(timeline),
    )


def _subject_key(subject: Subject) -> tuple[str, int, int]:
    return (
        subject.kind,
        -1 if subject.camera_id is None else subject.camera_id.int,
        -1 if subject.signal_id is None else subject.signal_id.int,
    )


def _node_interval_key(interval: CoverageInterval) -> tuple[datetime, tuple[str, int, int]]:
    subject = interval.subject or Subject(kind="")
    return (interval.starts_at, _subject_key(subject))


def _clip(start: int, end: int | None, a: int, b: int) -> tuple[int, int] | None:
    lower = max(start, a)
    upper = b if end is None else min(end, b)
    return (lower, upper) if upper > lower else None


def _node_interval(starter: _Starter, start: int, end: int) -> CoverageInterval:
    return CoverageInterval(
        starts_at=_at(start),
        ends_at=_at(end),
        layer=CoverageLayer.NODE_REPORT,
        state=starter.state,
        causes=starter.causes,
        subject=starter.subject,
        source_record_ids=starter.record_ids,
        clock_basis=ClockBasis.NODE,
        clock_offset_ms=starter.clock_offset_ms,
    )


def _summary(intervals: Iterable[CoverageInterval]) -> CoverageSummary:
    totals = dict.fromkeys(SUMMARY_LABELS_ES, 0)
    for interval in intervals:
        if interval.layer is CoverageLayer.PLATFORM_COMMUNICATION:
            bucket = f"{interval.causes[0]}_ms"
        else:
            bucket = f"{interval.state.value}_ms"
        totals[bucket] += interval.duration_ms
    return CoverageSummary(**totals)


# --- Estado en un instante (evaluación directa, sin construir la línea) ------------------------


def state_at(inputs: CoverageInputs, instant: datetime) -> CoverageStatus:
    """``estado_en``: el compuesto en ``instant`` (truncado al milisegundo), capa por capa."""
    t = _ms(instant, "instant")
    data = _normalize(inputs)

    containing = [
        row for row in data.assignments if row.start <= t and (row.end is None or t < row.end)
    ]
    assignment = max(containing, key=lambda r: r.order, default=None)
    node_id = None if assignment is None else assignment.node_id

    communication = max(
        (c for c in data.communication if c.node_id == node_id and c.effective <= t),
        key=lambda c: c.order,
        default=None,
    )
    gate = max((g for g in data.gates if g.at <= t), key=lambda g: g.order, default=None)

    zone_starters = [
        starter
        for subject, starters in _starters(data.observability).items()
        if _zone_subject(subject)
        for starter in starters
        if starter.start <= t
    ]
    latest = max(zone_starters, key=lambda s: s.order, default=None)
    zone = latest if latest is not None and (latest.own_end is None or t < latest.own_end) else None

    composite = _compose(
        node_id,
        communication,
        gate,
        zone,
        data.first_report is not None and data.first_report <= t,
    )
    return CoverageStatus(
        instant=_at(t),
        state=composite.state,
        causes=composite.causes,
        layer=composite.layer,
        clock_basis=composite.clock_basis,
        source_record_ids=composite.record_ids,
    )
