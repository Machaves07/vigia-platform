"""La integración de ``ci.yml`` en 4 grupos por tiempos medidos (2026-10-19).

Antes iba en dos mitades por directorio (retro 23 C, 2026-10-07): 32 y 37 min, el camino crítico
del CI. Ahora es una matriz de 4 grupos de pytest-split, como las pruebas sin integración:

- **Matriz** (``backend-integracion-grupos``, «backend (pruebas de integración, grupo N de 4)»):
  ``-m integration --ignore=tests/load`` sobre toda la suite (sin rutas), con
  ``--splits=4 --group="$GRUPO" --durations-path=.test_durations_integracion``, cobertura y un
  artefacto ``cobertura-integracion-N`` por grupo. ``tests/load`` corre en «backend (carga ci)».
- **Agregadores**: «backend (pruebas de integración)» y «backend (pruebas de integración, segunda
  mitad)» conservan su nombre porque la sesión de control los exige; dependen de la matriz, corren
  siempre (``always()``: un check omitido cuenta como superado) y fallan si no terminó en verde.
- ``cobertura`` espera a la matriz y a los 4 grupos sin integración.
- Perfil de Hypothesis: los 8 grupos usan ``pr`` en los pull_request y ``ci`` en main; la carga
  sigue con ``ci``. El perfil ``pr`` existe en ``tests/conftest.py`` con 50 ejemplos.

Que la unión de los 4 grupos sea la colección completa lo comprueba ``tools/check_test_split.py
--collect`` en «backend (lint y tipos)». Aquí se lee ``ci.yml`` y se exige que cada directorio de
``tests/`` con pruebas ``integration`` corra exactamente una vez. Cada regla se prueba también en
negativo con un flujo sintético que la incumple.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import settings

from tests.conftest import (
    CI_FIXED_SEED,
    CI_PROFILE,
    CI_PROFILES,
    PR_PROFILE,
    SESSION_SEED_ID,
    _seeds_for_profile,
)

REPOSITORY = Path(__file__).resolve().parents[3]
CI = REPOSITORY / ".github" / "workflows" / "ci.yml"
TESTS = REPOSITORY / "backend" / "tests"

MATRIX = "backend-integracion-grupos"
MATRIX_NAME = "backend (pruebas de integración, grupo ${{ matrix.grupo }} de 4)"
UNIT_MATRIX = "backend-pruebas"
AGGREGATORS = {
    "backend-integracion": "backend (pruebas de integración)",
    "backend-integracion-2": "backend (pruebas de integración, segunda mitad)",
}
"""Checks que exige la sesión de control por nombre: no se renombran."""
LOAD = "backend-carga"
LOAD_DIRECTORY = "tests/load"
DURATIONS = ".test_durations_integracion"
PROFILE_EXPRESSION = "${{ github.event_name == 'pull_request' && 'pr' || 'ci' }}"
GROUPS = [1, 2, 3, 4]

_INTEGRATION_MARK = re.compile(r"mark\.integration\b")
_OPTIONS_WITH_VALUE = frozenset({"-m", "-p", "-k", "--ignore", "--deselect"})


@dataclass
class PytestCall:
    markers: list[str] = field(default_factory=list)
    ignores: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)


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
            elif argument.startswith("--"):
                name, _, value = argument.partition("=")
                call.options[name] = value
            elif not argument.startswith("-"):
                call.paths.append(_normalize(argument))
        return call
    return None


def _job_call(job: dict[Any, Any]) -> tuple[PytestCall | None, dict[Any, Any]]:
    calls = [
        (call, step)
        for step in job.get("steps") or []
        if "run" in step and (call := _pytest_call(str(step["run"]))) is not None
    ]
    if len(calls) != 1:
        return None, {}
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


def _profile_problems(job_id: str, call: PytestCall, step: dict[Any, Any]) -> list[str]:
    env = step.get("env") or {}
    problems = []
    if call.options.get("--hypothesis-profile") != "$PERFIL":
        problems.append(f'{job_id} debe pasar --hypothesis-profile="$PERFIL"')
    if str(env.get("PERFIL", "")) != PROFILE_EXPRESSION:
        problems.append(f"{job_id}: PERFIL debe ser pr en los pull_request y ci en main")
    return problems


def _matrix_problems(jobs: dict[Any, Any]) -> list[str]:
    matrix = jobs[MATRIX]
    problems: list[str] = []
    if matrix.get("name") != MATRIX_NAME:
        problems.append(f"{MATRIX} debe llamarse «{MATRIX_NAME}»")
    strategy = matrix.get("strategy") or {}
    if (strategy.get("matrix") or {}).get("grupo") != GROUPS:
        problems.append(f"{MATRIX}: la matriz debe numerar los grupos {GROUPS}")
    if strategy.get("fail-fast") is not False:
        problems.append(f"{MATRIX}: fail-fast: false")
    call, step = _job_call(matrix)
    if call is None:
        return [*problems, f"{MATRIX} debe tener exactamente una orden pytest"]
    if call.markers != ["integration"]:
        problems.append(f"{MATRIX} debe correr -m integration")
    if call.ignores != {LOAD_DIRECTORY}:
        problems.append(f"{MATRIX} solo ignora {LOAD_DIRECTORY}: {sorted(call.ignores)}")
    if call.paths:
        problems.append(f"{MATRIX} corre toda la suite: sin rutas ({call.paths})")
    expected = {
        "--splits": "4",
        "--group": "$GRUPO",
        "--splitting-algorithm": "least_duration",
        "--durations-path": DURATIONS,
    }
    for option, value in expected.items():
        if call.options.get(option) != value:
            problems.append(f"{MATRIX} debe pasar {option}={value}")
    if str((step.get("env") or {}).get("GRUPO", "")) != "${{ matrix.grupo }}":
        problems.append(f"{MATRIX}: el paso de pytest necesita env GRUPO de la matriz")
    if "coverage run -m pytest" not in str(step.get("run")):
        problems.append(f"{MATRIX} debe medir cobertura")
    problems += _profile_problems(MATRIX, call, step)
    artifacts = [
        str((s.get("with") or {}).get("name", ""))
        for s in matrix.get("steps") or []
        if str(s.get("uses", "")).startswith("actions/upload-artifact")
    ]
    if "cobertura-integracion-${{ matrix.grupo }}" not in artifacts:
        problems.append(f"{MATRIX}: cada grupo sube su artefacto cobertura-integracion-N")
    return problems


def _aggregator_problems(jobs: dict[Any, Any]) -> list[str]:
    problems: list[str] = []
    for job_id, name in AGGREGATORS.items():
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            problems.append(f"falta el agregador {job_id} («{name}»)")
            continue
        if job.get("name") != name:
            problems.append(f"{job_id} debe llamarse «{name}»")
        if MATRIX not in (job.get("needs") or []):
            problems.append(f"{job_id} debe depender de {MATRIX}")
        if not str(job.get("if", "")).startswith("always()"):
            problems.append(f"{job_id} debe correr siempre (always()): un check omitido pasa")
        steps = job.get("steps") or []
        results = "".join(str(v) for s in steps for v in (s.get("env") or {}).values())
        runs = "\n".join(str(s.get("run") or "") for s in steps)
        failing = re.search(r'test "\$\w+" = success', runs)
        if f"needs.{MATRIX}.result" not in results or not failing:
            problems.append(f"{job_id} debe fallar si {MATRIX} no terminó en verde")
        if any(_pytest_call(str(s.get("run") or "")) for s in steps):
            problems.append(f"{job_id} solo agrega: no corre pytest")
    return problems


def split_problems(text: str, tests_root: Path) -> list[str]:
    """Lo que rompe el reparto de la integración en ``text`` (un ``ci.yml``)."""
    jobs = (yaml.safe_load(text) or {}).get("jobs") or {}
    if MATRIX not in jobs:
        return [f"falta el trabajo {MATRIX}"]
    problems = _matrix_problems(jobs) + _aggregator_problems(jobs)
    call, _ = _job_call(jobs[MATRIX])
    ignores = call.ignores if call is not None else set()

    load_job = jobs.get(LOAD) or {}
    load_runs = "\n".join(str(step.get("run") or "") for step in load_job.get("steps") or [])
    for directory in sorted(integration_directories(tests_root)):
        runs = int(directory not in ignores) + int(
            directory == LOAD_DIRECTORY and f"{LOAD_DIRECTORY}/" in load_runs
        )
        if runs == 0:
            problems.append(f"{directory} no corre en ningún trabajo")
        elif runs > 1:
            problems.append(f"{directory} corre {runs} veces")

    coverage: dict[Any, Any] = next(
        (job for job in jobs.values() if str(job.get("name", "")).startswith("cobertura")), {}
    )
    if not {MATRIX, UNIT_MATRIX} <= set(coverage.get("needs") or []):
        problems.append(f"cobertura debe esperar a {MATRIX} y a {UNIT_MATRIX}")
    return problems


# --- El ci.yml del repositorio ------------------------------------------------------------------


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    return jobs


def test_the_integration_groups_of_ci_cover_every_directory_exactly_once() -> None:
    assert split_problems(CI.read_text(encoding="utf-8"), TESTS) == []


def test_each_integration_group_has_a_bounded_timeout() -> None:
    # 45 min (dueño, 19-10): los grupos tardan 19-23 min con el perfil pr; en main corre ci, más lento.
    assert _jobs()[MATRIX]["timeout-minutes"] <= 45


def test_the_integration_groups_read_measured_durations_and_do_not_store_them() -> None:
    """Los tiempos están en el repositorio; guardarlos en cada PR los reescribiría sin uso."""
    call, _ = _job_call(_jobs()[MATRIX])
    assert call is not None
    assert "--store-durations" not in call.options
    durations = json.loads((TESTS.parent / DURATIONS).read_text(encoding="utf-8"))
    assert len(durations) > 1000
    assert not any(test.startswith(f"{LOAD_DIRECTORY}/") for test in durations)


def test_the_unit_groups_use_the_pr_profile_in_pull_requests() -> None:
    call, step = _job_call(_jobs()[UNIT_MATRIX])
    assert call is not None
    assert _profile_problems(UNIT_MATRIX, call, step) == []


def test_the_load_job_keeps_the_ci_profile() -> None:
    """``tests/load`` lee su propio perfil de carga: su orden sigue con ``ci`` en PR y en main."""
    call, _ = _job_call(_jobs()[LOAD])
    assert call is not None
    assert call.options.get("--hypothesis-profile") == "ci"


def test_the_repository_has_integration_tests_in_several_directories() -> None:
    """La detección por marca funciona en el árbol real (si no, el reparto no comprobaría nada)."""
    found = integration_directories(TESTS)
    assert {"tests/integration", "tests/properties", LOAD_DIRECTORY} <= found


# --- El perfil pr --------------------------------------------------------------------------------


def test_the_pr_profile_has_50_examples_and_the_rest_of_ci() -> None:
    pr, ci = settings.get_profile(PR_PROFILE), settings.get_profile(CI_PROFILE)
    assert pr.max_examples == 50
    assert ci.max_examples == 200
    assert (pr.deadline, pr.print_blob) == (ci.deadline, ci.print_blob)
    assert {CI_PROFILE, PR_PROFILE} == CI_PROFILES


@pytest.mark.parametrize("profile", sorted(CI_PROFILES))
def test_the_ci_profiles_use_the_fixed_and_the_session_seed(profile: str) -> None:
    """Con ``pr`` las propiedades corren con las dos semillas, como con ``ci``: mismos
    identificadores (``seed=20260929`` y ``seed=sesion``), mismos grupos y tiempos."""
    previous = settings.get_current_profile_name()
    settings.load_profile(profile)
    try:
        seeds = _seeds_for_profile()
    finally:
        settings.load_profile(previous)
    assert len(seeds) == 2
    assert seeds[0] == CI_FIXED_SEED
    assert SESSION_SEED_ID == "seed=sesion"


# --- Negativos con flujos sintéticos -------------------------------------------------------------

_MATRIX_COMMAND = (
    'uv run --frozen coverage run -m pytest -q -p no:cacheprovider --hypothesis-profile="$PERFIL" '
    '-m integration --ignore=tests/load --splits=4 --group="$GRUPO" '
    f"--splitting-algorithm=least_duration --durations-path={DURATIONS}"
)
_AGGREGATOR_RUN = 'test "$GRUPOS" = success'


def _workflow(
    command: str = _MATRIX_COMMAND,
    name: str = MATRIX_NAME,
    groups: str = "[1, 2, 3, 4]",
    fail_fast: str = "false",
    profile: str = PROFILE_EXPRESSION,
    artifact: str = "cobertura-integracion-${{ matrix.grupo }}",
    aggregator_needs: str = f"[{MATRIX}]",
    aggregator_if: str = "always() && github.event_name == 'push'",
    aggregator_run: str = _AGGREGATOR_RUN,
    second_name: str = AGGREGATORS["backend-integracion-2"],
    needs: str = f"[{UNIT_MATRIX}, {MATRIX}]",
    load: str = "uv run --frozen pytest -q --hypothesis-profile=ci -m integration tests/load/",
) -> str:
    def aggregator(job_id: str, job_name: str) -> str:
        return (
            f"  {job_id}:\n"
            f"    name: {job_name}\n"
            f"    needs: {aggregator_needs}\n"
            f'    if: "{aggregator_if}"\n'
            "    steps:\n"
            "      - env:\n"
            f"          GRUPOS: ${{{{ needs.{MATRIX}.result }}}}\n"
            f"        run: {yaml.safe_dump(aggregator_run).splitlines()[0]}\n"
        )

    return (
        "jobs:\n"
        f"  {MATRIX}:\n"
        f'    name: "{name}"\n'
        "    strategy:\n"
        f"      fail-fast: {fail_fast}\n"
        f"      matrix: {{grupo: {groups}}}\n"
        "    steps:\n"
        "      - env:\n"
        '          GRUPO: "${{ matrix.grupo }}"\n'
        f'          PERFIL: "{profile}"\n'
        f"        run: {yaml.safe_dump(command, width=1000).splitlines()[0]}\n"
        "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
        f'        with: {{name: "{artifact}"}}\n'
        + aggregator("backend-integracion", AGGREGATORS["backend-integracion"])
        + aggregator("backend-integracion-2", second_name)
        + f"  {LOAD}:\n    name: backend (carga ci)\n    steps:\n      - run: {load}\n"
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
            {"command": _MATRIX_COMMAND.replace("--ignore=tests/load ", "")},
            "tests/load corre 2 veces",
        ),
        (
            {"command": f"{_MATRIX_COMMAND} --ignore=tests/abuse"},
            "tests/abuse no corre en ningún trabajo",
        ),
        (
            {"command": f"{_MATRIX_COMMAND} --ignore=tests/abuse"},
            f"{MATRIX} solo ignora tests/load: ['tests/abuse', 'tests/load']",
        ),
        (
            {"command": f"{_MATRIX_COMMAND} tests/properties"},
            f"{MATRIX} corre toda la suite: sin rutas (['tests/properties'])",
        ),
        (
            {"command": _MATRIX_COMMAND.replace("-m integration", '-m "not integration"')},
            f"{MATRIX} debe correr -m integration",
        ),
        (
            {"command": _MATRIX_COMMAND.replace("--splits=4", "--splits=2")},
            f"{MATRIX} debe pasar --splits=4",
        ),
        (
            {"command": _MATRIX_COMMAND.replace('--group="$GRUPO"', "--group=1")},
            f"{MATRIX} debe pasar --group=$GRUPO",
        ),
        (
            {"command": _MATRIX_COMMAND.replace(DURATIONS, ".test_durations")},
            f"{MATRIX} debe pasar --durations-path={DURATIONS}",
        ),
        (
            {"command": _MATRIX_COMMAND.replace("coverage run -m pytest", "pytest")},
            f"{MATRIX} debe medir cobertura",
        ),
        (
            {"command": _MATRIX_COMMAND.replace('"$PERFIL"', "ci")},
            f'{MATRIX} debe pasar --hypothesis-profile="$PERFIL"',
        ),
        (
            {"profile": "ci"},
            f"{MATRIX}: PERFIL debe ser pr en los pull_request y ci en main",
        ),
        (
            {"name": "backend (pruebas de integración)"},
            f"{MATRIX} debe llamarse «{MATRIX_NAME}»",
        ),
        (
            {"groups": "[1, 2, 3]"},
            f"{MATRIX}: la matriz debe numerar los grupos {GROUPS}",
        ),
        (
            {"fail_fast": "true"},
            f"{MATRIX}: fail-fast: false",
        ),
        (
            {"artifact": "cobertura-integracion"},
            f"{MATRIX}: cada grupo sube su artefacto cobertura-integracion-N",
        ),
        (
            {"second_name": "backend (pruebas de integración, grupo 2)"},
            f"backend-integracion-2 debe llamarse «{AGGREGATORS['backend-integracion-2']}»",
        ),
        (
            {"aggregator_needs": f"[{UNIT_MATRIX}]"},
            f"backend-integracion debe depender de {MATRIX}",
        ),
        (
            {"aggregator_if": "github.event_name == 'push'"},
            "backend-integracion debe correr siempre (always()): un check omitido pasa",
        ),
        (
            {"aggregator_run": 'echo "$GRUPOS"'},
            f"backend-integracion-2 debe fallar si {MATRIX} no terminó en verde",
        ),
        (
            {"aggregator_run": "uv run pytest -q -m integration"},
            "backend-integracion solo agrega: no corre pytest",
        ),
        (
            {"needs": f"[{UNIT_MATRIX}]"},
            f"cobertura debe esperar a {MATRIX} y a {UNIT_MATRIX}",
        ),
        (
            {"load": "echo sin carga"},
            "tests/load no corre en ningún trabajo",
        ),
    ],
)
def test_a_broken_split_fails(tests_root: Path, arguments: dict[str, Any], expected: str) -> None:
    assert expected in split_problems(_workflow(**arguments), tests_root)


def test_a_missing_matrix_or_aggregator_fails(tests_root: Path) -> None:
    assert split_problems("jobs: {}\n", tests_root) == [f"falta el trabajo {MATRIX}"]
    text = _workflow().replace("  backend-integracion-2:", "  otro-trabajo:")
    assert "falta el agregador backend-integracion-2 («backend (pruebas de integración, " in (
        "\n".join(split_problems(text, tests_root))
    )


def test_a_new_directory_falls_in_the_groups(tests_root: Path) -> None:
    (tests_root / "nuevo").mkdir()
    (tests_root / "nuevo" / "test_nuevo.py").write_text(
        "pytestmark = pytest.mark.integration\n", encoding="utf-8"
    )
    assert split_problems(_workflow(), tests_root) == []
