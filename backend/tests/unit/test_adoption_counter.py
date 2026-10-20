"""El contador y la racha de adopción sin base (TASK-323; BR-LAZ-134 a 136; DE §3.6).

- **Partición de los tipos**: todo tipo de registro que lee la proyección del lazo es actividad de
  una zona (``LOOP_ACTIVITY_BY_RECORD_TYPE``) o no lo es (``NOT_LOOP_ACTIVITY``), nunca los dos ni
  ninguno: un tipo nuevo obliga a decidir. Las clases cubren todo ``loop_activity_kind`` menos
  ``inbox_opened`` (una lectura, no una escritura).
- **Día local**: una escritura cuenta en la fecha de la planta, no en la de UTC.
- **Racha** (bordes): el día de la activación no cuenta, hoy no cuenta, la última actividad
  reinicia; el umbral se cumple con ``silent_days == N`` y no con ``N - 1``.
- **Activación**: la última de las dos aprobaciones; una proyección sin instante no es activa.

Solo datos generados.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from vigia_platform.accreditation.adoption.counter import (
    LOOP_ACTIVITY_BY_RECORD_TYPE,
    NOT_LOOP_ACTIVITY,
    activity_day,
)
from vigia_platform.accreditation.adoption.streak import adoption_due, silence_streak
from vigia_platform.accreditation.adoption.task import activation_instant
from vigia_platform.loop.domain.enums import LoopActivityKind
from vigia_platform.loop.projection.fold import PROJECTED_RECORD_TYPES

DAY = timedelta(days=1)
TODAY = date(2026, 3, 20)


def test_every_projected_record_type_is_classified_exactly_once() -> None:
    activity = set(LOOP_ACTIVITY_BY_RECORD_TYPE)
    assert not activity & NOT_LOOP_ACTIVITY
    assert activity | NOT_LOOP_ACTIVITY == set(PROJECTED_RECORD_TYPES)
    assert set(LOOP_ACTIVITY_BY_RECORD_TYPE.values()) == set(LoopActivityKind) - {
        LoopActivityKind.INBOX_OPENED
    }


def test_the_day_is_the_local_date_of_the_plant() -> None:
    late = datetime(2026, 3, 21, 2, 30, tzinfo=UTC)  # 21:30 del 20 en Bogotá (UTC-5)
    assert activity_day(late, "America/Bogota") == date(2026, 3, 20)
    assert activity_day(late, "UTC") == date(2026, 3, 21)


def test_streak_borders() -> None:
    activated = TODAY - 3 * DAY
    streak = silence_streak(today=TODAY, active_since=activated, last_activity_day=None)
    assert (streak.start, streak.silent_days) == (activated + DAY, 2)
    assert not adoption_due(streak, 3)
    assert adoption_due(streak, 2)
    # La actividad posterior a la activación manda; la de ayer deja la racha en 0.
    streak = silence_streak(today=TODAY, active_since=activated, last_activity_day=TODAY - DAY)
    assert (streak.start, streak.silent_days) == (TODAY, 0)
    # Actividad de hoy: la racha empieza mañana, sin días.
    streak = silence_streak(today=TODAY, active_since=activated, last_activity_day=TODAY)
    assert (streak.start, streak.silent_days) == (TODAY + DAY, 0)
    # Una actividad anterior a la activación no adelanta la racha.
    streak = silence_streak(today=TODAY, active_since=activated, last_activity_day=TODAY - 9 * DAY)
    assert streak.start == activated + DAY
    # Activada hoy: nada.
    assert silence_streak(today=TODAY, active_since=TODAY, last_activity_day=None).silent_days == 0


@pytest.mark.parametrize("threshold", [0, -1, True, 1.5])
def test_the_threshold_must_be_a_positive_integer(threshold: object) -> None:
    streak = silence_streak(today=TODAY, active_since=TODAY - 30 * DAY, last_activity_day=None)
    with pytest.raises(ValueError, match="adoption_silence_days"):
        adoption_due(streak, threshold)  # type: ignore[arg-type]


def test_activation_is_the_later_approval_and_a_broken_projection_is_not_active() -> None:
    mounting, usage = "2026-03-01T10:00:00.000Z", "2026-03-02T08:30:00.000Z"
    assert activation_instant(mounting, usage) == datetime(2026, 3, 2, 8, 30, tzinfo=UTC)
    assert activation_instant(usage, mounting) == datetime(2026, 3, 2, 8, 30, tzinfo=UTC)
    for broken in ((None, usage), (mounting, None), ("no es fecha", usage), ("2026-03-01", usage)):
        assert activation_instant(*broken) is None
