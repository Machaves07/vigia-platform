"""``_due_periods`` de ``monthly_package`` (PAT-LAZ-RES-05; TASK-342).

La parte pura y property-testable de «la tarea corrida dos veces, o tras una caída de varios
meses simulada con el reloj inyectado, sella cada periodo exactamente una vez y en orden»: aquí
solo el cálculo de qué periodos están pendientes (sin base de datos). La garantía concurrente de
«una sola vez» sobre la tarea completa (composición y sellado reales) está en
``tests/integration/test_monthly_package_task.py``, con el reloj inyectado de verdad: no se
duplica aquí.

Módulo puro bajo prueba: ``vigia_platform.accreditation.monthly.task._due_periods``. Sin E/S.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.accreditation.monthly.task import _due_date, _due_periods


def _period_of(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_index(year: int, month: int) -> int:
    """Índice de ``(year, month)`` recalculado aquí, independiente de ``task._add_months``."""
    return year * 12 + (month - 1)


def _year_month(index: int) -> tuple[int, int]:
    return index // 12, index % 12 + 1


@st.composite
def _sealed_periods(draw: st.DrawFn, today_local: date) -> frozenset[str]:
    """Un puñado de periodos sellados, cerca de ``today_local`` para que de verdad se solapen con
    la ventana de recuperación que prueba ``_due_periods``."""
    today_index = _month_index(today_local.year, today_local.month)
    offsets = draw(st.lists(st.integers(-60, 60), max_size=10, unique=True))
    periods = {_period_of(*_year_month(today_index + offset)) for offset in offsets}
    return frozenset(periods)


@given(
    st.dates(min_value=date(2024, 1, 1), max_value=date(2030, 12, 31)),
    st.integers(1, 5),
    st.integers(1, 48),
    st.data(),
)
def test_every_due_period_is_unsealed_sorted_unique_in_window_and_complete(
    today_local: date, generation_day: int, max_months: int, data: st.DataObject
) -> None:
    """Solidez (a-e) y completitud (f): cada periodo devuelto está vencido, sin sellar y dentro
    de la ventana; y todo mes vencido y sin sellar dentro de la ventana aparece en el resultado."""
    already_sealed = data.draw(_sealed_periods(today_local))
    result = _due_periods(today_local, generation_day, already_sealed, max_months)

    assert result == sorted(result)
    assert len(result) == len(set(result))

    today_index = _month_index(today_local.year, today_local.month)
    for period in result:
        assert period not in already_sealed
        year, month = (int(part) for part in period.split("-"))
        assert _due_date(year, month, generation_day) <= today_local
        assert 0 <= today_index - _month_index(year, month) <= max_months

    # Completitud: ni de más ni de menos, mes a mes, dentro de la ventana de recuperación.
    for offset in range(0, max_months + 1):
        year, month = _year_month(today_index - offset)
        period = _period_of(year, month)
        due = _due_date(year, month, generation_day) <= today_local
        if due and period not in already_sealed:
            assert period in result, (period, result)
        else:
            assert period not in result, (period, result)


@given(
    st.dates(min_value=date(2024, 1, 1), max_value=date(2030, 12, 31)),
    st.integers(1, 5),
    st.integers(1, 48),
    st.data(),
)
def test_sealing_everything_just_returned_leaves_nothing_due(
    today_local: date, generation_day: int, max_months: int, data: st.DataObject
) -> None:
    """El análogo puro de «corrida dos veces sella cada periodo una sola vez»: una vez sellado
    todo lo que salió due, una segunda pasada con el mismo reloj no devuelve nada."""
    already_sealed = data.draw(_sealed_periods(today_local))
    due = _due_periods(today_local, generation_day, already_sealed, max_months)
    again = _due_periods(today_local, generation_day, already_sealed | frozenset(due), max_months)
    assert again == []


def test_clock_gap_catch_up_includes_every_missing_month_in_chronological_order() -> None:
    """«Huecos de reloj»: tras seis meses sin correr, se recuperan los seis, del más antiguo al
    más reciente (PAT-LAZ-RES-05: «procesa los periodos que le faltan, uno por uno»)."""
    today_local = date(2026, 7, 15)  # seis meses después de enero de 2026
    result = _due_periods(today_local, generation_day=1, already_sealed=frozenset(), max_months=6)
    assert result == [
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
    ]


def test_the_catch_up_window_is_bounded_even_after_more_than_max_months_elapsed() -> None:
    """Más de 36 meses sin correr no vuelca un histórico sin fin: la recuperación se acota a
    ``max_months`` ``[estimación propia]``, aunque haya pasado más tiempo que eso."""
    # La planta no tiene ningún periodo sellado y hoy es 54 meses después de su primer mes
    # (enero de 2022): un hueco bastante mayor que la ventana de recuperación de 36 meses.
    today_local = date(2026, 7, 15)
    result = _due_periods(today_local, generation_day=1, already_sealed=frozenset(), max_months=36)
    assert len(result) == 36
    assert result[0] == "2023-07"  # el más antiguo dentro de la ventana, no enero de 2022
    assert result[-1] == "2026-06"  # el más reciente ya vencido (julio de 2026 aún no lo está)
    assert result == sorted(result)
