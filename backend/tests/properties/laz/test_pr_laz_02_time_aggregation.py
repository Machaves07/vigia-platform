"""PR-LAZ-02: agregación temporal en zonas unipersonales (TASK-307; BR-LAZ-06 y 07;
PAT-LAZ-MAN-05).

- ``aggregate(ts, window_start, window_minutes)`` devuelve ``window_start`` más un múltiplo
  **exacto** de la ventana, nunca posterior a ``ts`` y a menos de una ventana de él; es
  idempotente, no depende de la zona horaria en que llega ``ts`` y coincide con el cubo de la
  proyección (``fold.aggregation_bucket``) sobre la misma cuadrícula. Fuera de [15, 480], una
  ventana que no es entera (también ``True``) o una marca sin zona horaria: ``ValueError``.
- Sobre las filas de ``findings_over_time``: con ``single_occupancy_zone = true`` **toda** marca
  del elemento de la bandeja (y de su vista HTTP) es múltiplo exacto del inicio de la ventana; con
  ``false``, la marca es la exacta (al milisegundo de la vista). La fila no cambia: la agregación
  es solo de presentación (el ``content_hash`` del registro no se toca: lo comprueba
  ``tests/integration/test_loop_inbox_routes.py`` contra el expediente real).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.inbox import (
    findings_over_time,
    instants,
    windows,
    zones_with_privacy,
)
from vigia_platform.loop.adapters.http.findings_routes import item_view
from vigia_platform.loop.application.inbox import inbox_item
from vigia_platform.loop.domain.time_aggregation import (
    GRID_ORIGIN,
    MAX_WINDOW_MINUTES,
    MIN_WINDOW_MINUTES,
    aggregate,
    bucket_start,
    presented,
)
from vigia_platform.loop.projection.fold import aggregation_bucket
from vigia_platform.shared.signing.keys import format_timestamp


def _on_grid(moment: datetime, window: int, origin: datetime = GRID_ORIGIN) -> bool:
    return (moment - origin) % timedelta(minutes=window) == timedelta(0)


@given(instants(), instants(), windows())
def test_aggregate_is_the_exact_start_of_the_window_that_contains_the_mark(
    ts: datetime, window_start: datetime, window: int
) -> None:
    result = aggregate(ts, window_start, window)
    assert result.utcoffset() == timedelta(0)
    assert _on_grid(result, window, window_start)
    assert result <= ts < result + timedelta(minutes=window)
    assert aggregate(result, window_start, window) == result
    assert aggregate(ts.astimezone(UTC), window_start.astimezone(UTC), window) == result


@given(instants(), windows())
def test_the_default_grid_is_the_bucket_grid_of_the_projection(ts: datetime, window: int) -> None:
    assert aggregate(ts, window_minutes=window) == bucket_start(ts, window)
    assert bucket_start(ts, window) == aggregation_bucket(ts.astimezone(UTC), window)
    assert presented(ts, single_occupancy=True, window_minutes=window) == bucket_start(ts, window)
    assert presented(ts, single_occupancy=False, window_minutes=window) == ts


@given(
    instants(),
    st.one_of(
        st.integers(max_value=MIN_WINDOW_MINUTES - 1),
        st.integers(min_value=MAX_WINDOW_MINUTES + 1),
        st.just(True),
        st.floats(MIN_WINDOW_MINUTES, MAX_WINDOW_MINUTES),
    ),
)
def test_a_window_outside_15_to_480_minutes_is_rejected(ts: datetime, window: Any) -> None:
    with pytest.raises(ValueError, match="ventana"):
        aggregate(ts, GRID_ORIGIN, window)
    with pytest.raises(ValueError, match="ventana"):
        presented(ts, single_occupancy=True, window_minutes=window)


def test_the_borders_of_the_window_range_are_accepted() -> None:
    moment = datetime(2026, 10, 19, 23, 59, 59, 999999, tzinfo=UTC)
    assert aggregate(moment, GRID_ORIGIN, MIN_WINDOW_MINUTES) == datetime(
        2026, 10, 19, 23, 45, tzinfo=UTC
    )
    assert aggregate(moment, GRID_ORIGIN, MAX_WINDOW_MINUTES) == datetime(
        2026, 10, 19, 16, 0, tzinfo=UTC
    )
    # Justo en el inicio de una ventana, la marca es su propio inicio.
    start = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    assert aggregate(start, GRID_ORIGIN, MAX_WINDOW_MINUTES) == start
    assert aggregate(start - timedelta(microseconds=1), GRID_ORIGIN, MAX_WINDOW_MINUTES) == (
        datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    )


def test_a_mark_without_time_zone_is_rejected() -> None:
    naive = datetime(2026, 10, 19, 10, 0)  # noqa: DTZ001 - la marca ingenua es la entrada inválida
    with pytest.raises(ValueError, match="zona horaria"):
        aggregate(naive, GRID_ORIGIN, 60)
    with pytest.raises(ValueError, match="zona horaria"):
        aggregate(datetime(2026, 10, 19, tzinfo=UTC), naive, 60)


def _marks(row: dict[str, Any]) -> list[tuple[str, datetime | None]]:
    """Las marcas reales de la fila, con el nombre de la marca que las presenta."""

    def instant(value: Any) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)

    marks = [("occurred_at", instant(row["occurred_at_exact"])), ("due_at", instant(row["due_at"]))]
    if row["zone_agreement_revoked"]:
        marks.append(("since", instant(row["zone_agreement_revoked_at"])))
    return marks


def _shown(view: dict[str, Any], name: str) -> str | None:
    if name == "since":
        (notice,) = [n for n in view["notices"] if n["code"] == "agreement_revoked"]
        value: str | None = notice["since"]
        return value
    shown: str | None = view[name]
    return shown


@given(st.data(), zones_with_privacy())
def test_pr_laz_02_every_mark_of_an_item_is_aggregated_only_in_single_occupancy_zones(
    data: st.DataObject, zone: Any
) -> None:
    rows = data.draw(findings_over_time(zone))
    for row in rows:
        before = copy.deepcopy(row)
        item = inbox_item(row)
        view = item_view(item).model_dump(mode="json")
        assert row == before  # la fila (y el expediente) no cambia
        assert item.time_aggregated is zone.single_occupancy
        assert view["aggregation_window_minutes"] == (
            zone.window_minutes if zone.single_occupancy else None
        )
        for name, real in _marks(row):
            shown = _shown(view, name)
            if real is None:
                assert shown is None
                continue
            assert shown is not None
            moment = datetime.fromisoformat(shown)
            if zone.single_occupancy:
                assert _on_grid(moment, zone.window_minutes), (name, shown)
                assert moment <= real < moment + timedelta(minutes=zone.window_minutes)
            else:
                assert shown == format_timestamp(real.astimezone(UTC)), (name, shown)
        # El cursor es el cubo de la proyección, nunca la marca exacta.
        assert item.cursor.occurred_at_bucket == datetime.fromisoformat(row["occurred_at_bucket"])
