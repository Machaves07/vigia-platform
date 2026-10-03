"""La regresión de los bancos es bloqueante (TASK-142, VIG-91; NFR-NUC-01).

``tests/benchmarks/conftest.py`` compara cada banco con ``baseline.json``: una mediana más de un
50 % peor que la base falla la prueba (y con ella ``nightly``), igual que un banco sin base; una
tasa que cae por debajo de la base entre 1,5 también; al actualizar la base nada falla. Además,
el p95 es el percentil por rango más cercano. Ejemplos en el borde de cada regla.
"""

from __future__ import annotations

import pytest

from tests.benchmarks.conftest import Report, Result, median, percentile


def _result(name: str, value: float, kind: str = "latency") -> Result:
    return Result(
        name=name,
        label=name,
        kind="rate" if kind == "rate" else "latency",
        unit="ms",
        median=value,
        p95=value,
        samples=100,
        objective=None,
        objective_met=None,
    )


BASELINE = {
    "benchmarks": {
        "op": {"median": 100.0, "p95": 120.0},
        "rate": {"median": 300.0, "p95": None},
    }
}


@pytest.mark.parametrize(("value", "fails"), [(100.0, False), (150.0, False), (150.001, True)])
def test_latency_regression_beyond_fifty_percent_fails(value: float, fails: bool) -> None:
    report = Report(BASELINE, update=False)
    result = report.add(_result("op", value))
    assert result.regression is fails
    assert result.median_ratio == pytest.approx(value / 100.0)
    if fails:
        with pytest.raises(pytest.fail.Exception, match="regresión en op"):
            report.check(result)
    else:
        report.check(result)


@pytest.mark.parametrize(("value", "fails"), [(300.0, False), (200.0, False), (199.99, True)])
def test_rate_drop_beyond_fifty_percent_fails(value: float, fails: bool) -> None:
    report = Report(BASELINE, update=False)
    result = report.add(_result("rate", value, kind="rate"))
    assert result.regression is fails
    if fails:
        with pytest.raises(pytest.fail.Exception, match="regresión en rate"):
            report.check(result)
    else:
        report.check(result)


def test_benchmark_without_baseline_fails_unless_updating() -> None:
    with pytest.raises(pytest.fail.Exception, match="no tiene línea base"):
        Report(BASELINE, update=False).check(_result("nuevo", 1.0))
    updating = Report(BASELINE, update=True)
    updating.check(updating.add(_result("nuevo", 1.0)))
    updating.check(updating.add(_result("op", 1_000.0)))  # al actualizar, ni regresión falla
    assert updating.baseline_document()["benchmarks"]["nuevo"]["median"] == 1.0


def test_nearest_rank_percentile_and_median() -> None:
    values = [float(n) for n in range(1, 101)]
    assert percentile(values, 0.95) == 95.0
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([1.0, 2.0], 0.95) == 2.0
    assert median(values) == 50.5
    assert median([3.0, 1.0, 2.0]) == 2.0
    with pytest.raises(ValueError):
        percentile([], 0.95)
