"""Tolerancia de reloj y antigüedad máxima de la ingesta (BR-GOB-90, 91; nota T-12; PR-GOB-17).

Reglas (BL §2.4 y la nota de BR-CTR-08 de U-01, que fija la misma tolerancia en el contrato):

- **tolerancia** = ``min(|node_clock.offset_ms|, 300 000 ms)``;
- con ``node_clock.synchronized = true``, la compuerta de uso se evalúa en la ventana
  ``[started_at - tol, started_at + tol]`` y el catálogo en ``[started_at - tol, ended_at + tol]``
  (vigente «en algún instante del intervalo del hecho»). **Decisión del redactor** de TASK-221
  (BR-GOB-90 no dice si la tolerancia amplía o estrecha): la ventana **amplía**, la misma lectura
  que la plataforma simulada de U-01 (``conformance.stub_platform``), que la suite de conformidad
  usa como referencia;
- con ``synchronized = false``, ambas se evalúan en el instante ``received_at`` de la plataforma
  (y la zona asignada, también: la marca del nodo no es fiable);
- **antigüedad máxima** = ``sent_records_retention_days`` del nodo (1 a 90; 30 si el nodo no tiene
  configuración, el valor por defecto de ``fleet.node_configuration``): un registro con
  ``ended_at + tol < received_at - retención`` es ``timestamp_out_of_window`` (permanente).

La cadena se ordena por recepción; la marca del nodo se conserva tal cual, con su desfase. Dominio
puro: ningún reloj del sistema, todo instante llega como argumento.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "MAX_CLOCK_TOLERANCE",
    "MAX_RETENTION_DAYS",
    "MIN_RETENTION_DAYS",
    "FactTime",
    "Window",
    "clock_tolerance",
    "retention",
]

MAX_CLOCK_TOLERANCE: Final = timedelta(minutes=5)
"""Cota de la tolerancia: 5 minutos `[estimación propia]` (BR-GOB-90)."""
MIN_RETENTION_DAYS: Final = 1
MAX_RETENTION_DAYS: Final = 90
DEFAULT_RETENTION_DAYS: Final = 30
"""``sent_records_retention_days`` por defecto (columna de ``fleet.node_configuration``, D-11)."""


def clock_tolerance(offset_ms: int) -> timedelta:
    """``min(|offset_ms|, 5 min)``; ``offset_ms`` es el desfase que declara el nodo."""
    if type(offset_ms) is not int:
        raise TypeError("offset_ms debe ser un entero")
    return min(timedelta(milliseconds=abs(offset_ms)), MAX_CLOCK_TOLERANCE)


def retention(days: int | None) -> timedelta:
    """La antigüedad máxima aceptada: ``days`` acotado a 1..90 (30 si no hay configuración)."""
    if days is None:
        days = DEFAULT_RETENTION_DAYS
    if type(days) is not int:
        raise TypeError("sent_records_retention_days debe ser un entero")
    return timedelta(days=min(max(days, MIN_RETENTION_DAYS), MAX_RETENTION_DAYS))


@dataclass(frozen=True, slots=True)
class Window:
    """Intervalo **cerrado** ``[start, end]`` de instantes."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("los extremos de la ventana llevan zona horaria")
        if self.end < self.start:
            raise ValueError("la ventana termina antes de empezar")

    @classmethod
    def at(cls, moment: datetime) -> Window:
        return cls(moment, moment)

    def overlaps(self, start: datetime, end: datetime | None) -> bool:
        """¿Hay algún instante de la ventana en ``[start, end)`` (``end`` nulo: abierto)?

        Un intervalo vacío (``end <= start``, dos publicaciones en el mismo milisegundo) no tiene
        ningún instante: nunca se cruza.
        """
        first = max(self.start, start)
        return first <= self.end and (end is None or first < end)


@dataclass(frozen=True, slots=True)
class FactTime:
    """El instante del hecho que declara el nodo y el de recepción en la plataforma."""

    started_at: datetime
    ended_at: datetime
    synchronized: bool
    offset_ms: int
    received_at: datetime

    def __post_init__(self) -> None:
        for moment in (self.started_at, self.ended_at, self.received_at):
            if moment.utcoffset() is None:
                raise ValueError("los instantes del hecho llevan zona horaria")
        if self.ended_at < self.started_at:
            raise ValueError("el hecho termina antes de empezar")

    @property
    def tolerance(self) -> timedelta:
        return clock_tolerance(self.offset_ms)

    @property
    def instant(self) -> datetime:
        """El instante en que se mira la asignación de la zona al nodo (paso 2)."""
        return self.started_at if self.synchronized else self.received_at

    @property
    def gate_window(self) -> Window:
        """Dónde tiene que haber estado aprobado el uso (paso 8)."""
        if not self.synchronized:
            return Window.at(self.received_at)
        tolerance = self.tolerance
        return Window(self.started_at - tolerance, self.started_at + tolerance)

    @property
    def catalog_window(self) -> Window:
        """Dónde tiene que haber estado vigente el catálogo que cita el registro (paso 7)."""
        if not self.synchronized:
            return Window.at(self.received_at)
        tolerance = self.tolerance
        return Window(self.started_at - tolerance, self.ended_at + tolerance)

    def too_old(self, retention_days: int | None) -> bool:
        """¿Es más antiguo que la antigüedad máxima aceptada (``timestamp_out_of_window``)?"""
        return self.ended_at + self.tolerance < self.received_at - retention(retention_days)
