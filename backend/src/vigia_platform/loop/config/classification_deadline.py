"""El plazo de clasificación de un hallazgo (BR-LAZ-112): una sola regla, pura, para la métrica de
acreditación (``accreditation.metrics.compute``) y la ficha de vida (``loop.application.timeline``,
TASK-585).

- La **recepción** fija el plazo: ``classification_deadline_business_days`` días hábiles desde la
  fecha local de la recepción, con el calendario y la zona horaria de la planta, según la
  configuración **vigente en la recepción**. La vigente es la de fábrica plegada con los registros
  ``loop_configuration_changed`` de la planta **anteriores** en la cadena a la recepción; un cambio
  posterior nunca altera lo ya recibido (``applies_to_new_items_only``).
- La **primera** clasificación fija el booleano: está en plazo si su ``classified_at`` es anterior
  al fin exclusivo del plazo (``business_deadline``). Reclasificar no lo cambia.

Sin reloj, sin base y sin puertos: lo que no se sabe (sin zona horaria de la planta) lo decide el
llamador.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.loop.calendar import BusinessCalendar, business_deadline
from vigia_platform.loop.config.parameters import BUSINESS_CALENDAR, LoopSettings

__all__ = [
    "CLASSIFICATION_DEADLINE_PARAMETER",
    "ClassificationDeadlineRule",
    "classified_in_time",
]

CLASSIFICATION_DEADLINE_PARAMETER: Final = "classification_deadline_business_days"


@dataclass(frozen=True, slots=True)
class ClassificationDeadlineRule:
    """Lo que fija el plazo de clasificación: los días hábiles y el calendario de la planta."""

    business_days: int
    calendar: BusinessCalendar

    @classmethod
    def defaults(cls) -> ClassificationDeadlineRule:
        """La configuración de fábrica (sin ningún cambio registrado de la planta)."""
        settings = LoopSettings.defaults()
        return cls(settings[CLASSIFICATION_DEADLINE_PARAMETER], settings.business_calendar)

    def after_change(self, new_values: Iterable[Mapping[str, Any]]) -> ClassificationDeadlineRule:
        """La regla tras un ``loop_configuration_changed`` (sus ``new_values``)."""
        days, calendar = self.business_days, self.calendar
        for setting in new_values:
            if setting["parameter"] == CLASSIFICATION_DEADLINE_PARAMETER:
                days = int(setting["integer_value"])
            elif setting["parameter"] == BUSINESS_CALENDAR:
                calendar = BusinessCalendar.from_document(setting[BUSINESS_CALENDAR])
        return ClassificationDeadlineRule(days, calendar)

    def deadline(self, received_at: datetime, timezone: str) -> datetime:
        """El fin **exclusivo** del plazo de un hallazgo recibido en ``received_at``."""
        return business_deadline(received_at, self.business_days, self.calendar, timezone)


def classified_in_time(classified_at: datetime, deadline: datetime) -> bool:
    """¿La primera clasificación llegó antes del fin exclusivo del plazo?"""
    return classified_at < deadline
