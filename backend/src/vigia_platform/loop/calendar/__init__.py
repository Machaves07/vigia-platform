"""Calendario del lazo (LC-LAZ-18): días hábiles, festivos declarados y zona horaria de la planta.

Funciones puras sobre el calendario **declarado** (``PlantLoopConfiguration.business_calendar``)
y el reloj inyectado. La biblioteca de festivos solo **propone** (``proposals``); el cálculo de
plazos nunca la consulta (tech-stack-decisions §6, BR-LAZ-145).
"""

from vigia_platform.loop.calendar.business_days import (
    MAX_BUSINESS_DAYS,
    MAX_NON_WORKING_DATES,
    MAX_NON_WORKING_DATES_PER_YEAR,
    BusinessCalendar,
    CalendarInvalid,
    add_business_days,
    business_deadline,
    is_business_day,
    local_date,
    month_bounds,
    parse_date,
    period_of,
    plant_zone,
    today,
)

__all__ = [
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
