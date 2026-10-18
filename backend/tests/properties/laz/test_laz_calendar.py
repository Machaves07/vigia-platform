"""Propiedades del calendario del lazo (TASK-304; LC-LAZ-18; DE §2.13; tech-stack §6).

Con ``periods_and_calendars``:

- ``add_business_days(d, n)`` nunca cae en una fecha declarada ni en un día no laborable, es
  estrictamente creciente en ``n`` y entre ``d`` (excluido) y el resultado hay exactamente ``n``
  días hábiles (oráculo por recuento);
- no depende del huso del proceso: el mismo cálculo con ``TZ`` cambiado (y ``time.tzset``) da lo
  mismo (``business_deadline``, ``month_bounds``, ``local_date``, ``today``);
- ``month_bounds`` devuelve ``[inicio, fin)`` en UTC que corresponden al mes en la zona de la
  planta, también en los cambios de horario (incluidos los de medianoche): un instante está dentro
  si y solo si su mes local es el pedido, y los meses consecutivos se tocan sin hueco ni solape.

Y los bordes: ``n`` fuera de 1 a ``MAX_BUSINESS_DAYS``, más de 40 fechas en un año, días ISO
fuera de 1 a 7, fechas que no existen, periodos y zonas mal formados (con ``..``), y la zona
leída del paquete ``tzdata`` y nunca de la del sistema.

La propuesta de festivos (``proposals``) **no se aplica sin declaración**: cambiar la versión de la
biblioteca (un doble) no mueve ningún plazo calculado con la lista declarada; el cálculo de plazos
no importa la biblioteca.

Solo datos generados.
"""

from __future__ import annotations

import ast
import contextlib
import os
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.calendar import (
    PLANT_ZONES,
    PeriodAndCalendar,
    business_calendars,
    periods_and_calendars,
)
from vigia_platform.loop import calendar as calendar_package
from vigia_platform.loop.calendar import (
    MAX_BUSINESS_DAYS,
    BusinessCalendar,
    CalendarInvalid,
    add_business_days,
    business_deadline,
    is_business_day,
    local_date,
    month_bounds,
    period_of,
    plant_zone,
    today,
)
from vigia_platform.loop.calendar.proposals import (
    HolidaysLibrary,
    NonWorkingDatesProposal,
    propose_non_working_dates,
)
from vigia_platform.shared.clock import SimulatedClock

ONE_DAY: Final = timedelta(days=1)
TICK: Final = timedelta(microseconds=1)
PROCESS_ZONES: Final = ("Pacific/Kiritimati", "America/Adak", "Asia/Kathmandu", "UTC")
"""Husos del proceso con los que se repite el cálculo (UTC+14, UTC-10 con verano, UTC+5:45)."""


@contextlib.contextmanager
def process_timezone(name: str) -> Iterator[None]:
    """``TZ`` del proceso cambiado (y ``time.tzset``) mientras dura el bloque."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _business_days_between(start: date, end: date, calendar: BusinessCalendar) -> int:
    """Oráculo: días hábiles en ``(start, end]`` contados uno a uno."""
    count = 0
    day = start + ONE_DAY
    while day <= end:
        weekday_ok = day.isoweekday() in calendar.working_weekdays
        count += int(weekday_ok and day not in calendar.non_working_dates)
        day += ONE_DAY
    return count


def _computations(case: PeriodAndCalendar, n: int) -> tuple[object, ...]:
    zone = plant_zone(case.zone)
    instant = datetime(case.start.year, case.start.month, case.start.day, 12, tzinfo=UTC)
    clock = SimulatedClock(instant)
    return (
        add_business_days(case.start, n, case.calendar),
        business_deadline(instant, n, case.calendar, zone),
        business_deadline(instant, n, case.calendar, case.zone),
        month_bounds(case.zone, case.period),
        local_date(instant, zone),
        today(clock, case.zone),
        period_of(instant, zone),
    )


# --- días hábiles --------------------------------------------------------------------------------


@given(periods_and_calendars(), st.integers(1, 60))
def test_add_business_days_lands_on_a_business_day_after_exactly_n(
    case: PeriodAndCalendar, n: int
) -> None:
    result = add_business_days(case.start, n, case.calendar)
    assert result > case.start
    assert is_business_day(result, case.calendar)
    assert result.isoweekday() in case.calendar.working_weekdays
    assert result not in case.calendar.non_working_dates
    assert _business_days_between(case.start, result, case.calendar) == n
    # El día anterior al resultado todavía no completa los n.
    assert _business_days_between(case.start, result - ONE_DAY, case.calendar) == n - 1


@given(periods_and_calendars(), st.integers(1, 59))
def test_add_business_days_is_strictly_increasing_in_n(case: PeriodAndCalendar, n: int) -> None:
    assert add_business_days(case.start, n, case.calendar) < add_business_days(
        case.start, n + 1, case.calendar
    )


@given(periods_and_calendars(), st.integers(1, 30))
def test_nothing_depends_on_the_process_timezone(case: PeriodAndCalendar, n: int) -> None:
    with process_timezone("UTC"):
        expected = _computations(case, n)
    for name in PROCESS_ZONES:
        with process_timezone(name):
            # El huso del proceso cambió de verdad (salvo en UTC).
            assert (time.localtime(1_800_000_000).tm_gmtoff != 0) == (name != "UTC")
            assert _computations(case, n) == expected


@given(periods_and_calendars(), st.integers(1, 30))
def test_business_deadline_is_the_local_end_of_the_nth_business_day(
    case: PeriodAndCalendar, n: int
) -> None:
    zone = plant_zone(case.zone)
    instant = datetime(case.start.year, case.start.month, case.start.day, 15, tzinfo=UTC)
    deadline = business_deadline(instant, n, case.calendar, zone)
    last = add_business_days(local_date(instant, zone), n, case.calendar)
    assert deadline.utcoffset() == timedelta(0)
    assert local_date(deadline - TICK, zone) == last
    assert local_date(deadline, zone) > last


def test_add_business_days_borders() -> None:
    weekdays = BusinessCalendar.default()
    friday = date(2026, 10, 16)
    assert add_business_days(friday, 1, weekdays) == date(2026, 10, 19)
    assert add_business_days(friday, MAX_BUSINESS_DAYS, weekdays) > friday
    for n in (0, -1, MAX_BUSINESS_DAYS + 1, True, 1.0, "1"):
        with pytest.raises(CalendarInvalid):
            add_business_days(friday, n, weekdays)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        add_business_days(datetime(2026, 10, 16, tzinfo=UTC), 1, weekdays)
    # Solo los domingos, con el próximo domingo declarado: salta a la semana siguiente.
    sundays = BusinessCalendar.of({7}, {date(2026, 10, 18)})
    assert add_business_days(friday, 1, sundays) == date(2026, 10, 25)


def test_the_declared_calendar_borders() -> None:
    year = [date(2026, 1, 1) + timedelta(days=i) for i in range(41)]
    assert len(BusinessCalendar.of({1}, year[:40]).non_working_dates) == 40
    with pytest.raises(CalendarInvalid):
        BusinessCalendar.of({1}, year)
    # Cuarenta en cada uno de dos años sí: el tope es por año.
    other = [day.replace(year=2027) for day in year[:40]]
    assert len(BusinessCalendar.of({1}, [*year[:40], *other]).non_working_dates) == 80
    for weekdays in (set(), {0}, {8}, {True}):
        with pytest.raises(CalendarInvalid):
            BusinessCalendar.of(weekdays, [])
    with pytest.raises(CalendarInvalid):
        BusinessCalendar.of({1}, [datetime(2026, 1, 1, tzinfo=UTC)])
    good = {"working_weekdays": [1, 2, 3, 4, 5], "non_working_dates": ["2026-01-01"]}
    assert BusinessCalendar.from_document(good).document() == good
    for bad in (
        {**good, "extra": []},
        {"working_weekdays": [1]},
        {**good, "working_weekdays": [1, 1]},
        {**good, "working_weekdays": [1.0]},
        {**good, "working_weekdays": [False]},
        {**good, "working_weekdays": "12345"},
        {**good, "non_working_dates": ["2026-02-30"]},
        {**good, "non_working_dates": ["2026-1-01"]},
        {**good, "non_working_dates": ["2026-01-01", "2026-01-01"]},
        {**good, "non_working_dates": ["2026-01-01T00:00:00"]},
        {**good, "non_working_dates": [20260101]},
        {**good, "working_weekdays": list(range(1, 9))},
        ["working_weekdays"],
    ):
        with pytest.raises(CalendarInvalid):
            BusinessCalendar.from_document(bad)


# --- meses en la zona de la planta ---------------------------------------------------------------


@given(periods_and_calendars(), st.data())
def test_month_bounds_are_the_plant_month_in_utc(
    case: PeriodAndCalendar, data: st.DataObject
) -> None:
    start, end = month_bounds(case.zone, case.period)
    assert start.utcoffset() == end.utcoffset() == timedelta(0)
    assert start < end
    assert timedelta(days=27) < end - start < timedelta(days=32)
    assert period_of(start, case.zone) == case.period
    assert period_of(start - TICK, case.zone) != case.period
    assert period_of(end - TICK, case.zone) == case.period
    assert period_of(end, case.zone) != case.period
    seconds = int((end - start).total_seconds())
    offset = data.draw(st.integers(-3 * 86_400, seconds + 3 * 86_400))
    instant = start + timedelta(seconds=offset)
    assert (period_of(instant, case.zone) == case.period) == (start <= instant < end)


@given(st.sampled_from(PLANT_ZONES), st.integers(2000, 2045), st.integers(1, 12))
def test_consecutive_months_touch(zone: str, year: int, month: int) -> None:
    following = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
    assert month_bounds(zone, f"{year:04d}-{month:02d}")[1] == month_bounds(zone, following)[0]


def _midnight_gaps(zone: str, years: Iterable[int]) -> list[str]:
    """Meses cuyo día 1 empieza en un adelanto de hora a medianoche (no hay 00:00 local)."""
    tz = plant_zone(zone)
    found = []
    for year in years:
        for month in range(1, 13):
            midnight = datetime(year, month, 1, tzinfo=tz)
            if midnight.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != midnight.replace(
                tzinfo=None
            ):
                found.append(f"{year:04d}-{month:02d}")
    return found


def test_month_bounds_across_daylight_saving_changes() -> None:
    # Madrid, marzo de 2026: empieza en CET (UTC+1) y acaba en CEST (UTC+2).
    assert month_bounds("Europe/Madrid", "2026-03") == (
        datetime(2026, 2, 28, 23, tzinfo=UTC),
        datetime(2026, 3, 31, 22, tzinfo=UTC),
    )
    # Bogotá no tiene horario de verano: siempre UTC-5.
    assert month_bounds("America/Bogota", "2026-12") == (
        datetime(2026, 12, 1, 5, tzinfo=UTC),
        datetime(2027, 1, 1, 5, tzinfo=UTC),
    )
    # Un mes que empieza en un adelanto de hora a medianoche: el inicio es el instante del salto.
    gaps = _midnight_gaps("America/Asuncion", range(2000, 2046))
    assert gaps, "se esperaba algún octubre de Asunción que empieza con el adelanto de hora"
    tz = plant_zone("America/Asuncion")
    for period in gaps:
        start, _ = month_bounds(tz, period)
        assert period_of(start, tz) == period
        assert period_of(start - TICK, tz) != period
        assert start.astimezone(tz).hour == 1


def test_periods_and_zones_borders() -> None:
    for period in ("2026-13", "2026-00", "2026-1", "26-01", "2026-01-01", "", "0000-01", 202601):
        with pytest.raises(CalendarInvalid):
            month_bounds("UTC", period)  # type: ignore[arg-type]
    for zone in (
        "",
        "../../etc/passwd",
        "America/../UTC",
        "America/Bogota/",
        "/America/Bogota",
        "Nowhere/Land",
        "America",
        "a" * 40,
        "America/Bogota\n",
    ):
        with pytest.raises(CalendarInvalid):
            plant_zone(zone)
    with pytest.raises(TypeError):
        local_date(datetime(2026, 1, 1), "UTC")  # noqa: DTZ001


def test_the_zone_comes_from_the_tzdata_package(monkeypatch: pytest.MonkeyPatch) -> None:
    import zoneinfo

    plant_zone.cache_clear()
    # Sin datos del sistema: la zona sigue disponible porque se lee del paquete.
    monkeypatch.setattr(zoneinfo, "TZPATH", ())
    zone = plant_zone("America/Santiago")
    assert zone.key == "America/Santiago"
    assert month_bounds(zone, "2026-01")[0] == datetime(2026, 1, 1, 3, tzinfo=UTC)
    plant_zone.cache_clear()


# --- la propuesta de festivos no decide -----------------------------------------------------------


class FakeHolidays:
    """Doble de la biblioteca: otra «versión» con otras fechas."""

    def __init__(self, version: str, dates: Iterable[date]) -> None:
        self._version = version
        self._dates = tuple(dates)

    @property
    def version(self) -> str:
        return self._version

    def holidays(self, country: str, year: int) -> Iterable[date]:
        if country != "CO":
            raise NotImplementedError(country)
        return tuple(day for day in self._dates if day.year == year)


@given(business_calendars(2026), st.lists(st.dates(date(2026, 1, 1), date(2026, 12, 31))))
def test_a_new_library_version_moves_no_deadline_of_the_declared_list(
    declared: BusinessCalendar, other_dates: list[date]
) -> None:
    starts = [date(2026, 1, 1) + timedelta(days=i) for i in range(0, 365, 17)]
    before = [add_business_days(start, n, declared) for start in starts for n in (3, 5, 30)]
    first = propose_non_working_dates(
        "CO", 2026, source=FakeHolidays("1.0", declared.non_working_dates)
    )
    second = propose_non_working_dates("CO", 2026, source=FakeHolidays("2.0", other_dates))
    assert (first.source_version, second.source_version) == ("1.0", "2.0")
    # Ninguna de las dos propuestas cambia la lista declarada ni los plazos calculados con ella.
    after = [add_business_days(start, n, declared) for start in starts for n in (3, 5, 30)]
    assert after == before


def test_the_deadline_computation_never_imports_the_holidays_library() -> None:
    package = Path(calendar_package.__file__).parent
    for name in ("__init__.py", "business_days.py"):
        imported: set[str] = set()
        for node in ast.walk(ast.parse((package / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert "holidays" not in imported, name


def test_the_real_library_proposes_without_deciding() -> None:
    proposal = propose_non_working_dates("CO", 2026)
    assert isinstance(proposal, NonWorkingDatesProposal)
    assert proposal.source_version == HolidaysLibrary().version
    assert date(2026, 1, 1) in proposal.dates
    assert date(2026, 7, 20) in proposal.dates  # día de la independencia
    assert all(day.year == 2026 for day in proposal.dates)
    assert list(proposal.dates) == sorted(set(proposal.dates))
    assert not proposal.truncated
    assert len(proposal.as_strings()) == len(proposal.dates) <= 40
    # Proponer no declara: el calendario por defecto sigue sin festivos.
    assert BusinessCalendar.default().non_working_dates == frozenset()


def test_proposal_borders() -> None:
    many = FakeHolidays("x", [date(2026, 1, 1) + timedelta(days=i) for i in range(45)])
    proposal = propose_non_working_dates("CO", 2026, source=many)
    assert len(proposal.dates) == 40 and proposal.truncated
    for country, year in (("ZZ", 2026), ("co", 2026), ("COL", 2026), ("CO", 1999), ("CO", 2101)):
        with pytest.raises(CalendarInvalid):
            propose_non_working_dates(country, year, source=many if country == "CO" else None)
    with pytest.raises(CalendarInvalid):
        propose_non_working_dates("ZZ", 2026)
