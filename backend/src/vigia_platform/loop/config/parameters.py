"""Los parámetros de ``PlantLoopConfiguration`` con sus rangos (DE §2.13 y nota U04-H-07).

``PARAMETERS`` es la **tabla única** de los 19 parámetros enteros: nombre, mínimo, máximo, valor
inicial y fuente. Más ``business_calendar`` (``loop.calendar.BusinessCalendar``) y ``timezone``,
que se hereda de ``Plant.timezone`` (U-02 §2.2) al crear la fila y no se cambia por esta vía.
Rigen los escritos: los valores iniciales `[estimación propia]` de DE §2.13 (``sample_*``,
U04-H-12) son los de la tabla, y donde DE §2.13 no da un tope la tabla fija uno
`[estimación propia]` para que cada parámetro tenga sus dos bordes (la columna ``source``).

Reglas entre parámetros (las mismas restricciones que la tabla ``loop.plant_loop_configuration``):
``action_deadline_min_days ≤ action_deadline_default_days ≤ action_deadline_max_days``,
``unclosable_review_default_months ≤ unclosable_review_max_months`` y ``sample_min ≤
sample_max``. Un valor fuera de su rango o que rompe una de estas reglas es
``loop_deadline_out_of_range``; un valor que no es un entero, ``invalid_request`` (esquema
estricto).

Funciones puras: ``check_values`` valida, ``changed`` da los pares anterior y nuevo de un cambio y
``setting_documents`` su forma ``LoopSetting`` del registro ``loop_configuration_changed``.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.loop.calendar import BusinessCalendar
from vigia_platform.loop.detail_codes import LoopDetailCode

__all__ = [
    "BUSINESS_CALENDAR",
    "INITIAL_DEFAULTS_REASON",
    "INTEGER_PARAMETERS",
    "PARAMETERS",
    "ConfigurationRejected",
    "IntegerParameter",
    "LoopSettings",
    "PlantLoopConfiguration",
    "changed",
    "check_values",
    "setting_documents",
]

INITIAL_DEFAULTS_REASON: Final = "initial_defaults"
"""Motivo fijo del registro de origen: los valores por defecto de la planta (versión 1)."""
BUSINESS_CALENDAR: Final = "business_calendar"


class ConfigurationRejected(Exception):
    """Rechazo de negocio con su ``detail_code`` (``deadline_out_of_range`` o
    ``configuration_change_reason_required``); el estado sale de la tabla única de A-68."""

    def __init__(self, detail_code: LoopDetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


@dataclass(frozen=True, slots=True)
class IntegerParameter:
    """Un parámetro entero: rango cerrado ``[minimum, maximum]`` y valor inicial."""

    name: str
    minimum: int
    maximum: int
    default: int
    source: str

    def accepts(self, value: int) -> bool:
        return self.minimum <= value <= self.maximum


PARAMETERS: Final[tuple[IntegerParameter, ...]] = (
    IntegerParameter("classification_deadline_business_days", 1, 30, 3, "DE §2.13, respuesta 4"),
    IntegerParameter("review_deadline_business_days", 1, 30, 5, "DE §2.13, respuesta 4"),
    IntegerParameter("action_deadline_default_days", 1, 180, 30, "DE §2.13, respuesta 4"),
    IntegerParameter("action_deadline_min_days", 1, 180, 1, "DE §2.13, respuesta 4"),
    IntegerParameter("action_deadline_max_days", 1, 180, 180, "DE §2.13, respuesta 4"),
    IntegerParameter(
        "manager_highlight_overdue_days", 1, 365, 30, "PRD §10 y H-27; tope [estimación propia]"
    ),
    IntegerParameter("unclosable_review_default_months", 1, 12, 6, "DE §2.13, respuesta 7"),
    IntegerParameter("unclosable_review_max_months", 1, 12, 12, "DE §2.13, respuesta 7"),
    IntegerParameter("monthly_package_generation_day", 1, 5, 1, "DE §2.13, respuesta 13"),
    IntegerParameter("sample_percent", 1, 100, 10, "DE §2.13, respuesta 13 (U04-H-12)"),
    IntegerParameter("sample_min", 1, 1_000, 5, "U04-H-12; tope [estimación propia]"),
    IntegerParameter("sample_max", 1, 1_000, 30, "U04-H-12; tope [estimación propia]"),
    IntegerParameter("adoption_silence_days", 1, 90, 14, "DE §2.13, respuesta 17"),
    IntegerParameter("exposure_coverage_threshold_percent", 0, 100, 80, "DE §2.13, respuesta 15"),
    IntegerParameter("email_grouping_hours", 1, 24, 4, "DE §2.13, respuesta 16"),
    IntegerParameter("export_max_days", 1, 3_660, 366, "respuesta 11; tope [estimación propia]"),
    IntegerParameter("export_max_zones", 1, 500, 20, "respuesta 11; tope [estimación propia]"),
    IntegerParameter(
        "single_occupancy_default_window_minutes", 15, 480, 60, "DE §2.13 (copia de U-03)"
    ),
    IntegerParameter(
        "reminder_interval_hours", 1, 168, 24, "U04-H-07; tope [estimación propia]: una semana"
    ),
)
"""Los 19 parámetros enteros, en el orden de DE §2.13 (``reminder_interval_hours`` al final)."""

INTEGER_PARAMETERS: Final[Mapping[str, IntegerParameter]] = {p.name: p for p in PARAMETERS}

_ORDERED: Final = (
    ("action_deadline_min_days", "action_deadline_default_days"),
    ("action_deadline_default_days", "action_deadline_max_days"),
    ("unclosable_review_default_months", "unclosable_review_max_months"),
    ("sample_min", "sample_max"),
)
"""Pares ``(a, b)`` con ``a ≤ b``."""


@dataclass(frozen=True, slots=True)
class LoopSettings:
    """Los 19 enteros y el calendario: lo que un cambio puede mover."""

    integers: Mapping[str, int]
    business_calendar: BusinessCalendar

    @classmethod
    def defaults(cls) -> LoopSettings:
        return cls(
            integers={p.name: p.default for p in PARAMETERS},
            business_calendar=BusinessCalendar.default(),
        )

    def __getitem__(self, name: str) -> int:
        return self.integers[name]

    def replaced(
        self,
        integers: Mapping[str, int],
        business_calendar: BusinessCalendar | None = None,
    ) -> LoopSettings:
        return LoopSettings(
            integers={**self.integers, **integers},
            business_calendar=(
                self.business_calendar if business_calendar is None else business_calendar
            ),
        )


def check_values(settings: LoopSettings) -> LoopSettings:
    """``settings`` si cada parámetro está en su rango y se cumplen las reglas entre ellos."""
    if set(settings.integers) != set(INTEGER_PARAMETERS):
        raise ValueError("faltan o sobran parámetros")
    for parameter in PARAMETERS:
        value = settings.integers[parameter.name]
        if type(value) is not int:
            raise TypeError(f"{parameter.name} debe ser int")
        if not parameter.accepts(value):
            raise ConfigurationRejected(LoopDetailCode.DEADLINE_OUT_OF_RANGE)
    if any(settings[low] > settings[high] for low, high in _ORDERED):
        raise ConfigurationRejected(LoopDetailCode.DEADLINE_OUT_OF_RANGE)
    if not isinstance(settings.business_calendar, BusinessCalendar):
        raise TypeError("business_calendar debe ser BusinessCalendar")
    return settings


def _document(name: str, settings: LoopSettings) -> dict[str, Any]:
    if name == BUSINESS_CALENDAR:
        return {"parameter": name, "business_calendar": settings.business_calendar.document()}
    return {"parameter": name, "integer_value": settings.integers[name]}


def changed(previous: LoopSettings | None, new: LoopSettings) -> tuple[str, ...]:
    """Los parámetros que cambian (todos si no había configuración), en el orden de la tabla."""
    names = (*INTEGER_PARAMETERS, BUSINESS_CALENDAR)
    if previous is None:
        return names
    return tuple(name for name in names if _document(name, previous) != _document(name, new))


def setting_documents(names: tuple[str, ...], settings: LoopSettings) -> list[dict[str, Any]]:
    """``LoopSetting`` de ``names`` con los valores de ``settings`` (registro del expediente)."""
    return [_document(name, settings) for name in names]


@dataclass(frozen=True, slots=True)
class PlantLoopConfiguration:
    """La fila vigente de una planta: proyección de sus registros ``loop_configuration_changed``."""

    plant_id: uuid.UUID
    organization_id: uuid.UUID
    settings: LoopSettings
    timezone: str
    version: int
    changed_at: datetime
    changed_by: Mapping[str, Any]
    """Instantánea ``UserSnapshot`` de quien hizo el cambio (``user_id``, ``display_name`` y
    ``role_in_use``)."""
    change_reason: str

    def value(self, name: str) -> int:
        """El entero ``name`` de la tabla (``review_deadline_business_days``…)."""
        if name not in INTEGER_PARAMETERS:
            raise KeyError(name)
        return self.settings.integers[name]

    @property
    def business_calendar(self) -> BusinessCalendar:
        return self.settings.business_calendar

    def next(
        self,
        settings: LoopSettings,
        *,
        changed_at: datetime,
        changed_by: Mapping[str, Any],
        change_reason: str,
    ) -> PlantLoopConfiguration:
        """La versión siguiente con ``settings``; ``timezone`` no cambia."""
        return dataclasses.replace(
            self,
            settings=settings,
            version=self.version + 1,
            changed_at=changed_at,
            changed_by=dict(changed_by),
            change_reason=change_reason,
        )
