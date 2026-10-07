"""La integración de ``ci.yml`` en dos mitades por directorio (retro 23 C, 2026-10-07).

La integración tardaba 60-75 min en un solo trabajo. Ahora va en dos trabajos paralelos:

- **Segunda mitad** («backend (pruebas de integración, segunda mitad)»): la lista explícita de
  directorios de primer nivel de ``tests/`` que se le pasan a pytest como rutas.
- **Primera mitad** («backend (pruebas de integración)», el check obligatorio de siempre): todo lo
  demás, sin rutas (``testpaths``) y con un ``--ignore`` por cada directorio de la segunda.

Así un directorio nuevo cae siempre en la primera mitad y nada queda sin correr. Esta prueba lee
``ci.yml`` y exige que cada directorio de ``tests/`` con pruebas ``integration`` corra exactamente
una vez: en una de las dos mitades o, ``tests/load``, en «backend (carga ci)». Además, las dos
mitades miden cobertura y suben artefactos distintos que ``cobertura`` combina.

Cada regla se prueba también en negativo con un flujo sintético que la incumple.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[3]
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"
TESTS = REPOSITORY / "backend" / "tests"

FIRST_HALF = "backend-integracion"
SECOND_HALF = "backend-integracion-2"
LOAD = "backend-carga"
FIRST_HALF_NAME = "backend (pruebas de integración)"
"""Check obligatorio de la rama y de la sesión de control: no se renombra."""
SECOND_HALF_NAME = "backend (pruebas de integración, segunda mitad)"
LOAD_DIRECTORY = "tests/load"

_INTEGRATION_MARK = re.compile(r"mark\.integration\b")
_OPTIONS_WITH_VALUE = frozenset({"-m", "-p", "-k", "--ignore", "--deselect"})


@dataclass
class PytestCall:
    markers: list[str] = field(default_factory=list)
    ignores: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)


def _normalize(path: str) -> str:
    return path.rstrip("/")


def _pytest_call(run: str) -> PytestCall | None:
    """Argumentos de la orden ``pytest`` de un ``run:`` (o ``None`` si no la hay)."""
    for line in run.splitlines():
        tokens = shlex.split(line)
        if "pytest" not in tokens:
            continue
        call = PytestCall()
        arguments = iter(tokens[tokens.index("pytest") + 1 :])
        for argument in arguments:
            if argument in _OPTIONS_WITH_VALUE:
                value = next(arguments, "")
                if argument == "-m":
                    call.markers.append(value)
                elif argument == "--ignore":
                    call.ignores.add(_normalize(value))
            elif argument.startswith("--ignore="):
                call.ignores.add(_normalize(argument.split("=", 1)[1]))
            elif not argument.startswith("-"):
                call.paths.append(_normalize(argument))
        return call
    return None


def _job_call(job: dict[Any, Any]) -> tuple[PytestCall | None, str]:
    calls = [
        (call, str(step["run"]))
        for step in job.get("steps") or []
        if "run" in step and (call := _pytest_call(str(step["run"]))) is not None
    ]
    if len(calls) != 1:
        return None, ""
    return calls[0]


def integration_directories(tests_root: Path) -> set[str]:
    """Directorios de primer nivel de ``tests/`` con al menos una prueba ``integration``."""
    found = set()
    for directory in sorted(path for path in tests_root.iterdir() if path.is_dir()):
        if any(
            _INTEGRATION_MARK.search(module.read_text(encoding="utf-8"))
            for module in directory.rglob("*.py")
        ):
            found.add(f"tests/{directory.name}")
    return found


def split_problems(text: str, tests_root: Path) -> list[str]:
    """Lo que rompe el reparto de la integración en ``text`` (un ``ci.yml``)."""
    jobs = (yaml.safe_load(text) or {}).get("jobs") or {}
    problems: list[str] = []
    for job_id, name in ((FIRST_HALF, FIRST_HALF_NAME), (SECOND_HALF, SECOND_HALF_NAME)):
        if job_id not in jobs:
            return [f"falta el trabajo {job_id}"]
        if jobs[job_id].get("name") != name:
            problems.append(f"{job_id} debe llamarse «{name}»")
    first, first_run = _job_call(jobs[FIRST_HALF])
    second, second_run = _job_call(jobs[SECOND_HALF])
    if first is None or second is None:
        return [*problems, "cada mitad debe tener exactamente una orden pytest"]

    for job_id, call, run in ((FIRST_HALF, first, first_run), (SECOND_HALF, second, second_run)):
        if call.markers != ["integration"]:
            problems.append(f"{job_id} debe correr -m integration")
        if LOAD_DIRECTORY not in call.ignores:
            problems.append(f"{job_id} debe ignorar {LOAD_DIRECTORY}")
        if "coverage run -m pytest" not in run:
            problems.append(f"{job_id} debe medir cobertura")
    if first.paths:
        problems.append(f"la primera mitad corre todo lo demás: sin rutas ({first.paths})")
    if not second.paths:
        problems.append("la segunda mitad necesita su lista explícita de directorios")
    if len(set(second.paths)) != len(second.paths):
        problems.append("la segunda mitad repite un directorio")
    if second.ignores != {LOAD_DIRECTORY}:
        problems.append(f"la segunda mitad solo ignora {LOAD_DIRECTORY}")
    for path in second.paths:
        parts = path.split("/")
        if len(parts) != 2 or parts[0] != "tests" or not (tests_root / parts[1]).is_dir():
            problems.append(f"{path} no es un directorio de primer nivel de tests/")
        elif path == LOAD_DIRECTORY:
            problems.append(f"{LOAD_DIRECTORY} va en «backend (carga ci)», no en la integración")

    load_job = jobs.get(LOAD) or {}
    load_runs = "\n".join(str(step.get("run") or "") for step in load_job.get("steps") or [])
    for directory in sorted(integration_directories(tests_root) | set(second.paths)):
        runs = (
            int(directory not in first.ignores)
            + second.paths.count(directory)
            + int(directory == LOAD_DIRECTORY and f"{LOAD_DIRECTORY}/" in load_runs)
        )
        if runs == 0:
            problems.append(f"{directory} no corre en ningún trabajo")
        elif runs > 1:
            problems.append(f"{directory} corre {runs} veces")
    for ignored in sorted(first.ignores - {LOAD_DIRECTORY} - set(second.paths)):
        problems.append(f"la primera mitad ignora {ignored}, que la segunda no corre")

    artifacts = []
    for job_id in (FIRST_HALF, SECOND_HALF):
        artifacts += [
            str((step.get("with") or {}).get("name", ""))
            for step in jobs[job_id].get("steps") or []
            if str(step.get("uses", "")).startswith("actions/upload-artifact")
        ]
    if len(artifacts) != 2 or len(set(artifacts)) != 2:
        problems.append("cada mitad sube su propio artefacto de cobertura")
    elif not all(name.startswith("cobertura-") for name in artifacts):
        problems.append("los artefactos de cobertura se llaman cobertura-*")
    coverage = next(
        (job for job in jobs.values() if str(job.get("name", "")).startswith("cobertura")), {}
    )
    if not {FIRST_HALF, SECOND_HALF} <= set(coverage.get("needs") or []):
        problems.append("cobertura debe esperar a las dos mitades")
    return problems


# --- El ci.yml del repositorio ------------------------------------------------------------------


def test_the_integration_split_of_ci_covers_every_directory_exactly_once() -> None:
    assert split_problems(CI.read_text(encoding="utf-8"), TESTS) == []


def test_each_half_has_a_timeout_of_an_hour() -> None:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    for job_id in (FIRST_HALF, SECOND_HALF):
        assert jobs[job_id]["timeout-minutes"] <= 60, job_id


def test_the_repository_has_integration_tests_in_several_directories() -> None:
    """La detección por marca funciona en el árbol real (si no, el reparto no comprobaría nada)."""
    found = integration_directories(TESTS)
    assert {"tests/integration", "tests/properties", LOAD_DIRECTORY} <= found


# --- Negativos con flujos sintéticos -------------------------------------------------------------

_COMMAND = (
    "uv run --frozen coverage run -m pytest -q -p no:cacheprovider --hypothesis-profile=ci "
    "-m integration"
)


def _workflow(
    first: str = "--ignore=tests/load --ignore=tests/properties",
    second: str = "--ignore=tests/load tests/properties",
    second_name: str = SECOND_HALF_NAME,
    artifacts: tuple[str, str] = ("cobertura-integracion", "cobertura-integracion-2"),
    needs: str = f"[backend, {FIRST_HALF}, {SECOND_HALF}]",
    load: str = "uv run --frozen pytest -q -m integration tests/load/",
) -> str:
    def job(name: str, arguments: str, artifact: str) -> str:
        return (
            f"    name: {name}\n"
            "    steps:\n"
            f"      - run: {_COMMAND} {arguments}\n"
            "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
            f"        with: {{name: {artifact}}}\n"
        )

    return (
        "jobs:\n"
        f"  {FIRST_HALF}:\n{job(FIRST_HALF_NAME, first, artifacts[0])}"
        f"  {SECOND_HALF}:\n{job(second_name, second, artifacts[1])}"
        f"  {LOAD}:\n    name: backend (carga ci)\n    steps:\n      - run: {load}\n"
        f"  cobertura:\n    name: cobertura\n    needs: {needs}\n"
    )


@pytest.fixture
def tests_root(tmp_path: Path) -> Path:
    for directory in ("integration", "properties", "load", "abuse", "unit"):
        (tmp_path / directory).mkdir()
        mark = "" if directory == "unit" else "pytestmark = pytest.mark.integration\n"
        (tmp_path / directory / "test_algo.py").write_text(mark, encoding="utf-8")
    return tmp_path


def test_the_synthetic_split_passes(tests_root: Path) -> None:
    assert split_problems(_workflow(), tests_root) == []


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"first": "--ignore=tests/load"},
            "tests/properties corre 2 veces",
        ),
        (
            {"first": "--ignore=tests/load --ignore=tests/properties --ignore=tests/abuse"},
            "tests/abuse no corre en ningún trabajo",
        ),
        (
            {"second": "--ignore=tests/load tests/properties tests/abuse"},
            "tests/abuse corre 2 veces",
        ),
        (
            {"first": "--ignore=tests/properties"},
            f"{FIRST_HALF} debe ignorar tests/load",
        ),
        (
            {"second": "tests/properties"},
            f"{SECOND_HALF} debe ignorar tests/load",
        ),
        (
            {"first": "--ignore=tests/load --ignore=tests/properties tests/integration"},
            "la primera mitad corre todo lo demás: sin rutas (['tests/integration'])",
        ),
        (
            {"first": "--ignore=tests/load", "second": "--ignore=tests/load"},
            "la segunda mitad necesita su lista explícita de directorios",
        ),
        (
            {
                "first": "--ignore=tests/load --ignore=tests/fantasma",
                "second": "--ignore=tests/load tests/fantasma",
            },
            "tests/fantasma no es un directorio de primer nivel de tests/",
        ),
        (
            {"second": "--ignore=tests/load --ignore=tests/properties/gob tests/properties"},
            "la segunda mitad solo ignora tests/load",
        ),
        (
            {"second_name": "backend (pruebas de integración)"},
            f"{SECOND_HALF} debe llamarse «{SECOND_HALF_NAME}»",
        ),
        (
            {"artifacts": ("cobertura-integracion", "cobertura-integracion")},
            "cada mitad sube su propio artefacto de cobertura",
        ),
        (
            {"needs": f"[backend, {FIRST_HALF}]"},
            "cobertura debe esperar a las dos mitades",
        ),
        (
            {"load": "echo sin carga"},
            "tests/load no corre en ningún trabajo",
        ),
    ],
)
def test_a_broken_split_fails(tests_root: Path, arguments: dict[str, Any], expected: str) -> None:
    assert expected in split_problems(_workflow(**arguments), tests_root)


def test_a_new_directory_falls_in_the_first_half(tests_root: Path) -> None:
    (tests_root / "nuevo").mkdir()
    (tests_root / "nuevo" / "test_nuevo.py").write_text(
        "pytestmark = pytest.mark.integration\n", encoding="utf-8"
    )
    assert split_problems(_workflow(), tests_root) == []
