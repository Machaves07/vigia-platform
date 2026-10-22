"""``ci.yml`` ejecuta las suites de U-03 y U-04 en cada PR (TASK-234 y TASK-333).

Criterio de aceptación: la prueba falla si un ``--ignore``, un ``--deselect`` o un marcador deja
fuera de los trabajos «backend» de ``ci.yml`` alguna de las suites nuevas de U-03:
``tests/isolation``, ``tests/abuse``, ``tests/examples``, ``tests/properties/gob`` y ``laz``,
o el perfil
``ci`` de ``tests/load`` (marcado ``integration``).

La prueba lee el flujo real y simula la cobertura de cada suite por el conjunto de los trabajos
«backend». Como cada suite tiene pruebas sin integración y con integración (salvo ``tests/load``,
que solo es de integración), la suite tiene que quedar cubierta por **las dos** clases de marcador:
una orden que la seleccione sin ignorarla en ``-m "not integration"`` y otra en ``-m integration``
(o una sin marcador). Ignorarla en una sola de las clases deja su otra mitad sin correr y falla. Un
marcador que nombre una suite concreta para excluirla también falla.
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
    "tests/properties/laz",
)
"""Directorios que los trabajos «backend» deben ejecutar entre todos."""

LOAD_SUITE = "tests/load"
"""El perfil de carga ``ci`` de ``tests/load`` corre en su trabajo, marcado ``integration``."""

_SEPARATE_VALUE_OPTIONS = ("-p", "-m", "-k", "-o", "--ignore", "--deselect", "--ignore-glob")
"""Opciones de pytest cuyo valor viaja en el argumento siguiente (no con ``=``)."""

_SUITE_TOKENS = ("isolation", "abuse", "examples", "properties", "gob", "laz", "load", "nightly")


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
            target = excluded.split("::", 1)[0].rstrip("/")
            if (
                target == directory
                or directory.startswith(target + "/")
                or target.startswith(directory + "/")
            ):
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
        elif argument.startswith("--ignore-glob="):
            options["--ignore-glob"] = argument.split("=", 1)[1]
        elif argument in _SEPARATE_VALUE_OPTIONS and position + 1 < len(tokens):
            value = tokens[position + 1]
            position += 1
            if argument == "-m":
                marker = value
            elif argument == "-k":
                options["-k"] = value
            elif argument == "--ignore":
                ignores.append(value)
            elif argument == "--deselect":
                deselects.append(value)
            elif argument == "--ignore-glob":
                options["--ignore-glob"] = value
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


def _marker_classes(command: Command) -> set[str]:
    """Las clases de marcador que una orden ejecuta: ``not-integration`` e ``integration``."""
    if not command.marker:
        return {"not-integration", "integration"}
    if "not integration" in command.marker:
        return {"not-integration"}
    if "integration" in command.marker:
        return {"integration"}
    return set()


def coverage_problems(text: str) -> list[str]:
    """Lo que dejaría alguna suite de U-03 sin ejecutar en los trabajos «backend».

    Cada suite tiene pruebas sin integración y con integración (salvo ``tests/load``, que solo es
    de integración): la orden que la selecciona tiene que cubrir **las dos** clases, porque
    ignorarla en una sola deja su otra mitad sin correr (por ejemplo, ``--ignore=tests/properties/
    gob`` solo en la segunda mitad de la integración deja las PR-GOB de integración sin ejecutar).
    """
    commands = _backend_commands(text)
    problems: list[str] = []
    for suite in SUITES:
        classes = {
            kind
            for command in commands
            if command.selects(suite) and not command.excludes(suite)
            for kind in _marker_classes(command)
        }
        if "not-integration" not in classes:
            problems.append(f"{suite}: ningún trabajo «backend» corre sus pruebas sin integración")
        if "integration" not in classes:
            problems.append(f"{suite}: ningún trabajo «backend» corre sus pruebas con integración")
    load = [c for c in commands if c.selects(LOAD_SUITE) and not c.excludes(LOAD_SUITE)]
    if not any(c.integration and c.profile == "ci" for c in load):
        problems.append(
            f"{LOAD_SUITE}: ningún trabajo «backend» corre -m integration"
            " con --hypothesis-profile=ci"
        )
    for command in commands:
        if command.options.get("-k"):
            problems.append(f"un trabajo «backend» filtra con -k: {command.raw}")
        if command.options.get("--ignore-glob"):
            problems.append(f"un trabajo «backend» filtra con --ignore-glob: {command.raw}")
        for token in _SUITE_TOKENS:
            if token in command.marker:
                problems.append(f"un marcador nombra la suite {token!r}: {command.raw}")
    return problems


def test_ci_yml_runs_every_u03_suite_in_the_backend_jobs() -> None:
    assert coverage_problems(CI.read_text(encoding="utf-8")) == []


def test_the_checker_reads_the_backend_jobs_and_their_pytest_commands() -> None:
    commands = _backend_commands(CI.read_text(encoding="utf-8"))
    # Los tres trabajos «backend» con pytest: los grupos sin integración, los de integración y la
    # carga (los agregadores no corren pytest).
    assert len(commands) >= 3
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
  backend-integracion-2:
    name: backend (pruebas de integración, segunda mitad)
    runs-on: ubuntu-24.04
    steps:
      - run: {integracion2}
  backend-carga:
    name: backend (carga ci)
    runs-on: ubuntu-24.04
    steps:
      - run: {carga}
"""

_GOOD_BACKEND = "uv run pytest -q --hypothesis-profile=ci -m 'not integration'"
_GOOD_INTEGRACION = (
    "uv run pytest -q --hypothesis-profile=ci -m integration --ignore=tests/load"
    " --ignore=tests/properties"
)
_GOOD_INTEGRACION2 = (
    "uv run pytest -q --hypothesis-profile=ci -m integration --ignore=tests/load tests/properties"
)
_GOOD_CARGA = "uv run pytest -q --hypothesis-profile=ci -m integration tests/load"


def _workflow(
    backend: str = _GOOD_BACKEND,
    integracion: str = _GOOD_INTEGRACION,
    integracion2: str = _GOOD_INTEGRACION2,
    carga: str = _GOOD_CARGA,
) -> str:
    return _HEADER.format(
        backend=backend, integracion=integracion, integracion2=integracion2, carga=carga
    )


def test_a_healthy_synthetic_workflow_passes() -> None:
    assert coverage_problems(_workflow()) == []


def test_ignoring_a_suite_everywhere_fails() -> None:
    for suite in SUITES:
        workflow = _workflow(
            backend=f"{_GOOD_BACKEND} --ignore={suite}",
            integracion=f"{_GOOD_INTEGRACION} --ignore={suite}",
            integracion2=f"{_GOOD_INTEGRACION2} --ignore={suite}",
        )
        assert any(suite in problem for problem in coverage_problems(workflow)), suite


def test_ignoring_a_suite_in_only_one_marker_class_fails() -> None:
    # El escenario del revisor: ignorar tests/properties/gob solo en la segunda mitad de la
    # integración deja sus PR-GOB de integración (21, 26, 27, 30…) sin ejecutar.
    only_integration = _workflow(integracion2=f"{_GOOD_INTEGRACION2} --ignore=tests/properties/gob")
    assert any("tests/properties/gob" in problem for problem in coverage_problems(only_integration))
    # Y al revés: ignorar tests/isolation solo en el trabajo sin integración pierde su mitad sin
    # integración.
    only_without = _workflow(backend=f"{_GOOD_BACKEND} --ignore=tests/isolation")
    assert any("tests/isolation" in problem for problem in coverage_problems(only_without))


def test_deselecting_a_suite_everywhere_fails() -> None:
    workflow = _workflow(
        backend=f"{_GOOD_BACKEND} --deselect=tests/isolation",
        integracion=f"{_GOOD_INTEGRACION} --deselect=tests/isolation",
        integracion2=f"{_GOOD_INTEGRACION2} --deselect=tests/isolation",
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
        integracion2=(
            "uv run pytest -q --hypothesis-profile=ci --ignore=tests/load tests/properties"
        ),
        carga="uv run pytest -q --hypothesis-profile=ci tests/load",
    )
    assert any("integration" in problem for problem in coverage_problems(workflow))


def test_a_suite_ignored_by_every_job_that_selects_it_fails() -> None:
    workflow = _workflow(
        backend=f"{_GOOD_BACKEND} --ignore=tests/properties/gob",
        integracion=f"{_GOOD_INTEGRACION} --ignore=tests/properties/gob",
        integracion2=f"{_GOOD_INTEGRACION2} --ignore=tests/properties/gob",
    )
    assert any("tests/properties/gob" in problem for problem in coverage_problems(workflow))
