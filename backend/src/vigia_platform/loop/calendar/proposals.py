"""Propuesta de fechas no laborables al configurar la planta (LC-LAZ-18; tech-stack §6).

**La biblioteca ``holidays`` solo propone; la lista declarada decide.**
``propose_non_working_dates`` devuelve las fechas del calendario del país para un año, con la
versión de la fuente que las dio; el administrador las revisa, acepta o cambia y las **declara** con
``PUT
/loop/plants/{plant_id}/configuration`` (``business_calendar.non_working_dates``). El cálculo de
plazos (``business_days``) solo lee esa lista declarada y versionada, así que actualizar la
biblioteca **no mueve ningún plazo** (BR-LAZ-145).

La fuente es un puerto (``HolidaySource``) para que las pruebas cambien la «versión de la
biblioteca» por un doble; ``HolidaysLibrary`` es el adaptador real. Una propuesta tiene a lo sumo
40 fechas (el tope de la lista declarada por año, DE §2.13): si el país tuviera más, se proponen
las 40 primeras y ``truncated`` lo dice.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final, Protocol

from vigia_platform.loop.calendar.business_days import (
    MAX_NON_WORKING_DATES_PER_YEAR,
    CalendarInvalid,
)

__all__ = [
    "HolidaySource",
    "HolidaysLibrary",
    "NonWorkingDatesProposal",
    "propose_non_working_dates",
]

MIN_YEAR: Final = 2000
MAX_YEAR: Final = 2100
"""Años que se pueden proponer `[estimación propia]`."""
_COUNTRY: Final = re.compile(r"^[A-Z]{2}$")


class HolidaySource(Protocol):
    """Fuente de festivos de un país (la biblioteca o un doble de prueba)."""

    @property
    def version(self) -> str: ...

    def holidays(self, country: str, year: int) -> Iterable[date]:
        """Las fechas festivas de ``country`` (ISO 3166-1 alfa-2) en ``year``; un país
        desconocido lanza ``NotImplementedError`` o ``KeyError``."""
        ...


class HolidaysLibrary:
    """Adaptador de la biblioteca ``holidays`` (MIT), fijada en ``uv.lock``."""

    @property
    def version(self) -> str:
        return importlib.metadata.version("holidays")

    def holidays(self, country: str, year: int) -> Iterable[date]:
        import holidays

        return tuple(holidays.country_holidays(country, years=year))


@dataclass(frozen=True, slots=True)
class NonWorkingDatesProposal:
    """Lo que la fuente propone para un país y un año; **no** se aplica sin declararlo."""

    country: str
    year: int
    source_version: str
    dates: tuple[date, ...]
    truncated: bool

    def as_strings(self) -> list[str]:
        return [day.isoformat() for day in self.dates]


def propose_non_working_dates(
    country: str, year: int, *, source: HolidaySource | None = None
) -> NonWorkingDatesProposal:
    """Las fechas no laborables que el calendario de ``country`` propone para ``year``."""
    if type(country) is not str or not _COUNTRY.fullmatch(country):
        raise CalendarInvalid("país ISO 3166-1 alfa-2")
    if type(year) is not int or not MIN_YEAR <= year <= MAX_YEAR:
        raise CalendarInvalid(f"año de {MIN_YEAR} a {MAX_YEAR}")
    library = HolidaysLibrary() if source is None else source
    try:
        found = library.holidays(country, year)
        dates = sorted({day for day in found if type(day) is date and day.year == year})
    except (NotImplementedError, KeyError):
        raise CalendarInvalid("país sin calendario de festivos") from None
    return NonWorkingDatesProposal(
        country=country,
        year=year,
        source_version=library.version,
        dates=tuple(dates[:MAX_NON_WORKING_DATES_PER_YEAR]),
        truncated=len(dates) > MAX_NON_WORKING_DATES_PER_YEAR,
    )
