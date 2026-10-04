"""Compuertas de una zona: modo resultante, proyección, intervalos y ``GateState`` (LC-GOB-03).

Dos compuertas que no se funden (BL §3.1, BR-GOB-19):

| Montaje | Uso | ``resulting_mode`` |
|---|---|---|
| no ``approved`` | cualquiera | ``no_capture`` |
| ``approved`` | no ``approved`` | ``commissioning`` |
| ``approved`` | ``approved`` | ``productive`` |

``resulting_mode`` es **función total y pura** de las dos (``revoked`` cuenta como no aprobada):
no depende de nada más, así que revocar nunca amplía el modo. ``ZoneGateState`` es la proyección
mutable (DE §2.4) y ``GateInterval`` un intervalo de ``GateStateHistory`` (DE §2.5): la verdad
histórica, de la que ``state_at`` responde siempre (BR-GOB-20).

Transiciones admitidas (BL §3.1): ``pending → approved`` y ``revoked → approved`` con un acta o un
acuerdo; ``approved → approved`` con un acta o un acuerdo nuevos (intervalo contiguo con el
``record_id`` nuevo, BR-GOB-32); ``approved → revoked`` con ``reason_es`` de 10 a 500. Nada vuelve
a ``pending`` ni existe transición por tiempo.

``gate_state_payload`` compone la carga del ``SignedEnvelope<GateState>`` del contrato (§3.3) con
``valid_until = issued_at + 7 días`` (BR-CTR-43); ``decided_by`` es de la plataforma y no viaja.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.time_windows import HalfOpenInterval, utc_instant
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "ENVELOPE_VALIDITY",
    "MAX_REASON_CHARS",
    "MIN_REASON_CHARS",
    "GateDecision",
    "GateInterval",
    "GateRuleViolated",
    "GateViolation",
    "ZoneGateState",
    "gate_state_payload",
    "mode_rank",
    "plan_transition",
    "resulting_mode",
]

ENVELOPE_VALIDITY: Final = timedelta(days=7)
"""``valid_until - issued_at`` del sobre (BR-CTR-43; ``zone_gate_state_validity`` en la base)."""
MIN_REASON_CHARS: Final = 10
MAX_REASON_CHARS: Final = 500
"""``reason_es`` de una revocación (DE §2.5 `[estimación propia]`)."""

_MODE_RANK: Final[Mapping[ZoneMode, int]] = {
    ZoneMode.NO_CAPTURE: 0,
    ZoneMode.COMMISSIONING: 1,
    ZoneMode.PRODUCTIVE: 2,
}


def resulting_mode(mounting: GateStatus, usage: GateStatus) -> ZoneMode:
    """El modo de la zona: función total de las dos compuertas (BR-GOB-19, BR-CTR-40)."""
    mounting, usage = GateStatus(mounting), GateStatus(usage)
    if mounting is not GateStatus.APPROVED:
        return ZoneMode.NO_CAPTURE
    if usage is not GateStatus.APPROVED:
        return ZoneMode.COMMISSIONING
    return ZoneMode.PRODUCTIVE


def mode_rank(mode: ZoneMode) -> int:
    """Orden de amplitud: ``no_capture`` < ``commissioning`` < ``productive``."""
    return _MODE_RANK[ZoneMode(mode)]


class GateViolation(enum.StrEnum):
    """Por qué el dominio rechaza una transición."""

    NOT_APPROVED = "not_approved"
    """Revocar una compuerta que no está ``approved`` (``conflict`` sin ``detail_code``)."""
    REQUEST_INVALID = "request_invalid"
    """Estado de destino, motivo o respaldo incoherentes con la transición."""


class GateRuleViolated(Exception):
    def __init__(self, violation: GateViolation) -> None:
        super().__init__(f"transición de compuerta rechazada: {violation.value}")
        self.violation = violation


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Una compuerta en la proyección: ``{status, decided_at?, record_id?, decided_by?}``.

    ``record_id`` es el acta (montaje) o el ``agreement_id`` (uso); en ``revoked`` sigue siendo el
    de la aprobación que se revocó.
    """

    status: GateStatus
    decided_at: datetime | None = None
    record_id: uuid.UUID | None = None
    decided_by: uuid.UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", GateStatus(self.status))
        pending = self.status is GateStatus.PENDING
        fields = (self.decided_at, self.record_id, self.decided_by)
        if pending and any(value is not None for value in fields):
            raise ValueError("una compuerta pendiente no tiene decisión")
        if not pending and any(value is None for value in fields):
            raise ValueError("una compuerta decidida lleva instante, respaldo y autor")

    @classmethod
    def pending(cls) -> GateDecision:
        return cls(GateStatus.PENDING)

    def as_json(self, gate: GateKind, *, with_author: bool) -> dict[str, Any]:
        """La forma del contrato (``record_id`` o ``agreement_id``), más ``decided_by`` si se
        pide (proyección de la plataforma; el sobre no lo lleva)."""
        value: dict[str, Any] = {"status": self.status.value}
        if self.decided_at is not None:
            value["decided_at"] = format_timestamp(self.decided_at)
        if self.record_id is not None:
            key = "record_id" if GateKind(gate) is GateKind.MOUNTING else "agreement_id"
            value[key] = str(self.record_id)
        if with_author and self.decided_by is not None:
            value["decided_by"] = str(self.decided_by)
        return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneGateState:
    """§2.4 ``ZoneGateState`` 🔒: la proyección de una zona, con su sobre ya emitido."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    mounting: GateDecision
    usage: GateDecision
    issued_at: datetime | None = None
    """``None`` solo en la zona sin ninguna transición (sin fila ni sobre)."""
    envelope: Mapping[str, Any] | None = None

    @property
    def resulting_mode(self) -> ZoneMode:
        return resulting_mode(self.mounting.status, self.usage.status)

    @property
    def valid_until(self) -> datetime | None:
        return None if self.issued_at is None else self.issued_at + ENVELOPE_VALIDITY

    @classmethod
    def initial(
        cls, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID
    ) -> ZoneGateState:
        """La zona sin intervalos: ``pending`` en las dos (decisión del redactor de TASK-211)."""
        return cls(
            organization_id=organization_id,
            plant_id=plant_id,
            zone_id=zone_id,
            mounting=GateDecision.pending(),
            usage=GateDecision.pending(),
        )

    def decision(self, gate: GateKind) -> GateDecision:
        return self.mounting if GateKind(gate) is GateKind.MOUNTING else self.usage

    def with_decision(self, gate: GateKind, decision: GateDecision) -> ZoneGateState:
        if GateKind(gate) is GateKind.MOUNTING:
            return replace(self, mounting=decision)
        return replace(self, usage=decision)


@dataclass(frozen=True, slots=True, kw_only=True)
class GateInterval:
    """§2.5 ``GateStateHistory`` ⛓: ``[effective_from, effective_until)`` de una compuerta."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    gate: GateKind
    status: GateStatus
    effective_from: datetime
    effective_until: datetime | None
    decided_by: uuid.UUID
    reason_es: str | None
    record_id: uuid.UUID | None
    ledger_record_id: uuid.UUID

    @property
    def window(self) -> HalfOpenInterval:
        return HalfOpenInterval(self.effective_from, self.effective_until)


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    """Lo que una transición escribe: el estado nuevo y si cierra un intervalo abierto."""

    state: ZoneGateState
    decision: GateDecision
    closes_open_interval: bool


def plan_transition(
    current: ZoneGateState,
    gate: GateKind,
    status: GateStatus,
    *,
    at: datetime,
    decided_by: uuid.UUID,
    record_id: uuid.UUID | None,
    reason_es: str | None,
) -> TransitionPlan:
    """La transición de ``gate`` a ``status`` en ``at``: puro, sin escribir nada.

    - ``approved``: exige ``record_id`` y ningún motivo; desde cualquier estado (un acta o un
      acuerdo nuevos sobre una aprobada abren un intervalo contiguo).
    - ``revoked``: exige motivo de 10 a 500; solo desde ``approved`` (si no, ``NOT_APPROVED``);
      conserva el ``record_id`` de la aprobación revocada si no se da otro.
    - ``pending`` nunca es destino.
    """
    gate, status = GateKind(gate), GateStatus(status)
    previous = current.decision(gate)
    if status is GateStatus.PENDING:
        raise GateRuleViolated(GateViolation.REQUEST_INVALID)
    if status is GateStatus.APPROVED:
        if record_id is None or reason_es is not None:
            raise GateRuleViolated(GateViolation.REQUEST_INVALID)
    else:
        if reason_es is None or not MIN_REASON_CHARS <= len(reason_es) <= MAX_REASON_CHARS:
            raise GateRuleViolated(GateViolation.REQUEST_INVALID)
        if previous.status is not GateStatus.APPROVED:
            raise GateRuleViolated(GateViolation.NOT_APPROVED)
        record_id = previous.record_id if record_id is None else record_id
    decision = GateDecision(status, utc_instant(at), record_id, decided_by)
    return TransitionPlan(
        state=current.with_decision(gate, decision),
        decision=decision,
        closes_open_interval=previous.status is not GateStatus.PENDING,
    )


def gate_state_payload(state: ZoneGateState, issued_at: datetime) -> dict[str, Any]:
    """Carga ``GateState`` del contrato (§3.3) emitida en ``issued_at`` (7 días de vigencia)."""
    issued = utc_instant(issued_at)
    return {
        "zone_id": str(state.zone_id),
        "organization_id": str(state.organization_id),
        "plant_id": str(state.plant_id),
        "mounting_gate": state.mounting.as_json(GateKind.MOUNTING, with_author=False),
        "usage_gate": state.usage.as_json(GateKind.USAGE, with_author=False),
        "resulting_mode": state.resulting_mode.value,
        "issued_at": format_timestamp(issued),
        "valid_until": format_timestamp(issued + ENVELOPE_VALIDITY),
    }
