"""Generadores del calendario y de la configuración del lazo (TASK-304; LC-LAZ-18 y 19).

- ``periods_and_calendars``: una zona de planta (con y sin cambio de horario, también los que
  ocurren a medianoche), un mes ``AAAA-MM``, un calendario declarado (días laborables de 1 a 7 y
  hasta 40 fechas no laborables por año, cerca del mes) y un día de arranque en esa ventana.
- ``configuration_changes``: secuencias de operaciones sobre una planta, intercalando cambios de
  configuración (con o sin motivo; enteros dentro o fuera de su rango; calendario) y plazos que
  **otro módulo** fija leyendo ``loop.config`` (``AssignDeadline``).

Solo datos generados.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from hypothesis import strategies as st

from vigia_platform.loop.calendar import MAX_NON_WORKING_DATES_PER_YEAR, BusinessCalendar
from vigia_platform.loop.config.parameters import PARAMETERS

__all__ = [
    "PLANT_ZONES",
    "AssignDeadline",
    "ChangeConfiguration",
    "PeriodAndCalendar",
    "business_calendars",
    "configuration_changes",
    "periods_and_calendars",
]

PLANT_ZONES: Final = (
    "America/Bogota",
    "America/Santiago",
    "America/Asuncion",
    "America/Havana",
    "America/New_York",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "Europe/Madrid",
    "Europe/London",
    "Asia/Beirut",
    "Asia/Kolkata",
    "Australia/Lord_Howe",
    "Pacific/Chatham",
    "UTC",
)
"""Zonas con y sin horario de verano; Santiago, Asunción, La Habana y Beirut cambian de hora a
medianoche (el primer instante del día local no es las 00:00)."""
MIN_YEAR: Final = 2000
MAX_YEAR: Final = 2045


@dataclass(frozen=True)
class PeriodAndCalendar:
    zone: str
    period: str
    year: int
    month: int
    calendar: BusinessCalendar
    start: date


def _year_dates(year: int) -> st.SearchStrategy[frozenset[date]]:
    return st.frozensets(
        st.dates(min_value=date(year, 1, 1), max_value=date(year, 12, 31)),
        max_size=MAX_NON_WORKING_DATES_PER_YEAR,
    )


@st.composite
def business_calendars(draw: st.DrawFn, year: int = 2026) -> BusinessCalendar:
    """Un calendario declarado con fechas en ``year - 1``, ``year`` y ``year + 1``."""
    weekdays = draw(st.frozensets(st.integers(1, 7), min_size=1, max_size=7))
    dates: frozenset[date] = frozenset()
    for each in (year - 1, year, year + 1):
        dates |= draw(_year_dates(each))
    return BusinessCalendar(weekdays, dates)


@st.composite
def periods_and_calendars(draw: st.DrawFn) -> PeriodAndCalendar:
    year = draw(st.integers(MIN_YEAR, MAX_YEAR))
    month = draw(st.integers(1, 12))
    calendar = draw(business_calendars(year))
    start = draw(st.dates(min_value=date(year - 1, 1, 1), max_value=date(year, 12, 31)))
    return PeriodAndCalendar(
        zone=draw(st.sampled_from(PLANT_ZONES)),
        period=f"{year:04d}-{month:02d}",
        year=year,
        month=month,
        calendar=calendar,
        start=start,
    )


# --- configuración ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeConfiguration:
    """Un ``PUT`` de configuración: enteros, calendario opcional y motivo (``None``: sin)."""

    integers: dict[str, int]
    calendar: dict[str, Any] | None
    reason: str | None


@dataclass(frozen=True)
class AssignDeadline:
    """Otro módulo fija un plazo de revisión desde ``start`` leyendo la configuración vigente."""

    start: date


def _integer(name: str) -> st.SearchStrategy[int]:
    parameter = next(p for p in PARAMETERS if p.name == name)
    inside = st.integers(parameter.minimum, parameter.maximum)
    borders = st.sampled_from(
        (parameter.minimum - 1, parameter.minimum, parameter.maximum, parameter.maximum + 1)
    )
    return st.one_of(inside, inside, borders)


_REASONS: Final = st.one_of(
    st.none(),
    st.just(""),
    st.just("Corto"),
    st.sampled_from(
        (
            "Ajuste acordado en el comité de seguridad",
            "Calendario del año siguiente revisado por la gerencia",
            "Plazo ampliado por la parada programada",
        )
    ),
)


@st.composite
def _change(draw: st.DrawFn) -> ChangeConfiguration:
    names = draw(st.lists(st.sampled_from([p.name for p in PARAMETERS]), max_size=4, unique=True))
    integers = {name: draw(_integer(name)) for name in names}
    calendar = None
    if not integers or draw(st.booleans()):
        declared = draw(business_calendars(2026))
        calendar = declared.document()
    return ChangeConfiguration(integers=integers, calendar=calendar, reason=draw(_REASONS))


@st.composite
def _assign(draw: st.DrawFn) -> AssignDeadline:
    start = date(2026, 1, 1) + timedelta(days=draw(st.integers(0, 364)))
    return AssignDeadline(start=start)


def configuration_changes(max_size: int = 8) -> st.SearchStrategy[list[Any]]:
    """Cambios de configuración y plazos fijados por otro módulo, intercalados."""
    return st.lists(st.one_of(_change(), _assign()), min_size=1, max_size=max_size)
