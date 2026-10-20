"""La racha de silencio de una zona activa (LC-LAZ-29; BR-LAZ-136; PR-LAZ-46). Funciones puras.

Una zona **activa** es la que está en modo ``productive``: montaje y uso aprobados, lo que
``zone_activated`` anuncia (U-03, ``catalog.application.agreements``). Su racha de silencio son los
**días locales completos** de la planta, consecutivos, sin ninguna actividad del lazo:

- empieza el día siguiente al **último día con actividad** o el siguiente al **día de la
  activación** (el día de la activación no cuenta: la zona no estuvo activa entero), el posterior
  de los dos;
- termina **ayer**: el día de hoy todavía no ha terminado y no se cuenta como silencioso.

``adoption_due(streak, threshold)`` es la condición de la alerta: la racha llega a
``adoption_silence_days`` días. Con la tarea corriendo cada día, la alerta sale **el día en que la
racha cumple exactamente** ``threshold`` días; tras una caída, la primera ejecución la publica con
los días que lleve (PAT-LAZ-RES-05: nada se pierde). La clave de la racha es su **día de inicio**:
mientras no haya actividad nueva ni una activación nueva, ``start`` no cambia, y por esa clave
``adoption_check`` publica una sola vez por racha.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

__all__ = ["MAX_REPORTED_SILENT_DAYS", "SilenceStreak", "adoption_due", "silence_streak"]

_ONE_DAY: Final = timedelta(days=1)

MAX_REPORTED_SILENT_DAYS: Final = 3_660
"""Tope de ``silent_days`` en la carga de ``adoption_low`` (``accreditation.events``)."""


@dataclass(frozen=True, slots=True)
class SilenceStreak:
    """La racha de silencio vigente de una zona activa."""

    start: date
    """El primer día silencioso de la racha (su clave)."""
    silent_days: int
    """Días completos, desde ``start`` hasta ayer, sin actividad; ``0`` si la racha no empezó."""


def silence_streak(
    *, today: date, active_since: date, last_activity_day: date | None
) -> SilenceStreak:
    """La racha de una zona activa desde ``active_since`` (fecha local de la activación), con su
    último día de actividad ``last_activity_day`` (o ninguno), vista el día local ``today``."""
    start = active_since + _ONE_DAY
    if last_activity_day is not None and last_activity_day + _ONE_DAY > start:
        start = last_activity_day + _ONE_DAY
    return SilenceStreak(start=start, silent_days=max(0, (today - start).days))


def adoption_due(streak: SilenceStreak, threshold: int) -> bool:
    """``True`` si la racha llega a ``threshold`` días (``adoption_silence_days``, 1 a 90)."""
    if type(threshold) is not int or threshold < 1:
        raise ValueError("adoption_silence_days debe ser un entero de al menos 1")
    return streak.silent_days >= threshold
