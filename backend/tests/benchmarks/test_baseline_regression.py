"""El factor de regresión por entrada de la línea base (NFR-NUC-01 y NFR-GOB-64; TASK-233).

Sin base de datos ni ``pytest-benchmark``: corre en el perfil ``ci``. Comprueba que

- una entrada de U-03 (``regression_factor`` 1,2) falla con una mediana más de un 20 % peor y pasa
  justo por debajo, y que una entrada de U-02 (sin factor escrito: 1,5) pasa con la misma mediana
  y falla por encima del 50 %;
- un caudal se compara al revés (tasa por debajo de la base entre el factor);
- ``baseline.json`` lleva 1,2 en **todas** las entradas de U-03 (``gob_``) y ninguna de U-02
  declara otro factor que el 1,5 por defecto;
- actualizar la base conserva las entradas que la sesión no midió, con su factor.

Solo datos generados.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.benchmarks.conftest import (
    BASELINE,
    GOB_REGRESSION_FACTOR,
    REGRESSION_FACTOR,
    Report,
    Result,
)

BASE_MS = 10.0


def _result(name: str, median: float, *, factor: float = REGRESSION_FACTOR) -> Result:
    return Result(
        name=name,
        label=name,
        kind="latency",
        unit="ms",
        median=median,
        p95=median * 1.1,
        samples=100,
        objective=None,
        objective_met=None,
        p99=median * 1.2,
        regression_factor=factor,
    )


def _report(entries: dict[str, dict[str, Any]], *, update: bool = False) -> Report:
    return Report({"benchmarks": entries}, update=update)


U03_ENTRY: dict[str, Any] = {"median": BASE_MS, "p95": 12.0, "regression_factor": 1.2}
U02_ENTRY: dict[str, Any] = {"median": BASE_MS, "p95": 12.0}


@pytest.mark.parametrize(
    ("median", "regression"),
    [(BASE_MS * 1.19, False), (BASE_MS * 1.2, False), (BASE_MS * 1.21, True)],
)
def test_u03_entry_fails_beyond_twenty_percent(median: float, regression: bool) -> None:
    report = _report({"gob_x": U03_ENTRY})
    # El banco declara 1,5 a propósito: manda el factor escrito en la entrada de la base.
    result = report.add(_result("gob_x", median))
    assert result.regression_factor == GOB_REGRESSION_FACTOR
    assert result.regression is regression
    if regression:
        with pytest.raises(pytest.fail.Exception, match=r"más allá del 20 %"):
            report.check(result)
    else:
        report.check(result)


@pytest.mark.parametrize(
    ("median", "regression"),
    [(BASE_MS * 1.21, False), (BASE_MS * 1.5, False), (BASE_MS * 1.51, True)],
)
def test_u02_entry_keeps_fifty_percent(median: float, regression: bool) -> None:
    report = _report({"ledger_x": U02_ENTRY})
    result = report.add(_result("ledger_x", median))
    assert result.regression_factor == REGRESSION_FACTOR
    assert result.regression is regression
    if regression:
        with pytest.raises(pytest.fail.Exception, match=r"más allá del 50 %"):
            report.check(result)


def test_entry_without_factor_takes_the_one_the_bench_declares() -> None:
    """Una entrada de U-03 aún sin factor escrito se compara con el 1,2 que declara el banco."""
    result = _report({"gob_y": U02_ENTRY}).add(
        _result("gob_y", BASE_MS * 1.25, factor=GOB_REGRESSION_FACTOR)
    )
    assert result.regression is True


def test_rate_regression_is_the_inverse() -> None:
    report = _report({"rate": {"median": 100.0, "p95": None, "regression_factor": 1.2}})
    slow = Result("rate", "rate", "rate", "1/s", 100 / 1.21, None, 1, None, None)
    ok = Result("rate", "rate", "rate", "1/s", 100 / 1.19, None, 1, None, None)
    assert report.add(slow).regression is True
    assert report.add(ok).regression is False


def test_informative_bench_never_fails() -> None:
    """``kms:Sign`` del doble (NFR-GOB-10) se publica, pero no es puerta: ni sin base ni con una
    mediana diez veces peor."""
    informative = _result("gob_info", BASE_MS * 10, factor=GOB_REGRESSION_FACTOR)
    informative.informative = True
    _report({}).check(_report({}).add(informative))
    report = _report({"gob_info": U03_ENTRY})
    checked = report.add(informative)
    assert checked.regression is True
    report.check(checked)


def test_missing_baseline_fails_unless_updating() -> None:
    result = _report({}).add(_result("gob_new", BASE_MS, factor=GOB_REGRESSION_FACTOR))
    with pytest.raises(pytest.fail.Exception, match="no tiene línea base"):
        _report({}).check(result)
    _report({}, update=True).check(result)


def test_update_keeps_unmeasured_entries_and_writes_the_u03_factor() -> None:
    report = _report({"ledger_x": U02_ENTRY, "gob_old": U03_ENTRY}, update=True)
    report.add(_result("gob_new", 7.0, factor=GOB_REGRESSION_FACTOR))
    report.add(_result("ledger_new", 7.0))
    entries = report.baseline_document()["benchmarks"]
    assert entries["ledger_x"] == U02_ENTRY
    assert entries["gob_old"] == U03_ENTRY
    assert entries["gob_new"]["regression_factor"] == GOB_REGRESSION_FACTOR
    assert entries["gob_new"]["median"] == 7.0
    assert entries["gob_new"]["runner"] and entries["gob_new"]["measured_at"]
    assert "regression_factor" not in entries["ledger_new"]


def test_checked_in_baseline_factors() -> None:
    """``baseline.json``: 1,2 en cada entrada de U-03 y el 1,5 por defecto en las de U-02."""
    entries: dict[str, dict[str, Any]] = json.loads(BASELINE.read_text(encoding="utf-8"))[
        "benchmarks"
    ]
    u03 = {name: entry for name, entry in entries.items() if name.startswith("gob_")}
    assert u03, "la base no tiene entradas de U-03"
    assert {name: entry.get("regression_factor") for name, entry in u03.items()} == dict.fromkeys(
        u03, GOB_REGRESSION_FACTOR
    )
    u02 = {name: entry for name, entry in entries.items() if not name.startswith("gob_")}
    assert u02, "la base no tiene entradas de U-02"
    assert all(entry.get("regression_factor", REGRESSION_FACTOR) == 1.5 for entry in u02.values())
