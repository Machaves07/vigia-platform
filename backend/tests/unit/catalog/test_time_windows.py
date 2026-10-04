"""Intervalos semiabiertos de ``catalog.domain.time_windows`` (TASK-211, tech-stack §2.6).

Bordes de la regla: contención ``[start, end)`` (el inicio entra, el fin no), contiguos que no se
solapan, no acotados, instantes sin zona horaria, intervalos vacíos o invertidos, normalización a
UTC y al milisegundo, historia rota (dos que contienen el mismo instante) e instante de relevo
monótono con el reloj quieto o hacia atrás. Más un oráculo con Hypothesis de ``overlaps`` y
``contains`` contra la definición aritmética sobre una rejilla pequeña (instantes repetidos).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.catalog.domain.time_windows import (
    MILLISECOND,
    HalfOpenInterval,
    containing,
    handover_instant,
    overlapping,
    utc_instant,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
NAIVE = T0.replace(tzinfo=None)
"""Un instante sin zona horaria: nunca se compara."""
H = timedelta(hours=1)


def _at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


# --- Contención -----------------------------------------------------------------------------


def test_the_start_is_contained_and_the_end_is_not() -> None:
    window = HalfOpenInterval(_at(0), _at(1))
    assert window.contains(_at(0))
    assert window.contains(_at(1) - MILLISECOND)
    assert not window.contains(_at(1))
    assert not window.contains(_at(0) - timedelta(microseconds=1))


def test_an_unbounded_interval_contains_everything_after_its_start() -> None:
    window = HalfOpenInterval(_at(0))
    assert not window.bounded
    assert window.contains(_at(0)) and window.contains(_at(10_000))
    assert not window.contains(_at(0) - MILLISECOND)


def test_contains_compares_instants_not_wall_clocks() -> None:
    window = HalfOpenInterval(_at(0), _at(1))
    bogota = timezone(timedelta(hours=-5))
    assert window.contains(_at(0.5).astimezone(bogota))
    assert window.start.tzinfo is UTC
    shifted = HalfOpenInterval(_at(0).astimezone(bogota), _at(1).astimezone(bogota))
    assert shifted == window


# --- Solapamiento -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((0, 1), (1, 2), False),  # contiguos: no se solapan
        ((1, 2), (0, 1), False),
        ((0, 2), (1, 3), True),
        ((0, 3), (1, 2), True),  # contenido
        ((0, None), (5, 6), True),  # no acotado
        ((5, None), (0, 5), False),  # contiguo con el no acotado
        ((0, None), (1, None), True),  # dos no acotados siempre se solapan
        ((0, 1), (0, 1), True),
    ],
)
def test_overlaps_edges(
    a: tuple[int, int | None], b: tuple[int, int | None], expected: bool
) -> None:
    first = HalfOpenInterval(_at(a[0]), None if a[1] is None else _at(a[1]))
    second = HalfOpenInterval(_at(b[0]), None if b[1] is None else _at(b[1]))
    assert first.overlaps(second) is expected
    assert second.overlaps(first) is expected


# --- Validación ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (NAIVE, None),  # sin zona horaria
        (T0 - H, NAIVE),
        (T0, T0),  # vacío
        (T0, T0 - MILLISECOND),  # invertido
        ("2026-10-01T08:00:00Z", None),
    ],
)
def test_invalid_intervals_are_rejected(start: object, end: object) -> None:
    with pytest.raises(ValueError):
        HalfOpenInterval(start, end)  # type: ignore[arg-type]


def test_a_naive_instant_is_never_compared() -> None:
    with pytest.raises(ValueError):
        HalfOpenInterval(T0).contains(NAIVE)
    with pytest.raises(ValueError):
        utc_instant(NAIVE)
    with pytest.raises(TypeError):
        HalfOpenInterval(T0).overlaps((T0, None))  # type: ignore[arg-type]


def test_closing_is_once_and_only_on_an_open_interval() -> None:
    closed = HalfOpenInterval(_at(0)).closed_at(_at(1))
    assert closed == HalfOpenInterval(_at(0), _at(1))
    with pytest.raises(ValueError):
        closed.closed_at(_at(2))
    with pytest.raises(ValueError):
        HalfOpenInterval(_at(1)).closed_at(_at(1))  # quedaría vacío


def test_utc_instant_truncates_to_the_millisecond() -> None:
    moment = T0 + timedelta(microseconds=1_999)
    assert utc_instant(moment) == T0 + MILLISECOND


# --- Consultas sobre una historia ---------------------------------------------------------------


def test_containing_returns_at_most_one_and_refuses_a_broken_history() -> None:
    history = [
        HalfOpenInterval(_at(0), _at(1)),
        HalfOpenInterval(_at(1), _at(2)),
        HalfOpenInterval(_at(2)),
    ]
    assert containing(history, _at(1), lambda w: w) == history[1]
    assert containing(history, _at(-1), lambda w: w) is None
    assert containing(history, _at(99), lambda w: w) == history[2]
    with pytest.raises(ValueError):
        containing([*history, HalfOpenInterval(_at(0.5), _at(1.5))], _at(1.2), lambda w: w)


def test_overlapping_keeps_order_and_excludes_contiguous() -> None:
    history = [HalfOpenInterval(_at(0), _at(1)), HalfOpenInterval(_at(1), _at(2))]
    query = HalfOpenInterval(_at(1), _at(3))
    assert overlapping(history, query, lambda w: w) == (history[1],)
    assert overlapping(history, HalfOpenInterval(_at(-5), _at(5)), lambda w: w) == tuple(history)
    assert overlapping([], query, lambda w: w) == ()


# --- Instante de relevo -----------------------------------------------------------------------


def test_handover_is_now_when_the_clock_moved_forward() -> None:
    assert handover_instant(_at(2), open_start=_at(1), last_issued=_at(1)) == _at(2)


def test_handover_never_leaves_an_empty_interval() -> None:
    # Reloj quieto: el intervalo abierto empezó en el mismo milisegundo.
    assert handover_instant(_at(1), open_start=_at(1)) == _at(1) + MILLISECOND
    # Reloj hacia atrás: nunca antes del inicio ni de la última emisión.
    assert handover_instant(_at(0), open_start=_at(1), last_issued=_at(3)) == _at(3)
    assert handover_instant(_at(0), open_start=_at(3), last_issued=_at(1)) == (_at(3) + MILLISECOND)
    assert handover_instant(_at(0), last_issued=_at(1)) == _at(1)
    assert handover_instant(_at(0) + timedelta(microseconds=900)) == _at(0)


# --- Oráculo --------------------------------------------------------------------------------

_GRID = st.integers(min_value=0, max_value=6)
_INTERVALS = st.tuples(_GRID, st.one_of(st.none(), _GRID)).filter(
    lambda pair: pair[1] is None or pair[1] > pair[0]
)


def _window(pair: tuple[int, int | None]) -> HalfOpenInterval:
    return HalfOpenInterval(_at(pair[0]), None if pair[1] is None else _at(pair[1]))


@given(a=_INTERVALS, b=_INTERVALS, t=_GRID)
def test_overlap_and_containment_match_the_arithmetic_definition(
    a: tuple[int, int | None], b: tuple[int, int | None], t: int
) -> None:
    a_end = float("inf") if a[1] is None else a[1]
    b_end = float("inf") if b[1] is None else b[1]
    assert _window(a).overlaps(_window(b)) == (a[0] < b_end and b[0] < a_end)
    assert _window(a).contains(_at(t)) == (a[0] <= t < a_end)
    # Dos intervalos se solapan si y solo si comparten algún instante de la rejilla fina.
    shared = any(
        _window(a).contains(_at(x / 2)) and _window(b).contains(_at(x / 2)) for x in range(0, 30)
    )
    assert _window(a).overlaps(_window(b)) == shared
