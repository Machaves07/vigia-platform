"""Marco de los bancos de NFR-NUC-01 y 02 (TASK-142, VIG-91; PAT-NUC-REN-07) y de U-03 (TASK-233).

Cada banco de operación corre con ``pytest-benchmark`` (``pedantic``: 100 rondas de una
iteración, con calentamiento; la preparación de cada ronda queda fuera de la medida) contra
PostgreSQL 16 y LocalStack en contenedores, y deja en el **informe** su mediana, su p95 y su p99
(rango más cercano sobre los tiempos de las rondas), el objetivo del diseño (``[objetivo propio]``
de NFR-NUC-01 y 04 o de NFR-GOB-01 a 10) y si lo cumple. Los bancos de caudal (NFR-NUC-02) dejan
su tasa.

**Línea base y regresión**: ``baseline.json`` guarda la mediana y el p95 medidos de cada banco y,
en las entradas de U-03, su **factor de regresión**. Un banco **falla** si su mediana supera
``factor`` veces la de la base (o, en un caudal, si su tasa cae por debajo de la base dividida
entre ``factor``), así que ``nightly`` falla. El factor es el de la entrada de la base; sin él,
el que declara el banco:

- U-02 (NFR-NUC-01: «una regresión superior al 50 %… falla la canalización»): 1,5, el valor por
  defecto; sus entradas no lo llevan escrito;
- U-03 (NFR-GOB-01 y 64: «regresión superior al 20 %»): 1,2 (``GOB_REGRESSION_FACTOR``), escrito
  en cada entrada.

Se compara la mediana y no el p95: con 100 rondas en un equipo compartido, el p95 depende de una
o dos rondas y fallaría por ruido; el p95, el p99 y su razón frente a la base van en el informe
como **tendencia** (nota del 2026-09-21 de NFR-GOB §1: en ``nightly`` los objetivos absolutos no
son puerta). Un banco sin entrada en la base también falla: la base se actualiza a propósito,
nunca por omisión.

- ``VIGIA_BENCHMARK_REPORT``: ruta del informe JSON (por defecto
  ``.benchmarks/vigia-benchmarks.json``, que ``.gitignore`` excluye); es el artefacto que publica
  ``nightly``.
- ``VIGIA_BENCHMARK_UPDATE_BASELINE=1``: reescribe ``baseline.json`` con lo medido (sin comparar)
  al terminar la sesión; las entradas que la sesión no midió se conservan tal cual. Se hace al
  cerrar una etiqueta, con la medición en el PR.

Las cifras del diseño son objetivos hasta que la medición los sustituya (P6): el informe dice si
se cumplen; los bancos solo fallan por regresión o por los criterios de caudal de TASK-142.
"""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import pytest

from tests.gob_platform_support import GobPlatform, gob_platform
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint

BACKEND: Final = Path(__file__).resolve().parents[2]
BASELINE: Final = Path(__file__).with_name("baseline.json")
DEFAULT_REPORT: Final = BACKEND / ".benchmarks" / "vigia-benchmarks.json"
REPORT_VARIABLE: Final = "VIGIA_BENCHMARK_REPORT"
UPDATE_VARIABLE: Final = "VIGIA_BENCHMARK_UPDATE_BASELINE"

ROUNDS: Final = 100
WARMUP_ROUNDS: Final = 3
REGRESSION_FACTOR: Final = 1.5
"""Regresión superior al 50 % frente a la base (NFR-NUC-01): el de las entradas de U-02."""
GOB_REGRESSION_FACTOR: Final = 1.2
"""Regresión superior al 20 % frente a la base (NFR-GOB-01 y 64): el de las entradas de U-03."""


def percentile(values: list[float], fraction: float) -> float:
    """Percentil por rango más cercano (p95 de 100 valores: el 95.º ordenado)."""
    if not values:
        raise ValueError("sin valores")
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


@dataclass
class Result:
    """Una entrada del informe."""

    name: str
    label: str
    kind: Literal["latency", "rate"]
    unit: str
    median: float
    p95: float | None
    samples: int
    objective: float | None
    objective_met: bool | None
    p99: float | None = None
    regression_factor: float = REGRESSION_FACTOR
    """El que declara el banco; si la entrada de la base lleva el suyo, manda el de la base."""
    informative: bool = False
    """Solo se publica (p. ej. ``kms:Sign`` del doble, NFR-GOB-10): nunca falla por regresión."""
    baseline_median: float | None = None
    baseline_p95: float | None = None
    median_ratio: float | None = None
    p95_ratio: float | None = None
    regression: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)


class Report:
    """Resultados de la sesión, comparados con ``baseline.json``."""

    def __init__(self, baseline: dict[str, Any], *, update: bool) -> None:
        self.baseline = baseline
        self.update = update
        self.results: dict[str, Result] = {}

    def add(self, result: Result) -> Result:
        base = self.baseline.get("benchmarks", {}).get(result.name)
        if base is not None:
            if base.get("regression_factor") is not None:
                result.regression_factor = float(base["regression_factor"])
            factor = result.regression_factor
            result.baseline_median = float(base["median"])
            result.baseline_p95 = None if base.get("p95") is None else float(base["p95"])
            result.median_ratio = result.median / result.baseline_median
            if result.p95 is not None and result.baseline_p95:
                result.p95_ratio = result.p95 / result.baseline_p95
            if result.kind == "latency":
                result.regression = result.median > factor * result.baseline_median
            else:
                result.regression = result.median < result.baseline_median / factor
        self.results[result.name] = result
        return result

    def check(self, result: Result) -> None:
        """Falla la prueba si hay regresión o falta la base (salvo al actualizarla o si el banco
        es informativo)."""
        if self.update or result.informative:
            return
        if result.baseline_median is None:
            pytest.fail(
                f"el banco {result.name} no tiene línea base en {BASELINE.name}: mídela y "
                f"guárdala con {UPDATE_VARIABLE}=1",
                pytrace=False,
            )
        if result.regression:
            relation = "más lenta" if result.kind == "latency" else "más baja"
            margin = round((result.regression_factor - 1) * 100)
            pytest.fail(
                f"regresión en {result.name}: mediana {result.median:.3f} {result.unit},"
                f" {relation} que la base {result.baseline_median:.3f} {result.unit} más allá"
                f" del {margin} % (razón {result.median_ratio:.2f}, factor"
                f" {result.regression_factor})",
                pytrace=False,
            )

    def document(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),  # noqa: TID251
            "environment": _environment(),
            "rounds": ROUNDS,
            "regression_factor": REGRESSION_FACTOR,
            "gob_regression_factor": GOB_REGRESSION_FACTOR,
            "baseline": {
                "path": str(BASELINE.relative_to(BACKEND)),
                "measured_at": self.baseline.get("measured_at"),
                "environment": self.baseline.get("environment"),
            },
            "benchmarks": {name: asdict(result) for name, result in sorted(self.results.items())},
        }

    def baseline_document(self) -> dict[str, Any]:
        """La base con lo medido en esta sesión; cada entrada medida lleva su fecha y su equipo
        (``runner``: la base mezcla entradas de U-02 medidas en el equipo del dueño con las de
        U-03 medidas en el runner de ``nightly``)."""
        benchmarks = dict(self.baseline.get("benchmarks", {}))
        measured_at = datetime.now(UTC).date().isoformat()  # noqa: TID251
        runner = _environment()["runner"]
        for name, result in self.results.items():
            entry: dict[str, Any] = {
                "label": result.label,
                "kind": result.kind,
                "unit": result.unit,
                "median": round(result.median, 3),
                "p95": None if result.p95 is None else round(result.p95, 3),
                "objective": result.objective,
            }
            if result.regression_factor != REGRESSION_FACTOR:
                entry["regression_factor"] = result.regression_factor
            if result.informative:
                entry["informative"] = True
            entry["measured_at"] = measured_at
            entry["runner"] = runner
            benchmarks[name] = entry
        return {
            "description": (
                "Línea base de los bancos de NFR-NUC-01 y 02 (TASK-142) y de NFR-GOB-01 a 10"
                f" (TASK-233). La regenera {UPDATE_VARIABLE}=1 y conserva lo que no se midió."
                " Una mediana peor que la de su entrada más allá de su regression_factor (1,5"
                " si no lo lleva; 1,2 en U-03) falla nightly."
            ),
            "measured_at": datetime.now(UTC).date().isoformat(),  # noqa: TID251
            "environment": _environment(),
            "benchmarks": dict(sorted(benchmarks.items())),
        }


def _environment() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git del equipo
            capture_output=True,
            text=True,
            check=False,
            cwd=BACKEND,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return {
        "commit": commit or None,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "release": platform.release(),
        "cpus": os.cpu_count(),
        "runner": os.environ.get("RUNNER_NAME") or os.environ.get("GITHUB_RUN_ID") or "local",
    }


_REPORT: Report | None = None


@pytest.fixture(scope="session")
def benchmark_report() -> Iterator[Report]:
    """El informe de la sesión; al terminar se escribe (y, si se pide, la base)."""
    global _REPORT
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    report = Report(baseline, update=os.environ.get(UPDATE_VARIABLE) == "1")
    _REPORT = report
    yield report
    path = Path(os.environ.get(REPORT_VARIABLE) or DEFAULT_REPORT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.document(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if report.update and report.results:
        BASELINE.write_text(
            json.dumps(report.baseline_document(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


Measure = Callable[..., Result]


@pytest.fixture
def measure(benchmark: Any, benchmark_report: Report) -> Measure:
    """``measure(name, label, target, setup=None, objective_ms=None, rounds=100,
    regression_factor=1.5)``.

    ``target`` es la operación (síncrona: envuelve la corrutina en el bucle del entorno);
    ``setup`` prepara cada ronda fuera de la medida. Los bancos de U-03 pasan
    ``regression_factor=GOB_REGRESSION_FACTOR``; ``informative=True`` publica sin comparar.
    Devuelve el ``Result`` ya comparado; la prueba falla si hay regresión.
    """

    def run(
        name: str,
        label: str,
        target: Callable[[], object],
        *,
        setup: Callable[[], object] | None = None,
        objective_ms: float | None = None,
        rounds: int = ROUNDS,
        details: dict[str, Any] | None = None,
        regression_factor: float = REGRESSION_FACTOR,
        informative: bool = False,
    ) -> Result:
        benchmark.group = name
        benchmark.pedantic(
            target,
            setup=None if setup is None else lambda: (setup(), None)[1],
            rounds=rounds,
            warmup_rounds=WARMUP_ROUNDS,
            iterations=1,
        )
        if benchmark.stats is None:
            pytest.fail("pytest-benchmark está desactivado: los bancos necesitan medir", False)
        times = [seconds * 1000 for seconds in benchmark.stats.stats.data]
        result = benchmark_report.add(
            Result(
                name=name,
                label=label,
                kind="latency",
                unit="ms",
                median=median(times),
                p95=percentile(times, 0.95),
                p99=percentile(times, 0.99),
                samples=len(times),
                objective=objective_ms,
                objective_met=None
                if objective_ms is None
                else percentile(times, 0.95) <= objective_ms,
                regression_factor=regression_factor,
                informative=informative,
                details=dict(details or {}),
            )
        )
        benchmark.extra_info.update(
            {
                "median_ms": result.median,
                "p95_ms": result.p95,
                "p99_ms": result.p99,
                "objective_ms": objective_ms,
                "regression_factor": result.regression_factor,
            }
        )
        benchmark_report.check(result)
        return result

    return run


@pytest.fixture(scope="package")
def _gob_world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    """La aplicación completa de U-03 (``tests/gob_platform_support.py``), una vez por paquete y
    solo si algún banco de U-03 la pide; cada banco crea sus propias organizaciones."""
    with gob_platform(postgres_endpoint, localstack_endpoint, "bench_gob") as world:
        yield world


@pytest.fixture
def gob(_gob_world: GobPlatform) -> GobPlatform:
    """El mundo de U-03, con la hora simulada en la de la base al empezar cada banco."""
    _gob_world.resync()
    return _gob_world


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Tabla del informe: mediana, p95, p99, objetivo y razón frente a la base con su factor."""
    if _REPORT is None or not _REPORT.results:
        return
    terminalreporter.section("bancos de NFR-NUC-01 y 02 y de NFR-GOB-01 a 10 (informe)")
    for result in sorted(_REPORT.results.values(), key=lambda item: item.name):
        p95 = "" if result.p95 is None else f" p95 {result.p95:,.1f}"
        p95 += "" if result.p99 is None else f" p99 {result.p99:,.1f}"
        objective = ""
        if result.objective is not None:
            verdict = "cumple" if result.objective_met else "NO cumple"
            objective = f" | objetivo {result.objective:,.0f} {result.unit}: {verdict}"
        ratio = ""
        if result.median_ratio is not None:
            ratio = f" | razón base {result.median_ratio:.2f} (factor {result.regression_factor})"
        terminalreporter.write_line(
            f"{result.name}: mediana {result.median:,.1f}{p95} {result.unit}{objective}{ratio}"
        )
    path = os.environ.get(REPORT_VARIABLE) or DEFAULT_REPORT
    terminalreporter.write_line(f"informe: {path}")
