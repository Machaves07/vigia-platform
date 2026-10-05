"""``PlantFleetThresholds``: los umbrales del panel de flota por planta (DE §3.10; BR-GOB-79).

Tres enteros ≥ 1 por planta: pendientes de la cola local (100), antigüedad del más viejo en minutos
(30) y desviación de reloj en milisegundos (5 000). Los valores por defecto son `[estimación
propia]` (respuesta 16): ninguna fuente fija estas cifras y se revisan con datos del piloto. Una
planta sin fila usa los valores por defecto; la fila la escribe ``PUT
/plants/{plant_id}/fleet-thresholds`` (``fleet.manage``) con ``updated_by`` y ``updated_at``.

El tope superior es el de la columna ``integer`` de ``gob_0018`` (2 147 483 647): un valor mayor no
se trunca ni desborda la base, se rechaza igual que uno menor que 1 (``fleet_threshold_invalid``).

Módulo puro: sin base, sin FastAPI y sin leer la hora del sistema.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

__all__ = [
    "DEFAULT_CLOCK_DRIFT_THRESHOLD_MS",
    "DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES",
    "DEFAULT_QUEUE_PENDING_THRESHOLD",
    "MAX_THRESHOLD",
    "MIN_THRESHOLD",
    "FleetThresholds",
    "ThresholdInvalid",
    "check_threshold",
]

DEFAULT_QUEUE_PENDING_THRESHOLD: Final = 100
"""Pendientes de la cola local `[estimación propia]` (BR-GOB-79)."""
DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES: Final = 30
"""Antigüedad del pendiente más viejo, en minutos `[estimación propia]` (BR-GOB-79)."""
DEFAULT_CLOCK_DRIFT_THRESHOLD_MS: Final = 5_000
"""Desviación de reloj, en milisegundos `[estimación propia]` (BR-GOB-79, NFR-GOB-45)."""
MIN_THRESHOLD: Final = 1
MAX_THRESHOLD: Final = 2_147_483_647
"""El mayor ``integer`` de PostgreSQL, el tipo de las columnas de ``plant_fleet_thresholds``."""


class ThresholdInvalid(ValueError):
    """Un umbral que no es un entero de 1 a ``MAX_THRESHOLD`` (``fleet_threshold_invalid``)."""

    def __init__(self) -> None:
        super().__init__(f"cada umbral es un entero de {MIN_THRESHOLD} a {MAX_THRESHOLD}")


def check_threshold(value: object) -> int:
    """``value`` si es un entero (no booleano) de 1 a ``MAX_THRESHOLD``; si no,
    ``ThresholdInvalid``."""
    if type(value) is not int or not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
        raise ThresholdInvalid()
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetThresholds:
    """Los umbrales vigentes de una planta; sin fila, los valores por defecto y sin autor."""

    plant_id: uuid.UUID
    queue_pending_threshold: int = DEFAULT_QUEUE_PENDING_THRESHOLD
    queue_age_threshold_minutes: int = DEFAULT_QUEUE_AGE_THRESHOLD_MINUTES
    clock_drift_threshold_ms: int = DEFAULT_CLOCK_DRIFT_THRESHOLD_MS
    updated_by: uuid.UUID | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.plant_id) is not uuid.UUID:
            raise TypeError("plant_id debe ser uuid.UUID")
        check_threshold(self.queue_pending_threshold)
        check_threshold(self.queue_age_threshold_minutes)
        check_threshold(self.clock_drift_threshold_ms)
        if (self.updated_by is None) != (self.updated_at is None):
            raise ValueError("updated_by y updated_at van juntos (o ninguno: valores por defecto)")
        if self.updated_at is not None and self.updated_at.utcoffset() is None:
            raise ValueError("updated_at lleva zona horaria")

    @property
    def is_default(self) -> bool:
        """``True`` si la planta no tiene fila: valores por defecto `[estimación propia]`."""
        return self.updated_at is None
