"""Agregación temporal de presentación en zonas de ocupación unipersonal (BR-LAZ-06 y 07; DE §2.2).

En una zona marcada ``single_occupancy_zone``, toda marca que **se devuelve** a una persona es el
inicio de su ventana de agregación (``aggregation_window_minutes``, 15 a 480); el expediente
conserva la marca exacta y su hash no cambia: la agregación es solo de presentación (escenario R7).

Módulo **puro** (PAT-LAZ-SEG-09). Es la función local que declara TASK-308 mientras
``time_aggregation`` de la bandeja (TASK-307) no está en ``main``; TASK-307 la unifica. Usa la
misma cuadrícula que ``loop.projection.fold`` para ``occurred_at_bucket`` (ventanas contadas desde
1970-01-01T00:00Z).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = ["MAX_WINDOW_MINUTES", "MIN_WINDOW_MINUTES", "bucket_start", "presented"]

MIN_WINDOW_MINUTES: Final = 15
MAX_WINDOW_MINUTES: Final = 480
"""Rango de la ventana fijado por U-03 (DE §2.2, pregunta 3 = A)."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(moment: datetime, window_minutes: int) -> datetime:
    """El inicio de la ventana de ``window_minutes`` que contiene ``moment`` (en UTC)."""
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria")
    if type(window_minutes) is not int or not (
        MIN_WINDOW_MINUTES <= window_minutes <= MAX_WINDOW_MINUTES
    ):
        raise ValueError("ventana de agregación fuera de 15 a 480 minutos")
    window = timedelta(minutes=window_minutes)
    return _EPOCH + ((moment.astimezone(UTC) - _EPOCH) // window) * window


def presented(moment: datetime, *, single_occupancy: bool, window_minutes: int) -> datetime:
    """La marca que se devuelve: agregada si la zona es unipersonal, exacta si no."""
    if single_occupancy:
        return bucket_start(moment, window_minutes)
    return moment.astimezone(UTC)
