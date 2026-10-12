"""``ci.yml`` ejecuta las suites de U-03 en cada PR (NFR-GOB-60 a 62; TASK-234).

Criterio de aceptación: la prueba falla si un ``--ignore``, un ``--deselect`` o un marcador deja
fuera de los trabajos «backend» de ``ci.yml`` alguna de las suites nuevas de U-03:
``tests/isolation``, ``tests/abuse``, ``tests/examples``, ``tests/properties/gob`` o el perfil
``ci`` de ``tests/load`` (marcado ``integration``).

La prueba lee el flujo real y simula la cobertura de cada suite por el conjunto de los trabajos
«backend»: una suite está cubierta si algún trabajo la selecciona sin ignorarla ni deseleccionarla.
El reparto entre la mitad sin integración (``-m "not integration"``) y las mitades con integración
(``-m integration``) es lo que hace que, juntas, cubran cada directorio. Un marcador que nombre una
suite concreta para excluirla también falla.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"

SUITES = (
    "tests/isolation",
    "tests/abuse",
    "tests/examples",
    "tests/properties/gob",
)
"""Directorios que los trabajos «backend» deben ejecutar entre todos."""

LOAD_SUITE = "tests/load"
"""El perfil de carga ``ci`` de ``tests/load`` corre en su trabajo, marcado ``integration``."""

_SEPARATE_VALUE_OPTIONS = ("-p", "-m", "-k", "-o")
"""Opciones de pytest cuyo valor viaja en el argumento siguiente (no con ``=``)."""

_SUITE_TOKENS = ("isolation", "abuse", "examples", "properties", "gob", "load", "nightly")


@dataclass
class Command:
    """Una invocación de pytest de un trabajo «backend», con lo que selecciona y lo que excluye."""

    marker: str
    paths: tuple[str, ...]
    ignores: tuple[str, ...]
    deselects: tuple[str, ...]
    profile: str
    raw: str
    options: dict[str, str] = field(default_factory=dict)

    def selects(self, directory: str) -> bool:
        """La orden toca ``directory``: sin rutas selecciona la raíz; con rutas, una la contiene."""
        if not self.paths:
            return True
        return any(
            directory == path
            or directory.startswith(path.rstrip("/") + "/")
            or path.startswith(directory.rstrip("/") + "/")
            for path in self.paths
        )

    def excludes(self, directory: str) -> bool:
        """Un ``--ignore`` o ``--deselect`` deja fuera ``directory`` (o una parte)."""
        for excluded in (*self.ignores, *self.deselects):
            target = excluded.rstrip("/")
            if target == directory or directory.startswith(target + "/"):
                return True
        return False

    @property
    def integration(self) -> bool:
        """La orden corre las pruebas marcadas ``integration`` (no las excluye con ``not``)."""
        return "integration" in self.marker and "not integration" not in self.marker


def _pytest_commands(text: str) -> list[Command]:
    commands: list[Command] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "pytest" not in stripped or stripped.startswith("#"):
            continue
        index = stripped.find("pytest")
        after = stripped[index + len("pytest") :]
        try:
            tokens = shlex.split(after)
        except ValueError:  # pragma: no cover - línea sin comillas equilibradas
            continue
        commands.append(_parse(tokens, stripped))
    return commands


def _parse(tokens: list[str], raw: str) -> Command:
    marker = ""
    profile = ""
    ignores: list[str] = []
    deselects: list[str] = []
    paths: list[str] = []
    options: dict[str, str] = {}
    position = 0
    while position < len(tokens):
        argument = tokens[position]
        if argument.startswith("--ignore="):
            ignores.append(argument.split("=", 1)[1])
        elif argument.startswith("--deselect="):
            deselects.append(argument.split("=", 1)[1])
        elif argument.startswith("--hypothesis-profile="):
            profile = argument.split("=", 1)[1]
        elif argument in _SEPARATE_VALUE_OPTIONS and position + 1 < len(tokens):
            value = tokens[position + 1]
            position += 1
            if argument == "-m":
                marker = value
            elif argument == "-k":
                options["-k"] = value
        elif not argument.startswith("-"):
            paths.append(argument.rstrip("/"))
        position += 1
    return Command(
        marker=marker,
        paths=tuple(paths),
        ignores=tuple(ignores),
        deselects=tuple(deselects),
        profile=profile,
        raw=raw,
        options=options,
    )


def _backend_commands(text: str) -> list[Command]:
    """Las invocaciones de pytest de los trabajos cuyo ``name`` empieza por «backend»."""
    workflow = yaml.safe_load(text)
    commands: list[Command] = []
    for job in (workflow.get("jobs") or {}).values():
        if not str(job.get("name", "")).startswith("backend"):
            continue
        for step in job.get("steps") or []:
            run = str(step.get("run") or "")
            commands.extend(_pytest_commands(run))
    return commands


def coverage_problems(text: str) -> list[str]:
    """Lo que dejaría alguna suite de U-03 sin ejecutar en los trabajos «backend»."""
    commands = _backend_commands(text)
    problems: list[str] = []
    for suite in SUITES:
        if not any(c.selects(suite) and not c.excludes(suite) for c in commands):
            problems.append(f"{suite}: ningún trabajo «backend» la ejecuta sin ignorarla")
    markers = {c.marker for c in commands}
    if "not integration" not in markers:
        problems.append('falta el reparto sin integración (-m "not integration")')
    if "integration" not in markers:
        problems.append("falta el reparto con integración (-m integration)")
    load = [c for c in commands if c.selects(LOAD_SUITE) and not c.excludes(LOAD_SUITE)]
    if not any(c.integration and c.profile == "ci" for c in load):
        problems.append(
            f"{LOAD_SUITE}: ningún trabajo «backend» corre -m integration"
            " con --hypothesis-profile=ci"
        )
    for command in commands:
        if command.options.get("-k"):
            problems.append(f"un trabajo «backend» filtra con -k: {command.raw}")
        for token in _SUITE_TOKENS:
            if token in command.marker:
                problems.append(f"un marcador nombra la suite {token!r}: {command.raw}")
    return problems


def test_ci_yml_runs_every_u03_suite_in_the_backend_jobs() -> None:
    assert coverage_problems(CI.read_text(encoding="utf-8")) == []


def test_the_checker_reads_the_backend_jobs_and_their_pytest_commands() -> None:
    commands = _backend_commands(CI.read_text(encoding="utf-8"))
    # Los cuatro trabajos «backend»: sin integración, las dos mitades y la carga.
    assert len(commands) >= 4
    assert {c.marker for c in commands} >= {"not integration", "integration"}
    assert any(c.profile == "ci" for c in commands)
    assert any(c.selects(LOAD_SUITE) for c in commands)


# --- Negativos: cada forma de exclusión deja la comprobación en rojo ---------------------------

_HEADER = """\
name: ci
on: [push]
permissions: {{contents: read}}
jobs:
  backend:
    name: backend (lint, tipos y pruebas sin integración)
    runs-on: ubuntu-24.04
    steps:
      - run: {backend}
  backend-integracion:
    name: backend (pruebas de integración)
    runs-on: ubuntu-24.04
    steps:
      - run: {integracion}
  backend-carga:
    name: backend (carga ci)
    runs-on: ubuntu-24.04
    steps:
      - run: {carga}
"""

_GOOD_BACKEND = "uv run pytest -q --hypothesis-profile=ci -m 'not integration'"
_GOOD_INTEGRACION = "uv run pytest -q --hypothesis-profile=ci -m integration --ignore=tests/load"
_GOOD_CARGA = "uv run pytest -q --hypothesis-profile=ci -m integration tests/load"


def _workflow(
    backend: str = _GOOD_BACKEND, integracion: str = _GOOD_INTEGRACION, carga: str = _GOOD_CARGA
) -> str:
    return _HEADER.format(backend=backend, integracion=integracion, carga=carga)


def test_a_healthy_synthetic_workflow_passes() -> None:
    assert coverage_problems(_workflow()) == []


def test_ignoring_a_suite_everywhere_fails() -> None:
    for suite in SUITES:
        workflow = _workflow(
            backend=f"{_GOOD_BACKEND} --ignore={suite}",
            integracion=f"{_GOOD_INTEGRACION} --ignore={suite}",
            carga=f"{_GOOD_CARGA} --ignore={suite}",
        )
        assert any(suite in problem for problem in coverage_problems(workflow)), suite


def test_deselecting_a_suite_everywhere_fails() -> None:
    workflow = _workflow(
        backend=f"{_GOOD_BACKEND} --deselect=tests/isolation",
        integracion=f"{_GOOD_INTEGRACION} --deselect=tests/isolation",
    )
    assert any("tests/isolation" in problem for problem in coverage_problems(workflow))


def test_a_marker_that_names_a_suite_fails() -> None:
    workflow = _workflow(
        backend="uv run pytest -q --hypothesis-profile=ci -m 'not integration and not isolation'"
    )
    assert any("isolation" in problem for problem in coverage_problems(workflow))


def test_a_keyword_filter_fails() -> None:
    workflow = _workflow(backend=f"{_GOOD_BACKEND} -k 'not isolation'")
    assert any("-k" in problem for problem in coverage_problems(workflow))


def test_dropping_the_load_profile_fails() -> None:
    assert any(
        "tests/load" in problem
        for problem in coverage_problems(_workflow(carga="echo 'tests/load aún no existe'"))
    )
    without_profile = coverage_problems(
        _workflow(carga="uv run pytest -q -m integration tests/load")
    )
    assert any("--hypothesis-profile=ci" in problem for problem in without_profile)


def test_dropping_a_marker_class_fails() -> None:
    workflow = _workflow(
        integracion="uv run pytest -q --hypothesis-profile=ci --ignore=tests/load",
        carga="uv run pytest -q --hypothesis-profile=ci tests/load",
    )
    assert any("integration" in problem for problem in coverage_problems(workflow))


def test_a_suite_ignored_by_every_job_that_selects_it_fails() -> None:
    workflow = _workflow(
        backend=f"{_GOOD_BACKEND} --ignore=tests/properties/gob",
        integracion=f"{_GOOD_INTEGRACION} --ignore=tests/properties/gob",
    )
    assert any("tests/properties/gob" in problem for problem in coverage_problems(workflow))
