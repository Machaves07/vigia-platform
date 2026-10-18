"""Días hábiles y meses de la planta (LC-LAZ-18; DE §2.13; tech-stack-decisions §6).

Funciones **puras** sobre el calendario declarado de la planta y el reloj inyectado:

- ``BusinessCalendar``: ``working_weekdays`` (ISO 8601: 1 = lunes … 7 = domingo, al menos uno) y
  ``non_working_dates``, la lista **declarada** de fechas no laborables, con a lo sumo 40 por año
  (DE §2.13). El valor por defecto es lunes a viernes sin festivos (``[hipótesis]`` de DE §2.13):
  ninguna fuente define el calendario laboral, lo declara la planta;
- ``is_business_day`` y ``add_business_days``: el día ``n`` hábil **después** de ``start``
  (``n`` ≥ 1). Nunca cae en un día no laborable ni en una fecha declarada y es estrictamente
  creciente en ``n``;
- ``business_deadline``: el fin (exclusivo) del día ``n`` hábil contado desde la fecha local de un
  instante, en la zona de la planta y devuelto en UTC;
- ``month_bounds``: ``[inicio, fin)`` en UTC del mes ``AAAA-MM`` en la zona de la planta, también
  cuando el mes empieza o acaba en un cambio de horario; ``period_of`` es su inversa;
- ``today``: la fecha local de la planta según el ``Clock`` inyectado (ningún módulo lee la hora
  del sistema).

**Zona horaria** (``plant_zone``): ``zoneinfo`` con los datos del paquete ``tzdata`` fijado en
``uv.lock``, leídos de ese paquete y nunca de los del sistema operativo de la imagen: el mes de un
paquete sellado no puede depender de la imagen base. Nada aquí usa la zona del proceso (``TZ``):
toda conversión es explícita, y lo que se almacena va en UTC.

La biblioteca de festivos **no se importa aquí**: solo propone al configurar
(``loop.calendar.proposals``), y el cálculo de plazos usa siempre la lista declarada
(BR-LAZ-145): actualizar la biblioteca no mueve ningún plazo.
"""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import Any, Final, Self
from zoneinfo import ZoneInfo

from vigia_platform.shared.clock import Clock

__all__ = [
    "DEFAULT_WORKING_WEEKDAYS",
    "MAX_BUSINESS_DAYS",
    "MAX_NON_WORKING_DATES",
    "MAX_NON_WORKING_DATES_PER_YEAR",
    "BusinessCalendar",
    "CalendarInvalid",
    "add_business_days",
    "business_deadline",
    "is_business_day",
    "local_date",
    "month_bounds",
    "parse_date",
    "period_of",
    "plant_zone",
    "today",
]

MAX_NON_WORKING_DATES_PER_YEAR: Final = 40
"""Fechas no laborables declaradas por año (DE §2.13)."""
MAX_NON_WORKING_DATES: Final = 400
"""Tope de la lista: 40 por año durante diez años (``record_types.MAX_NON_WORKING_DATES``)."""
MAX_BUSINESS_DAYS: Final = 3_660
"""Tope de ``n`` en ``add_business_days`` `[estimación propia]`: diez años de días hábiles cubren
cualquier plazo de DE §2.13 (el mayor, 180 días)."""
DEFAULT_WORKING_WEEKDAYS: Final = frozenset({1, 2, 3, 4, 5})
"""Lunes a viernes (``[hipótesis]`` de DE §2.13)."""

_DATE: Final = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
_PERIOD: Final = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
_ZONE_KEY: Final = re.compile(r"^[A-Za-z0-9_+-]{1,32}(?:/[A-Za-z0-9_+-]{1,32}){0,2}$")
"""Clave IANA: la forma de ``identity.plant.timezone`` (``plant_timezone_format``), sin ``..``."""
_ONE_DAY: Final = timedelta(days=1)


class CalendarInvalid(ValueError):
    """Calendario, fecha, periodo o zona horaria fuera de su forma o de sus límites."""


def _is_int(value: object) -> bool:
    return type(value) is int


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """El calendario laboral declarado por la planta (DE §2.13)."""

    working_weekdays: frozenset[int]
    non_working_dates: frozenset[date]

    def __post_init__(self) -> None:
        if not isinstance(self.working_weekdays, frozenset) or not isinstance(
            self.non_working_dates, frozenset
        ):
            raise CalendarInvalid("los días y las fechas van como frozenset")
        if not self.working_weekdays or not all(
            _is_int(day) and 1 <= day <= 7 for day in self.working_weekdays
        ):
            raise CalendarInvalid("working_weekdays: de 1 a 7 días ISO (1 = lunes)")
        if not all(type(day) is date for day in self.non_working_dates):
            raise CalendarInvalid("non_working_dates: fechas de calendario")
        if len(self.non_working_dates) > MAX_NON_WORKING_DATES:
            raise CalendarInvalid(f"non_working_dates: a lo sumo {MAX_NON_WORKING_DATES}")
        per_year = Counter(day.year for day in self.non_working_dates)
        if any(count > MAX_NON_WORKING_DATES_PER_YEAR for count in per_year.values()):
            raise CalendarInvalid(
                f"non_working_dates: a lo sumo {MAX_NON_WORKING_DATES_PER_YEAR} por año"
            )

    @classmethod
    def default(cls) -> BusinessCalendar:
        """Lunes a viernes, sin festivos cargados."""
        return cls(DEFAULT_WORKING_WEEKDAYS, frozenset())

    @classmethod
    def of(cls, working_weekdays: Iterable[int], non_working_dates: Iterable[date]) -> Self:
        return cls(frozenset(working_weekdays), frozenset(non_working_dates))

    @classmethod
    def from_document(cls, document: object) -> BusinessCalendar:
        """El calendario de ``{working_weekdays: [int], non_working_dates: ["AAAA-MM-DD"]}``.

        Estricto: exactamente esas dos claves, listas sin repetidos, enteros que no son ``bool``
        y fechas ``AAAA-MM-DD`` que existen. Cualquier otra forma es ``CalendarInvalid``.
        """
        if not isinstance(document, Mapping) or set(document) != {
            "working_weekdays",
            "non_working_dates",
        }:
            raise CalendarInvalid("el calendario lleva working_weekdays y non_working_dates")
        weekdays = document["working_weekdays"]
        dates = document["non_working_dates"]
        if not isinstance(weekdays, list | tuple) or not isinstance(dates, list | tuple):
            raise CalendarInvalid("working_weekdays y non_working_dates son listas")
        if len(weekdays) > 7 or len(dates) > MAX_NON_WORKING_DATES:
            raise CalendarInvalid("lista del calendario demasiado larga")
        if not all(_is_int(day) for day in weekdays) or len(set(weekdays)) != len(weekdays):
            raise CalendarInvalid("working_weekdays: enteros sin repetir")
        parsed = tuple(parse_date(value) for value in dates)
        if len(set(parsed)) != len(parsed):
            raise CalendarInvalid("non_working_dates: fechas sin repetir")
        return cls(frozenset(weekdays), frozenset(parsed))

    def document(self) -> dict[str, Any]:
        """La forma de ``business_calendar`` (jsonb y registro), ordenada."""
        return {
            "working_weekdays": sorted(self.working_weekdays),
            "non_working_dates": [day.isoformat() for day in sorted(self.non_working_dates)],
        }


def parse_date(value: object) -> date:
    """``AAAA-MM-DD`` que existe; si no, ``CalendarInvalid``."""
    if type(value) is not str or not _DATE.fullmatch(value):
        raise CalendarInvalid("fecha AAAA-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CalendarInvalid("la fecha no existe") from None


def _check_calendar(calendar: object) -> BusinessCalendar:
    if not isinstance(calendar, BusinessCalendar):
        raise TypeError("calendar debe ser BusinessCalendar")
    return calendar


def is_business_day(day: date, calendar: BusinessCalendar) -> bool:
    """¿Es ``day`` laborable y no está en la lista declarada?"""
    if type(day) is not date:
        raise TypeError("day debe ser date")
    _check_calendar(calendar)
    return day.isoweekday() in calendar.working_weekdays and day not in calendar.non_working_dates


def add_business_days(start: date, n: int, calendar: BusinessCalendar) -> date:
    """El día ``n`` hábil estrictamente posterior a ``start`` (``1 ≤ n ≤ MAX_BUSINESS_DAYS``)."""
    if type(start) is not date:
        raise TypeError("start debe ser date")
    _check_calendar(calendar)
    if not _is_int(n) or not 1 <= n <= MAX_BUSINESS_DAYS:
        raise CalendarInvalid(f"n es un entero de 1 a {MAX_BUSINESS_DAYS}")
    day = start
    remaining = n
    # Termina: hay al menos un día laborable por semana y la lista declarada es finita.
    try:
        while remaining:
            day += _ONE_DAY
            if is_business_day(day, calendar):
                remaining -= 1
    except OverflowError:
        raise CalendarInvalid("el plazo sale del calendario representable") from None
    return day


@functools.lru_cache(maxsize=512)
def plant_zone(key: str) -> ZoneInfo:
    """La zona IANA ``key`` con los datos del paquete ``tzdata`` (nunca los del sistema)."""
    if type(key) is not str or not _ZONE_KEY.fullmatch(key):
        raise CalendarInvalid("zona horaria IANA")
    resource = resources.files("tzdata").joinpath("zoneinfo", *key.split("/"))
    try:
        with resource.open("rb") as data:
            return ZoneInfo.from_file(data, key=key)
    except (OSError, ValueError):
        raise CalendarInvalid("zona horaria desconocida") from None


def _zone(zone: ZoneInfo | str) -> ZoneInfo:
    if isinstance(zone, ZoneInfo):
        return zone
    return plant_zone(zone)


def _aware(instant: datetime) -> datetime:
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise TypeError("instant debe ser un datetime con zona horaria")
    return instant


def _local_midnight_utc(day: date, zone: ZoneInfo) -> datetime:
    """El primer instante de ``day`` en ``zone``, en UTC.

    Si la medianoche local no existe (adelanto de hora a las 00:00), ``fold = 0`` toma el
    desplazamiento anterior al cambio, que da el instante del salto: el primero del día local.
    """
    try:
        return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)
    except OverflowError:
        raise CalendarInvalid("el instante sale del calendario representable") from None


def local_date(instant: datetime, zone: ZoneInfo | str) -> date:
    """La fecha de ``instant`` en la zona de la planta."""
    return _aware(instant).astimezone(_zone(zone)).date()


def today(clock: Clock, zone: ZoneInfo | str) -> date:
    """La fecha local de la planta según el reloj inyectado."""
    return local_date(clock.now(), zone)


def business_deadline(
    instant: datetime, n: int, calendar: BusinessCalendar, zone: ZoneInfo | str
) -> datetime:
    """Fin exclusivo, en UTC, del día ``n`` hábil posterior a la fecha local de ``instant``."""
    tz = _zone(zone)
    last = add_business_days(local_date(instant, tz), n, calendar)
    try:
        following = last + _ONE_DAY
    except OverflowError:
        raise CalendarInvalid("el plazo sale del calendario representable") from None
    return _local_midnight_utc(following, tz)


def _period(period: object) -> tuple[int, int]:
    match = _PERIOD.fullmatch(period) if type(period) is str else None
    if match is None:
        raise CalendarInvalid("periodo AAAA-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= year <= 9998:
        raise CalendarInvalid("año del periodo fuera de rango")
    return year, month


def month_bounds(plant_tz: ZoneInfo | str, period: str) -> tuple[datetime, datetime]:
    """``[inicio, fin)`` en UTC del mes ``AAAA-MM`` en la zona de la planta."""
    tz = _zone(plant_tz)
    year, month = _period(period)
    first = date(year, month, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return _local_midnight_utc(first, tz), _local_midnight_utc(following, tz)


def period_of(instant: datetime, zone: ZoneInfo | str) -> str:
    """El mes ``AAAA-MM`` de ``instant`` en la zona de la planta."""
    day = local_date(instant, zone)
    return f"{day.year:04d}-{day.month:02d}"
