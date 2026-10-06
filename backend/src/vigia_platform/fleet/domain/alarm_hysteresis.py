"""Histéresis de las alarmas de flota (NFR-GOB-45; PAT-GOB-RES-04; nota de BR §7; BR-GOB-94).

La evaluación es una **función de transición**, no de estado: ``observe`` acumula las
evaluaciones consecutivas iguales de una (organización, nodo, clase) y ``transition`` decide si esa
evaluación **levanta** o **baja** la alarma, sabiendo si hay una abierta. Nunca hay un segundo
``raise`` con la alarma abierta ni un ``clear`` sin ella (BR-GOB-81).

- ``queue_over_threshold``, ``clock_drift`` y ``camera_below_min_fps`` exigen **dos evaluaciones
  consecutivas** iguales para entrar y para salir: una oscilación de un solo ciclo alrededor del
  umbral no alarma ni baja la alarma (``CONFIRMED_KINDS``).
- ``orphan_clips_growing`` entra en cuanto hay más de 50 huérfanos en la ventana (``immediate``) o
  cuando la condición se sostiene **24 h**, y sale cuando lleva 24 h sin cumplirse (nota del
  2026-09-23 de BR-GOB-94: «durante 24 h») `[objetivo propio]`.
- El resto (``node_mute``, ``version_retiring``, ``simulated_adapter_in_productive`` y
  ``certificate_expiring``) se decide en la **primera** evaluación (``REQUIRED`` de 1).

``Evaluation`` es lo que guarda ``fleet.fleet_alarm_evaluation`` (gob_0026) para las clases con
estado (``STATEFUL_KINDS``): el valor observado, cuántas evaluaciones seguidas lo repiten (con tope
en ``REQUIRED_CONSECUTIVE``: más no cambia ninguna decisión) y desde cuándo.

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.fleet.domain.enums import FleetAlarmKind

__all__ = [
    "CONFIRMED_KINDS",
    "ORPHAN_SUSTAIN",
    "REQUIRED_CONSECUTIVE",
    "STATEFUL_KINDS",
    "Evaluation",
    "Transition",
    "observe",
    "required_evaluations",
    "transition",
]

REQUIRED_CONSECUTIVE: Final = 2
"""Evaluaciones consecutivas iguales para entrar y para salir (NFR-GOB-45)."""
CONFIRMED_KINDS: Final = frozenset(
    {
        FleetAlarmKind.QUEUE_OVER_THRESHOLD,
        FleetAlarmKind.CLOCK_DRIFT,
        FleetAlarmKind.CAMERA_BELOW_MIN_FPS,
    }
)
"""Las tres clases con histéresis de dos evaluaciones (NFR-GOB-45, nota de BR §7)."""
ORPHAN_SUSTAIN: Final = timedelta(hours=24)
"""Tiempo sostenido para entrar por el 5 % y para salir de ``orphan_clips_growing`` (BR-GOB-94)."""
STATEFUL_KINDS: Final = CONFIRMED_KINDS | {FleetAlarmKind.ORPHAN_CLIPS_GROWING}
"""Clases cuya decisión depende de evaluaciones anteriores: tienen fila de estado."""


class Transition(enum.StrEnum):
    """Lo que hace una evaluación con la alarma de (clase, nodo)."""

    RAISE = "raise"
    CLEAR = "clear"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """El valor observado en las últimas evaluaciones consecutivas iguales y desde cuándo."""

    observed: bool
    consecutive: int
    since: datetime

    def __post_init__(self) -> None:
        if type(self.observed) is not bool:
            raise TypeError("observed debe ser booleano")
        if type(self.consecutive) is not int or not 1 <= self.consecutive <= REQUIRED_CONSECUTIVE:
            raise ValueError(f"consecutive va de 1 a {REQUIRED_CONSECUTIVE}")
        if self.since.utcoffset() is None:
            raise ValueError("since lleva zona horaria")


def required_evaluations(kind: FleetAlarmKind) -> int:
    """Evaluaciones consecutivas iguales que necesita ``kind`` para entrar o salir."""
    return REQUIRED_CONSECUTIVE if kind in CONFIRMED_KINDS else 1


def observe(previous: Evaluation | None, condition: bool, now: datetime) -> Evaluation:
    """La evaluación de ``now``: suma una a la racha si repite el valor; si no, empieza otra."""
    if type(condition) is not bool:
        raise TypeError("condition debe ser booleano")
    if previous is not None and previous.observed is condition:
        return Evaluation(
            condition, min(previous.consecutive + 1, REQUIRED_CONSECUTIVE), previous.since
        )
    return Evaluation(condition, 1, now)


def transition(
    kind: FleetAlarmKind,
    evaluation: Evaluation,
    *,
    open_alarm: bool,
    now: datetime,
    immediate: bool = False,
) -> Transition | None:
    """``RAISE``, ``CLEAR`` o nada para ``kind`` tras ``evaluation``, con o sin alarma abierta.

    ``immediate`` solo cuenta en ``orphan_clips_growing``: más de 50 huérfanos entra sin esperar
    las 24 h. Un ``RAISE`` exige la alarma cerrada y un ``CLEAR``, abierta (BR-GOB-81).
    """
    if evaluation.observed is open_alarm:
        return None
    if kind is FleetAlarmKind.ORPHAN_CLIPS_GROWING:
        sustained = now - evaluation.since >= ORPHAN_SUSTAIN
        if evaluation.observed:
            return Transition.RAISE if immediate or sustained else None
        return Transition.CLEAR if sustained else None
    if evaluation.consecutive < required_evaluations(kind):
        return None
    return Transition.RAISE if evaluation.observed else Transition.CLEAR
