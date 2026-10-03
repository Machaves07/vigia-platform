"""``_Steps`` de la composición de cobertura: pintar por bisección = pintar filtrando (VIG-91).

La línea de tiempo de 31 días con el volumen máximo de eventos por zona (seguimiento de VIG-64)
llevó ``_Steps.paint`` de filtrar la lista entera en cada tramo (cuadrático) a sustituir por
bisección el rango ``[start, end]``. La propiedad compara, para toda secuencia generada de
``paint`` y ``paint_from`` en cualquier orden (solapes, rangos vacíos, bordes iguales), los puntos
de la implementación con los del oráculo literal anterior, y ``value_at`` en cada borde.

Semillas y ejemplos del perfil activo (``tests/conftest.py``). Solo datos generados.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.ledger.domain.coverage import _Steps


class _FilteringSteps:
    """El ``_Steps`` anterior a VIG-91, como oráculo: filtra la lista entera en cada tramo."""

    def __init__(self) -> None:
        self.points: list[tuple[int, int | None]] = []

    def value_at(self, t: int) -> int | None:
        previous = [value for start, value in self.points if start <= t]
        return previous[-1] if previous else None

    def paint_from(self, start: int, value: int) -> None:
        while self.points and self.points[-1][0] >= start:
            self.points.pop()
        self.points.append((start, value))

    def paint(self, start: int, end: int, value: int) -> None:
        if end <= start:
            return
        resumed = self.value_at(end)
        left = [point for point in self.points if point[0] < start]
        right = [point for point in self.points if point[0] > end]
        self.points = [*left, (start, value), (end, resumed), *right]


_moment = st.integers(min_value=0, max_value=60)
_operation = st.one_of(
    st.tuples(st.just("paint"), _moment, _moment, st.integers(0, 9)),
    st.tuples(st.just("paint_from"), _moment, st.just(0), st.integers(0, 9)),
)


@given(st.lists(_operation, max_size=40))
def test_bisection_paint_equals_filtering_paint(
    operations: list[tuple[str, int, int, int]],
) -> None:
    steps: _Steps[int] = _Steps()
    oracle = _FilteringSteps()
    for kind, start, end, value in operations:
        if kind == "paint":
            steps.paint(start, end, value)
            oracle.paint(start, end, value)
        else:
            steps.paint_from(start, value)
            oracle.paint_from(start, value)
        assert steps.points == oracle.points
    for t in range(-1, 62):
        assert steps.value_at(t) == oracle.value_at(t)


def test_paint_keeps_what_follows_and_is_a_no_op_when_empty() -> None:
    steps: _Steps[str] = _Steps()
    steps.paint_from(0, "a")
    steps.paint(10, 20, "b")
    steps.paint(15, 15, "x")  # rango vacío: no cambia nada
    assert steps.points == [(0, "a"), (10, "b"), (20, "a")]
    steps.paint(5, 12, "c")  # solapa el inicio de "b": "b" sigue desde 12
    assert steps.points == [(0, "a"), (5, "c"), (12, "b"), (20, "a")]
    steps.paint(5, 20, "d")  # bordes iguales a puntos existentes
    assert steps.points == [(0, "a"), (5, "d"), (20, "a")]
