"""El factor de regresión por entrada de la línea base (NFR-NUC-01 y NFR-GOB-64; TASK-233).

Las políticas corren sin medir en el perfil ``ci``; solo la prueba marcada de
aislamiento de clúster necesita PostgreSQL en Docker. Comprueba que

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

import ast
import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from tests.benchmarks import conftest as framework
from tests.benchmarks import laz_support as support
from tests.benchmarks import test_nfr_laz_01_operations as operations
from tests.benchmarks.conftest import (
    BASELINE,
    GOB_REGRESSION_FACTOR,
    LAZ_REGRESSION_FACTOR,
    REGRESSION_FACTOR,
    Report,
    Result,
    pytest_collection_modifyitems,
    runner_class,
    unit_regression_factor,
)
from tests.benchmarks.laz_support import Traces, measured
from tests.fleet_ingest_support import IngestSite
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization
from vigia_platform.shared.clock import SimulatedClock

BASE_MS = 10.0


@pytest.mark.integration
def test_laz_world_has_independent_postgres_roles(
    postgres_endpoint: PostgresEndpoint, laz_postgres: PostgresEndpoint
) -> None:
    """Migrar la pila de detalle no puede invalidar los pools del mundo anual."""
    assert (postgres_endpoint.host, postgres_endpoint.port) != (
        laz_postgres.host,
        laz_postgres.port,
    )


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


@pytest.fixture
def laz_baseline() -> dict[str, dict[str, Any]]:
    """Línea base manipulable; la mediana de este mismo runner es siempre 10 ms."""
    return {
        "laz_probe": {"median": BASE_MS, "p95": 12.0, "regression_factor": LAZ_REGRESSION_FACTOR}
    }


@pytest.mark.parametrize("ratio,regression", [(1.49, False), (1.5, False), (1.51, True)])
def test_laz_relative_regression(
    laz_baseline: dict[str, dict[str, Any]], ratio: float, regression: bool
) -> None:
    report = _report(laz_baseline)
    result = report.add(
        _result("laz_probe", BASE_MS * ratio, factor=unit_regression_factor("laz_probe"))
    )
    assert result.regression is regression
    if regression:
        with pytest.raises(pytest.fail.Exception, match="más allá del 50 %"):
            report.check(result)
    else:
        report.check(result)


def test_laz_unit_policy_does_not_change_previous_units() -> None:
    assert unit_regression_factor("laz_probe") == LAZ_REGRESSION_FACTOR == 1.5
    assert unit_regression_factor("gob_probe") == GOB_REGRESSION_FACTOR == 1.2
    assert unit_regression_factor("ledger_probe") == REGRESSION_FACTOR == 1.5


def test_laz_absolute_is_only_a_reported_trend(laz_baseline: dict[str, Any]) -> None:
    report = _report(laz_baseline)
    result = _result("laz_probe", BASE_MS)
    result.objective = 0.001
    result.objective_met = False
    report.check(report.add(result))
    entry = report.document()["benchmarks"]["laz_probe"]
    assert entry["objective"] == 0.001
    assert entry["objective_met"] is False
    assert entry["regression"] is False


def test_laz_missing_baseline_fails() -> None:
    report = _report({})
    with pytest.raises(pytest.fail.Exception, match="no tiene línea base"):
        report.check(report.add(_result("laz_new", BASE_MS)))


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"runner": "otro-equipo"}, "different_runner"),
        ({"details": {"profile": "nightly"}}, "different_profile"),
    ],
)
def test_laz_never_compares_different_runners_or_profiles(
    laz_baseline: dict[str, Any], change: dict[str, Any], expected: str
) -> None:
    laz_baseline["laz_probe"].update(change)
    report = _report(laz_baseline)
    result = _result("laz_probe", BASE_MS * 100)
    result.details = {"profile": "ci"}
    report.add(result)
    report.check(result)
    assert result.regression is None
    assert result.comparison == expected
    assert result.median_ratio is None and result.p95_ratio is None
    entry = report.document()["benchmarks"]["laz_probe"]
    assert entry["regression"] is None and entry["comparison"] == expected


def test_laz_hosted_runner_class_is_stable_between_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("RUNNER_NAME", "GitHub Actions 1")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setenv("RUNNER_ARCH", "X64")
    monkeypatch.setenv("ImageOS", "ubuntu24")
    monkeypatch.setattr(framework.os, "cpu_count", lambda: 4)
    first = runner_class()
    monkeypatch.setenv("RUNNER_NAME", "GitHub Actions 2")
    assert runner_class() == first == "github-hosted:Linux:X64:ubuntu24:4"
    monkeypatch.setattr(framework.os, "cpu_count", lambda: 8)
    assert runner_class() != first
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    second = runner_class()
    monkeypatch.setenv("RUNNER_NAME", "otro-runner")
    assert runner_class() != second


@pytest.mark.parametrize("ratio,regression", [(1.49, False), (1.51, True)])
def test_laz_hosted_names_do_not_disable_relative_gate(
    ratio: float, regression: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = "github-hosted:Linux:X64:ubuntu24:4"
    monkeypatch.setattr(
        framework,
        "_environment",
        lambda: {
            "runner": "GitHub Actions 2",
            "runner_class": identity,
        },
    )
    report = _report(
        {
            "laz_probe": {
                "median": BASE_MS,
                "p95": 12.0,
                "regression_factor": 1.5,
                "runner": "GitHub Actions 1",
                "runner_class": identity,
            }
        }
    )
    result = report.add(_result("laz_probe", BASE_MS * ratio))
    assert result.comparison == "same_runner"
    assert result.regression is regression
    if regression:
        with pytest.raises(pytest.fail.Exception, match="más allá del 50 %"):
            report.check(result)
    else:
        report.check(result)


def test_laz_different_runner_classes_are_only_a_trend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        framework,
        "_environment",
        lambda: {
            "runner": "GitHub Actions 2",
            "runner_class": "github-hosted:Linux:X64:ubuntu24:8",
        },
    )
    report = _report(
        {
            "laz_probe": {
                "median": BASE_MS,
                "p95": 12.0,
                "regression_factor": 1.5,
                "runner_class": "github-hosted:Linux:X64:ubuntu24:4",
            }
        }
    )
    result = report.add(_result("laz_probe", BASE_MS))
    assert result.comparison == "different_runner"
    report.check(result)
    assert result.regression is None and result.median_ratio is None


def test_laz_terminal_explicitly_reports_no_comparable_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report({"laz_probe": {**U02_ENTRY, "runner": "otro-equipo"}})
    report.add(_result("laz_probe", BASE_MS * 2))
    monkeypatch.setattr(framework, "_REPORT", report)
    lines: list[str] = []
    framework.pytest_terminal_summary(
        SimpleNamespace(section=lambda _: None, write_line=lines.append)
    )
    assert "sin base comparable: different_runner (solo tendencia)" in lines[0]
    assert "razón base" not in lines[0]


@pytest.mark.parametrize("profile", ["ci", "nightly"])
def test_laz_seed_is_bounded_in_both_profiles(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Controla la preparación real sin base ni HTTP: el doble rechaza el hallazgo 319."""
    from hypothesis import settings

    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    site = IngestSite(
        **{
            name: uuid.UUID(int=index)
            for index, name in enumerate(
                (
                    "organization_id",
                    "plant_id",
                    "node_id",
                    "zone_id",
                    "other_zone_id",
                    "camera_id",
                    "signal_id",
                    "standard_id",
                ),
                start=1,
            )
        },
        certificate=object(),
    )
    org = LoopOrganization(site)
    ingested: list[tuple[uuid.UUID, datetime]] = []

    def ingest(organization: LoopOrganization) -> object:
        ingested.append((organization.zone_id, clock.now()))
        assert len(ingested) <= 318, "la densidad completa pertenece a VIG-246"
        return object()

    world = SimpleNamespace(
        organization=lambda: org,
        ingest_finding=ingest,
        project=lambda _: SimpleNamespace(applied=True),
        stack=SimpleNamespace(
            authz=SimpleNamespace(
                operator_id=uuid.UUID(int=9),
                sessions=SimpleNamespace(clock=clock, admin=object()),
            ),
            authority=object(),
            publish_catalog=Mock(),
            usage=Mock(),
        ),
    )
    actions = SimpleNamespace(world=world, now=clock.now, execute=Mock(), run=asyncio.run)
    monkeypatch.setattr(support, "issue", AsyncMock(return_value=(object(), None)))
    previous = settings.get_current_profile_name()
    try:
        settings.load_profile(profile)
        _, zones, start, end, count = support.seed_year(actions)
    finally:
        settings.load_profile(previous)
    assert len(zones) == 20 and (end - start).days == 366
    assert count == len(ingested) == 318
    assert len({zone for zone, _ in ingested}) == 3
    assert len({at for _, at in ingested}) == 53


@pytest.mark.parametrize("rounds", [20, 100])
def test_laz_measured_forwards_declared_rounds(rounds: int) -> None:
    captured: dict[str, Any] = {}

    def fake_measure(name: str, label: str, target: Any, **kwargs: Any) -> Result:
        captured.update(kwargs)
        return _result(name, BASE_MS)

    kwargs = {} if rounds == 20 else {"rounds": rounds}
    measured(fake_measure, "laz_probe", 10, lambda _: None, **kwargs)
    assert captured["rounds"] == captured["details"]["rounds"] == rounds


@pytest.mark.parametrize("profile", ["ci", "nightly"])
@pytest.mark.parametrize("maximum,zones", [(False, 1), (True, 3)])
def test_laz_export_scope_is_bounded_in_both_profiles(
    profile: str,
    maximum: bool,
    zones: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hypothesis import settings

    captured: dict[str, Any] = {}
    monkeypatch.setattr(operations, "measured", lambda *a, **kw: captured.update(kw))
    laz = SimpleNamespace(
        composer=lambda: object(),
        zones=tuple(uuid.UUID(int=i) for i in range(1, 21)),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        dimensions=lambda: {"findings": 318},
    )
    previous = settings.get_current_profile_name()
    try:
        settings.load_profile(profile)
        operations.export_bench(laz, lambda: None, maximum=maximum)
    finally:
        settings.load_profile(previous)
    assert captured["details"]["zones"] == zones
    assert captured["details"]["days"] == 31
    assert captured["details"]["maximum_scope_bounded"] is maximum
    assert captured["details"]["findings"] == 318


def test_laz_update_keeps_other_units(laz_baseline: dict[str, Any]) -> None:
    report = _report({**laz_baseline, "gob_old": U03_ENTRY, "ledger_old": U02_ENTRY}, update=True)
    report.add(_result("laz_probe", 11.0))
    entries = report.baseline_document()["benchmarks"]
    assert entries["laz_probe"]["regression_factor"] == LAZ_REGRESSION_FACTOR
    assert entries["gob_old"] == U03_ENTRY
    assert entries["ledger_old"] == U02_ENTRY


def test_laz_trace_ports_preserve_results_without_recording_arguments() -> None:
    class Port:
        def read(self, value: str) -> str:
            return value

        async def fetch(self, value: str) -> str:
            return value

        async def fail(self, value: str) -> None:
            raise ValueError(value)

        def fail_sync(self, value: str) -> None:
            raise ValueError(value)

    port = Port()
    owner = SimpleNamespace(port=port)
    text = "Contenido sintético que no debe convertirse en atributo de traza"
    trace = Traces()
    try:
        with trace.instrument(owner, ("port",)):
            assert owner.port.read(text) == text
            assert asyncio.run(owner.port.fetch(text)) == text
            with pytest.raises(ValueError, match="Contenido sintético"):
                asyncio.run(owner.port.fail(text))
            with pytest.raises(ValueError, match="Contenido sintético"):
                owner.port.fail_sync(text)
        assert owner.port is port
        assert set(trace.breakdown()) == {"port.read", "port.fetch", "port.fail", "port.fail_sync"}
        for span in trace.exporter.get_finished_spans():
            assert span.attributes is not None
            assert set(span.attributes) == {"step"}
            assert text not in span.attributes.values()
            assert not span.events
            assert span.status.description is None
    finally:
        trace.close()


def test_laz_keeps_trace_breakdown_when_regression_fails() -> None:
    captured: list[Result] = []

    def failing_measure(name: str, label: str, target: Any, *, details: Any, **_: Any) -> Result:
        target()
        result = _result(name, BASE_MS * 2)
        result.details = dict(details)
        captured.append(result)
        pytest.fail("regresión sintética")

    with pytest.raises(pytest.fail.Exception, match="regresión sintética"):
        measured(failing_measure, "laz_probe", 10, lambda trace: trace.call("read", lambda: None))
    assert set(captured[0].details["trace_segments"]) == {"total", "read"}
    assert captured[0].details["trace_segments"]["total"]["samples"] == 1


@pytest.mark.parametrize(
    "arguments,profile,skipped",
    [
        (["tests"], "ci", True),
        (["tests/benchmarks"], "ci", False),
        (["tests"], "nightly", False),
    ],
)
def test_laz_long_banks_are_explicit_in_ci(
    arguments: list[str], profile: str, skipped: bool
) -> None:
    from hypothesis import settings

    marks: list[Any] = []
    unit_marks: list[Any] = []
    config = SimpleNamespace(args=arguments)
    bank = SimpleNamespace(path=Path("test_nfr_laz_01_operations.py"), add_marker=marks.append)
    unit = SimpleNamespace(path=Path(__file__), add_marker=unit_marks.append)
    previous = settings.get_current_profile_name()
    try:
        settings.load_profile(profile)
        pytest_collection_modifyitems(config, [bank, unit])
    finally:
        settings.load_profile(previous)
    assert bool(marks) is skipped
    assert not unit_marks


def test_u04_seventeen_rows_have_composition_and_baseline() -> None:
    """Contrato independiente de la colección: falla por banco omitido o docstring vacía."""
    directory = Path(__file__).parent
    paths = [
        directory / "test_nfr_laz_01_operations.py",
        directory / "test_laz_validators_bench.py",
        directory / "test_laz_evaluate_deadlines_bench.py",
    ]
    banks = [
        node
        for path in paths
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_laz_")
    ]
    assert len(banks) == 17
    for bank in banks:
        doc = ast.get_docstring(bank)
        assert doc and "Composición:" in doc and "Tramos:" in doc, bank.name
        assert "[objetivo propio]" in doc, bank.name
    entries = json.loads(BASELINE.read_text())["benchmarks"]
    assert {name for name in entries if name.startswith("laz_")} == {
        bank.name.removeprefix("test_") for bank in banks
    }
    for bank in banks:
        assert bank.name.removeprefix("test_") in entries, bank.name
        entry = entries[bank.name.removeprefix("test_")]
        assert entry["regression_factor"] == LAZ_REGRESSION_FACTOR
        assert not entry.get("informative", False)
        assert entry["median"] > 0 and entry["p95"] >= entry["median"]
        segments = entry["details"]["trace_segments"]
        assert segments["total"]["samples"] >= 20
        assert len(segments) >= 2
