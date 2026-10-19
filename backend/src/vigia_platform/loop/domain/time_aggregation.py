"""Agregación temporal de presentación en zonas de ocupación unipersonal (TASK-307;
PAT-LAZ-MAN-05; BR-LAZ-04 a 07; DE §2.2).

En una zona marcada ``single_occupancy_zone``, toda marca que **se devuelve** a una persona es el
inicio de su ventana de agregación (``aggregation_window_minutes``, 15 a 480); el expediente
conserva la marca exacta y su hash no cambia: la agregación es solo de presentación (escenario R7).

Módulo **puro** (PAT-LAZ-SEG-09) y **única** función de agregación de U-04: la bandeja (TASK-307),
la clasificación (TASK-308), el detalle (TASK-342), la exportación (TASK-318) y la exposición
(TASK-322) la importan. ``aggregate`` cuenta las ventanas desde ``window_start``; por defecto, la
cuadrícula de ``loop.projection.fold`` para ``occurred_at_bucket`` (ventanas contadas desde
1970-01-01T00:00Z), de modo que la marca devuelta coincide con la columna de la proyección.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = [
    "GRID_ORIGIN",
    "MAX_WINDOW_MINUTES",
    "MIN_WINDOW_MINUTES",
    "aggregate",
    "bucket_start",
    "presented",
]

MIN_WINDOW_MINUTES: Final = 15
MAX_WINDOW_MINUTES: Final = 480
"""Rango de la ventana fijado por U-03 (DE §2.2, pregunta 3 = A)."""

GRID_ORIGIN: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""El inicio de la primera ventana de la cuadrícula de ``occurred_at_bucket``."""


def aggregate(
    ts: datetime, window_start: datetime = GRID_ORIGIN, window_minutes: int = 60
) -> datetime:
    """El inicio de la ventana de ``window_minutes`` que contiene ``ts``: ``window_start`` más un
    múltiplo exacto de la ventana, nunca posterior a ``ts`` (en UTC)."""
    for moment in (ts, window_start):
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ValueError("marca sin zona horaria")
    if type(window_minutes) is not int or not (
        MIN_WINDOW_MINUTES <= window_minutes <= MAX_WINDOW_MINUTES
    ):
        raise ValueError("ventana de agregación fuera de 15 a 480 minutos")
    window = timedelta(minutes=window_minutes)
    origin = window_start.astimezone(UTC)
    return origin + ((ts.astimezone(UTC) - origin) // window) * window


def bucket_start(moment: datetime, window_minutes: int) -> datetime:
    """El inicio de la ventana de ``window_minutes`` que contiene ``moment`` (en UTC)."""
    return aggregate(moment, GRID_ORIGIN, window_minutes)


def presented(moment: datetime, *, single_occupancy: bool, window_minutes: int) -> datetime:
    """La marca que se devuelve: agregada si la zona es unipersonal, exacta si no."""
    if single_occupancy:
        return bucket_start(moment, window_minutes)
    return moment.astimezone(UTC)
