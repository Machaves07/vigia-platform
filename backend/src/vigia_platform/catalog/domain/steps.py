"""Pasos cronometrados del walk-test y sus horas (DE §2.12; BR-GOB-44 a 47; LC-GOB-06).

- **Cronómetro del servidor** (BR-GOB-44): ``started_at`` al abrir y ``ended_at`` al cerrar, con
  el ``Clock`` de la plataforma truncado al milisegundo; el cierre ocurre una sola vez.
- **Corrección anexa** (BR-GOB-45): ``{started_at?, ended_at?, reason_es, corrected_by,
  corrected_at}`` se guarda **junto** a las marcas del servidor, que nunca se sustituyen. Una
  corrección que no corrige ninguna marca, que deja una duración negativa, que pone una marca
  después del cierre del servidor o que supera un año es ``StepRequestInvalid``.
- **Duración efectiva** (BR-GOB-47; decisión del redactor de TASK-214): la de las marcas
  corregidas si hay corrección (cada marca no corregida conserva la del servidor) y la de las
  originales si no. El acta muestra las dos (G-15).
- **Horas** (BR-GOB-46, 47; H-53): ``total_hours`` es la suma **exacta**, en milisegundos
  enteros, de las duraciones efectivas de los pasos cerrados; ``steps_summary`` las agrupa por
  ``step_kind``. Ninguna función agrupa, filtra ni ordena por ``responsible_user_id``: el
  responsable solo vive en el paso y en su registro ``commissioning_step``.

Funciones puras: ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.catalog.domain.enums import StepKind
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_STEP_DURATION_MS",
    "CorrectionRequest",
    "StepAlreadyClosed",
    "StepCorrection",
    "StepHours",
    "StepRequestInvalid",
    "WalkTestStep",
    "close_step",
    "duration_ms",
    "steps_summary",
    "total_hours",
]

MAX_STEP_DURATION_MS: Final = 366 * 24 * 3600 * 1000
"""Tope de la duración efectiva de un paso: un año, como ``DurationMs`` del acta."""
_MILLISECOND: Final = timedelta(milliseconds=1)


class StepRequestInvalid(ValueError):
    """El cierre o su corrección son incoherentes por sí mismos: ``invalid_request``."""

    def __init__(self, reason: str = "corrección del paso fuera de los límites") -> None:
        super().__init__(reason)


class StepAlreadyClosed(Exception):
    """El paso ya tiene ``ended_at``: el cierre ocurre una sola vez (``conflict``)."""

    def __init__(self) -> None:
        super().__init__("el paso ya está cerrado")


def duration_ms(start: datetime, end: datetime) -> int:
    """Milisegundos enteros de ``[start, end]`` (las marcas ya van truncadas al milisegundo)."""
    return (utc_instant(end) - utc_instant(start)) // _MILLISECOND


@dataclass(frozen=True, slots=True)
class CorrectionRequest:
    """``{started_at?, ended_at?, reason_es}`` del cuerpo; el motivo ya pasó la política."""

    reason_es: str
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StepCorrection:
    """La corrección anexa del paso (BR-GOB-45), con quién y cuándo."""

    reason_es: str
    corrected_by: uuid.UUID
    corrected_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        """Forma de ``StepCorrection`` del registro y de la columna ``correction``."""
        content: dict[str, Any] = {}
        if self.started_at is not None:
            content["started_at"] = format_timestamp(self.started_at)
        if self.ended_at is not None:
            content["ended_at"] = format_timestamp(self.ended_at)
        content["reason_es"] = self.reason_es
        content["corrected_by"] = str(self.corrected_by)
        content["corrected_at"] = format_timestamp(self.corrected_at)
        return content


@dataclass(frozen=True, slots=True)
class WalkTestStep:
    """Un paso de la sesión (DE §2.12). ``ended_at`` nulo es un paso abierto."""

    step_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    session_id: uuid.UUID
    step_kind: StepKind
    responsible_user_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None = None
    correction: StepCorrection | None = None

    @property
    def closed(self) -> bool:
        return self.ended_at is not None

    @property
    def effective_window(self) -> tuple[datetime, datetime] | None:
        """``(inicio, fin)`` efectivos de un paso cerrado; ``None`` si sigue abierto."""
        if self.ended_at is None:
            return None
        start, end = self.started_at, self.ended_at
        if self.correction is not None:
            start = self.correction.started_at or start
            end = self.correction.ended_at or end
        return start, end

    @property
    def effective_duration_ms(self) -> int | None:
        window = self.effective_window
        return None if window is None else duration_ms(*window)

    def record_content(self) -> dict[str, Any]:
        """Contenido de ``commissioning_step`` (``source_key = step_id``)."""
        content: dict[str, Any] = {
            "step_id": str(self.step_id),
            "session_id": str(self.session_id),
            "step_kind": StepKind(self.step_kind).value,
            "responsible_user_id": str(self.responsible_user_id),
            "started_at": format_timestamp(self.started_at),
        }
        if self.ended_at is not None:
            content["ended_at"] = format_timestamp(self.ended_at)
        if self.correction is not None:
            content["correction"] = self.correction.to_json()
        return content


def close_step(
    step: WalkTestStep,
    *,
    at: datetime,
    corrected_by: uuid.UUID,
    correction: CorrectionRequest | None = None,
) -> WalkTestStep:
    """El paso cerrado en ``at`` (reloj del servidor) con su corrección anexa, si la hay.

    ``ended_at`` nunca queda antes de ``started_at`` (dos instancias con relojes casi iguales);
    las marcas del servidor se conservan siempre. ``StepAlreadyClosed`` si ya tenía cierre;
    ``StepRequestInvalid`` si la corrección no es coherente.
    """
    if step.ended_at is not None:
        raise StepAlreadyClosed
    ended_at = max(utc_instant(at), step.started_at)
    closed = dataclasses.replace(step, ended_at=ended_at)
    if correction is None:
        return closed
    if correction.started_at is None and correction.ended_at is None:
        raise StepRequestInvalid("la corrección no corrige ninguna marca")
    try:
        started = None if correction.started_at is None else utc_instant(correction.started_at)
        ended = None if correction.ended_at is None else utc_instant(correction.ended_at)
    except ValueError:
        raise StepRequestInvalid("las marcas corregidas llevan zona horaria") from None
    if any(mark is not None and mark > ended_at for mark in (started, ended)):
        raise StepRequestInvalid("ninguna marca corregida es posterior al cierre del servidor")
    corrected = dataclasses.replace(
        closed,
        correction=StepCorrection(
            reason_es=correction.reason_es,
            corrected_by=corrected_by,
            corrected_at=ended_at,
            started_at=started,
            ended_at=ended,
        ),
    )
    window = corrected.effective_window
    if window is None or not 0 <= duration_ms(*window) <= MAX_STEP_DURATION_MS:
        raise StepRequestInvalid("la duración corregida es negativa o supera un año")
    return corrected


@dataclass(frozen=True, slots=True)
class StepHours:
    """Duración total de un tipo de paso, **sin responsable** (``StepSummary`` del acta)."""

    step_kind: StepKind
    duration_ms: int


def total_hours(steps: Iterable[WalkTestStep]) -> int:
    """Suma exacta, en milisegundos enteros, de las duraciones efectivas de los pasos cerrados.

    El nombre es el del diseño (``total_hours``, BR-GOB-47); la unidad es la de toda duración del
    proyecto (milisegundos enteros), así que la suma no redondea nada.
    """
    total = 0
    for step in steps:
        duration = step.effective_duration_ms
        if duration is not None:
            total += duration
    return total


def steps_summary(steps: Iterable[WalkTestStep]) -> tuple[StepHours, ...]:
    """Horas de los pasos cerrados por ``step_kind`` (en el orden de la lista cerrada), nunca por
    responsable (BR-GOB-46). Un tipo sin pasos cerrados no aparece."""
    by_kind: dict[StepKind, int] = {}
    for step in steps:
        duration = step.effective_duration_ms
        if duration is not None:
            kind = StepKind(step.step_kind)
            by_kind[kind] = by_kind.get(kind, 0) + duration
    return tuple(StepHours(kind, by_kind[kind]) for kind in StepKind if kind in by_kind)
