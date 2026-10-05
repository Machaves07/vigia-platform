"""Sesión de walk-test persistida y su matriz derivada (DE §2.11 y §2.13; BL §2.2.2 y §3.3).

- **Matriz** (BR-GOB-36, 37): ``session_rows`` toma ``derive_matrix`` del catálogo vigente (una
  fila por estándar, combinación de condiciones de su predicado y postura) y le pone
  ``required_passes = passes_per_cell``. Nadie añade ni quita filas: la sesión guarda la matriz
  con la versión del catálogo de la que salió.
- **Pases por celda** (interfaces §3.3, que manda sobre el ``max(…, 3)`` de BL §2.2.2): menos de
  3 es ``passes_below_minimum``; más de ``MAX_PASSES_PER_CELL`` `[estimación propia]` o un valor
  que no es entero, ``invalid_request``.
- **Estados** (BL §3.3): ``in_progress`` y ``reopened`` están abiertas; ``expire_if_inactive``
  deja ``incomplete`` una sesión abierta a los **7 días exactos** sin actividad (``>=``), sin
  borrar nada; ``reopen`` solo sale de ``incomplete`` y conserva pases, pasos y matriz. Sobre una
  sesión ``incomplete`` toda operación es ``walk_test_incomplete``; sobre una ``closed``,
  ``conflict``.
- **Pases** (BR-GOB-38): de solo anexar; ``row_id`` tiene que estar en la matriz de la sesión.

Funciones puras: ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import dataclasses
import enum
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.catalog.domain.enums import (
    PassResult,
    Posture,
    WalkTestKind,
    WalkTestStatus,
)
from vigia_platform.catalog.domain.matrix import derive_matrix
from vigia_platform.catalog.domain.time_windows import utc_instant

__all__ = [
    "INACTIVITY_LIMIT",
    "MAX_PASSES_PER_CELL",
    "MIN_PASSES_PER_CELL",
    "OPEN_STATUSES",
    "PassCounts",
    "SessionRow",
    "WalkTestPass",
    "WalkTestRuleViolated",
    "WalkTestSession",
    "WalkTestViolation",
    "check_operable",
    "check_passes_per_cell",
    "expire_if_inactive",
    "open_session",
    "pass_counts",
    "predicate_condition",
    "reopen",
    "session_rows",
]

MIN_PASSES_PER_CELL: Final = 3
"""Mínimo de pases por celda (BR-GOB-37, `[estimación propia]` del diseño)."""
MAX_PASSES_PER_CELL: Final = 1000
"""Tope de pases por celda `[estimación propia]` de TASK-214: acota el cuerpo; el diseño solo fija
el mínimo."""
INACTIVITY_LIMIT: Final = timedelta(days=7)
"""Siete días sin actividad dejan la sesión ``incomplete`` (BR-GOB-50, `[estimación propia]`)."""
OPEN_STATUSES: Final = frozenset({WalkTestStatus.IN_PROGRESS, WalkTestStatus.REOPENED})
"""Estados abiertos: a lo sumo una sesión por zona en ellos (índice único parcial de gob_0017)."""


class WalkTestViolation(enum.StrEnum):
    """Por qué el dominio rechaza una operación de la sesión."""

    PASSES_BELOW_MINIMUM = "passes_below_minimum"
    """``passes_per_cell < 3``: ``catalog_passes_below_minimum``."""
    INCOMPLETE = "incomplete"
    """Sesión ``incomplete``: ``catalog_walk_test_incomplete``."""
    CLOSED = "closed"
    """Sesión ``closed``: ``conflict`` sin ``detail_code``."""
    NOT_INCOMPLETE = "not_incomplete"
    """Reabrir una sesión que no está ``incomplete``: ``conflict`` sin ``detail_code``."""
    ROW_NOT_IN_MATRIX = "row_not_in_matrix"
    """Pase con un ``row_id`` ajeno a la matriz: ``invalid_request``."""
    REQUEST_INVALID = "request_invalid"
    """Valor fuera de los límites de la forma: ``invalid_request``."""


class WalkTestRuleViolated(Exception):
    """El dominio rechaza la operación; ``violation`` dice por qué."""

    def __init__(self, violation: WalkTestViolation) -> None:
        super().__init__(violation.value)
        self.violation = violation


def predicate_condition(condition: tuple[str, str]) -> dict[str, Any]:
    """Una condición canónica en la forma de la gramática del predicado (contrato §3.2.1)."""
    key, value = condition
    if key == "presence":
        return {"presence": True}
    return {"signal_role": key, "value": value}


def _condition_of(value: Mapping[str, Any]) -> tuple[str, str]:
    if "presence" in value:
        return ("presence", "true")
    return (str(value["signal_role"]), str(value["value"]))


@dataclass(frozen=True, slots=True)
class SessionRow:
    """Una fila de ``matrix_rows`` de la sesión (DE §2.11)."""

    row_id: uuid.UUID
    standard_id: uuid.UUID
    standard_version: int
    conditions: tuple[tuple[str, str], ...]
    posture: Posture
    required_passes: int

    def to_json(self) -> dict[str, Any]:
        return {
            "row_id": str(self.row_id),
            "standard_id": str(self.standard_id),
            "standard_version": self.standard_version,
            "predicate_conditions": [predicate_condition(c) for c in self.conditions],
            "posture": Posture(self.posture).value,
            "required_passes": self.required_passes,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SessionRow:
        return cls(
            row_id=uuid.UUID(str(value["row_id"])),
            standard_id=uuid.UUID(str(value["standard_id"])),
            standard_version=int(value["standard_version"]),
            conditions=tuple(_condition_of(c) for c in value["predicate_conditions"]),
            posture=Posture(value["posture"]),
            required_passes=int(value["required_passes"]),
        )


def check_passes_per_cell(value: object) -> int:
    """``passes_per_cell`` válido; si no, ``WalkTestRuleViolated`` con su motivo."""
    if type(value) is not int:
        raise WalkTestRuleViolated(WalkTestViolation.REQUEST_INVALID)
    if value < MIN_PASSES_PER_CELL:
        raise WalkTestRuleViolated(WalkTestViolation.PASSES_BELOW_MINIMUM)
    if value > MAX_PASSES_PER_CELL:
        raise WalkTestRuleViolated(WalkTestViolation.REQUEST_INVALID)
    return value


def session_rows(catalog: Mapping[str, Any], passes_per_cell: int) -> tuple[SessionRow, ...]:
    """La matriz de la sesión: ``derive_matrix`` del catálogo con ``required_passes``."""
    passes = check_passes_per_cell(passes_per_cell)
    return tuple(
        SessionRow(
            row_id=row.row_id,
            standard_id=row.standard_id,
            standard_version=row.standard_version,
            conditions=row.conditions,
            posture=row.posture,
            required_passes=passes,
        )
        for row in derive_matrix(catalog)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkTestSession:
    """``WalkTestSession`` 🔒 (DE §2.11), con la última reapertura (gob_0023)."""

    session_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    catalog_version: int
    kind: WalkTestKind
    status: WalkTestStatus
    passes_per_cell: int
    matrix_rows: tuple[SessionRow, ...]
    started_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None
    commissioning_record_id: uuid.UUID | None = None
    reopened_at: datetime | None = None
    reopened_by: uuid.UUID | None = None
    reopen_reason_es: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def row(self, row_id: uuid.UUID) -> SessionRow | None:
        """La fila ``row_id`` de la matriz, o ``None`` si no es de esta sesión."""
        for row in self.matrix_rows:
            if row.row_id == row_id:
                return row
        return None


def open_session(
    *,
    session_id: uuid.UUID,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    node_id: uuid.UUID,
    catalog_version: int,
    catalog: Mapping[str, Any],
    passes_per_cell: int,
    at: datetime,
) -> WalkTestSession:
    """La sesión ``initial`` recién abierta, con su matriz derivada del catálogo."""
    moment = utc_instant(at)
    return WalkTestSession(
        session_id=session_id,
        organization_id=organization_id,
        plant_id=plant_id,
        zone_id=zone_id,
        node_id=node_id,
        catalog_version=catalog_version,
        kind=WalkTestKind.INITIAL,
        status=WalkTestStatus.IN_PROGRESS,
        passes_per_cell=passes_per_cell,
        matrix_rows=session_rows(catalog, passes_per_cell),
        started_at=moment,
        last_activity_at=moment,
    )


def expire_if_inactive(session: WalkTestSession, now: datetime) -> WalkTestSession:
    """``incomplete`` si la sesión está abierta y lleva 7 días o más sin actividad.

    Transición pura (BL §3.3): no borra pases, pasos ni matriz. La registra como tarea periódica
    TASK-227; las operaciones de la sesión la evalúan también antes de actuar.
    """
    if session.is_open and utc_instant(now) - session.last_activity_at >= INACTIVITY_LIMIT:
        return dataclasses.replace(session, status=WalkTestStatus.INCOMPLETE)
    return session


def check_operable(session: WalkTestSession) -> None:
    """Una sesión abierta; ``incomplete`` o ``closed`` rechazan la operación."""
    if session.status is WalkTestStatus.INCOMPLETE:
        raise WalkTestRuleViolated(WalkTestViolation.INCOMPLETE)
    if not session.is_open:
        raise WalkTestRuleViolated(WalkTestViolation.CLOSED)


def reopen(
    session: WalkTestSession, *, at: datetime, by: uuid.UUID, reason_es: str
) -> WalkTestSession:
    """``incomplete → reopened`` con motivo; conserva todo lo registrado (BL §3.3)."""
    if session.status is WalkTestStatus.CLOSED:
        raise WalkTestRuleViolated(WalkTestViolation.CLOSED)
    if session.status is not WalkTestStatus.INCOMPLETE:
        raise WalkTestRuleViolated(WalkTestViolation.NOT_INCOMPLETE)
    moment = max(session.last_activity_at, utc_instant(at))
    return dataclasses.replace(
        session,
        status=WalkTestStatus.REOPENED,
        last_activity_at=moment,
        reopened_at=moment,
        reopened_by=by,
        reopen_reason_es=reason_es,
    )


@dataclass(frozen=True, slots=True)
class WalkTestPass:
    """``WalkTestPass`` ⛓ (DE §2.13): corregir es registrar otro pase."""

    pass_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    session_id: uuid.UUID
    row_id: uuid.UUID
    result: PassResult
    evidence_ref: uuid.UUID | None
    recorded_by: uuid.UUID
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PassCounts:
    """Pases de una fila por resultado."""

    detected: int = 0
    missed: int = 0
    false_alarm: int = 0


def pass_counts(
    rows: Iterable[SessionRow], passes: Iterable[WalkTestPass]
) -> dict[uuid.UUID, PassCounts]:
    """El conteo de cada fila de la matriz (cero si no tiene pases), en el orden de la matriz."""
    counts = {row.row_id: [0, 0, 0] for row in rows}
    index = {PassResult.DETECTED: 0, PassResult.MISSED: 1, PassResult.FALSE_ALARM: 2}
    for recorded in passes:
        if recorded.row_id in counts:
            counts[recorded.row_id][index[PassResult(recorded.result)]] += 1
    return {row_id: PassCounts(*values) for row_id, values in counts.items()}
