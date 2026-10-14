"""``tools/check_test_split.py``: los 4 grupos sin integración de ``ci.yml`` reparten la colección.

Criterio de aceptación de VIG-198: la unión de los grupos de pytest-split es la colección completa
de ``-m "not integration"``, sin pruebas fuera ni duplicadas, y la comprobación falla si un
archivo queda fuera. Aquí se fijan:

- la comprobación estática sobre el ``ci.yml`` real y, en negativo, sobre flujos sintéticos que
  sacan pruebas antes del reparto o numeran mal la matriz;
- la partición (``partition_problems``) con colecciones sintéticas;
- la comprobación dinámica de punta a punta sobre un proyecto pytest sintético en ``tmp_path``:
  en verde con pytest-split de verdad y en rojo cuando un ``conftest`` saca un archivo de un grupo.

La comprobación dinámica sobre la suite real la corre el trabajo «backend (lint y tipos)».
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import CI_FIXED_SEED
from tools.check_test_split import (
    BACKEND,
    CI,
    SplitError,
    check,
    main,
    parse_collection,
    partition_problems,
    read_job,
    static_problems,
)

_COMMAND = (
    'uv run --frozen coverage run -m pytest -q -p no:cacheprovider --hypothesis-profile=ci -m "not '
    'integration" --splits=4 --group="$GRUPO" --splitting-algorithm=least_duration '
    "--durations-path=.test_durations"
)


def _workflow(
    command: str = _COMMAND,
    groups: str = "[1, 2, 3, 4]",
    env: str = "GRUPO: ${{ matrix.grupo }}",
) -> str:
    return (
        "jobs:\n"
        "  backend-pruebas:\n"
        "    strategy:\n"
        f"      matrix: {{grupo: {groups}}}\n"
        "    steps:\n"
        "      - run: uv sync --frozen\n"
        "      - env:\n"
        f"          {env}\n"
        f"        run: {json.dumps(command)}\n"
    )


# --- El ci.yml del repositorio ------------------------------------------------------------------


def test_the_ci_workflow_splits_without_excluding_anything() -> None:
    job = read_job(CI.read_text(encoding="utf-8"))
    assert static_problems(job) == []
    assert job.groups == (1, 2, 3, 4)
    assert job.option("--splits") == "4"
    assert job.for_group(3)[job.arguments.index("--group=$GRUPO")] == "--group=3"


def test_the_main_entry_point_passes_on_the_repository_workflow(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    assert "sin huecos ni duplicados" in capsys.readouterr().out


# --- Negativos estáticos -------------------------------------------------------------------------


def test_the_synthetic_workflow_passes() -> None:
    assert static_problems(read_job(_workflow())) == []


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"groups": "[1, 2, 3]"},
            "la matriz debe numerar los grupos 1..4: [1, 2, 3]",
        ),
        (
            {"groups": "[1, 2, 3, 4, 5]"},
            "la matriz debe numerar los grupos 1..4: [1, 2, 3, 4, 5]",
        ),
        (
            {"groups": "[0, 1, 2, 3]"},
            "la matriz debe numerar los grupos 1..4: [0, 1, 2, 3]",
        ),
        (
            {"command": _COMMAND.replace("--splits=4", "--splits=5")},
            "la matriz debe numerar los grupos 1..5: [1, 2, 3, 4]",
        ),
        (
            {"command": _COMMAND.replace("--splits=4 ", "")},
            "la orden necesita --splits=N con N ≥ 2",
        ),
        (
            {"command": _COMMAND.replace("--splits=4", "--splits=1"), "groups": "[1]"},
            "la orden necesita --splits=N con N ≥ 2",
        ),
        (
            {"command": _COMMAND.replace('--group="$GRUPO"', "--group=1")},
            'la orden debe pasar --group="$GRUPO"',
        ),
        (
            {"command": _COMMAND.replace('"not integration"', "integration")},
            "la orden debe seleccionar exactamente -m \"not integration\": ['integration']",
        ),
        (
            {"command": _COMMAND.replace('-m "not integration" ', "")},
            'la orden debe seleccionar exactamente -m "not integration": []',
        ),
        (
            {"command": f"{_COMMAND} --ignore=tests/unit/test_algo.py"},
            "la orden saca pruebas antes del reparto: --ignore=tests/unit/test_algo.py",
        ),
        (
            {"command": f"{_COMMAND} --ignore-glob=tests/unit/*"},
            "la orden saca pruebas antes del reparto: --ignore-glob=tests/unit/*",
        ),
        (
            {"command": f"{_COMMAND} --deselect=tests/unit/test_algo.py::test_x"},
            "la orden saca pruebas antes del reparto: --deselect=tests/unit/test_algo.py::test_x",
        ),
        (
            {"command": f"{_COMMAND} -k 'not lento'"},
            "la orden saca pruebas antes del reparto: -k",
        ),
        (
            {"command": f"{_COMMAND} tests/unit"},
            "la orden corre todo testpaths, sin rutas: tests/unit",
        ),
    ],
)
def test_a_broken_split_fails(arguments: dict[str, str], expected: str) -> None:
    assert expected in static_problems(read_job(_workflow(**arguments)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("jobs: {}\n", "falta el trabajo backend-pruebas"),
        (_workflow(groups="'1-4'"), "strategy.matrix.grupo debe ser una lista de enteros"),
        (
            _workflow(groups="[true, 2, 3, 4]"),
            "strategy.matrix.grupo debe ser una lista de enteros",
        ),
        (_workflow(env="OTRO: 1"), "el paso de pytest debe tener env GRUPO"),
        (_workflow(command="echo sin pruebas"), "exactamente una orden pytest (0)"),
        ("jobs: [\n", "ci.yml no es YAML válido"),
    ],
)
def test_a_malformed_workflow_is_an_error(text: str, expected: str) -> None:
    with pytest.raises(SplitError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        read_job(text)


def test_main_reports_problems_and_errors(tmp_path: Path) -> None:
    broken = tmp_path / "roto.yml"
    broken.write_text(_workflow(groups="[1, 2, 3]"), encoding="utf-8")
    assert main(["--workflow", str(broken)]) == 1
    broken.write_text("jobs: {}\n", encoding="utf-8")
    assert main(["--workflow", str(broken)]) == 2
    assert main(["--workflow", str(tmp_path / "no-existe.yml")]) == 2


# --- Partición ---------------------------------------------------------------------------------

_FULL = ["tests/a.py::t1", "tests/a.py::t2", "tests/b.py::t1", "tests/c.py::t[x y]"]


def test_an_exact_partition_passes() -> None:
    groups = {1: ["tests/a.py::t1", "tests/c.py::t[x y]"], 2: ["tests/a.py::t2", "tests/b.py::t1"]}
    assert partition_problems(_FULL, groups) == []


def test_a_file_left_out_fails() -> None:
    groups = {1: ["tests/a.py::t1", "tests/c.py::t[x y]"], 2: ["tests/a.py::t2"]}
    assert partition_problems(_FULL, groups) == ["ningún grupo corre tests/b.py::t1"]


def test_a_test_in_two_groups_fails() -> None:
    groups = {1: _FULL[:3], 2: _FULL[2:]}
    assert partition_problems(_FULL, groups) == ["tests/b.py::t1 corre 2 veces"]


def test_a_test_outside_the_full_collection_fails() -> None:
    groups = {1: _FULL[:2], 2: [*_FULL[2:], "tests/d.py::t1"]}
    assert partition_problems(_FULL, groups) == [
        "un grupo corre tests/d.py::t1, que no está en la colección completa"
    ]


def test_an_empty_group_fails() -> None:
    assert partition_problems(_FULL, {1: _FULL, 2: []}) == ["el grupo 2 está vacío"]


def test_parse_collection_keeps_parametrized_ids_with_spaces() -> None:
    output = (
        "tests/a.py::t1\n"
        "tests/c.py::t[x y]\n"
        "\n"
        "2 tests collected in 0.01s\n"
        "tests/zz.py::no-es-una-prueba aviso posterior\n"
    )
    assert parse_collection(output) == ["tests/a.py::t1", "tests/c.py::t[x y]"]


# --- De punta a punta sobre un proyecto sintético -----------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\nmarkers =\n    integration: con contenedores\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "from hypothesis import settings\nsettings.register_profile('ci')\n", encoding="utf-8"
    )
    for name in ("a", "b", "c", "d", "e"):
        (tests / f"test_{name}.py").write_text(
            "".join(f"def test_{name}{n}():\n    pass\n" for n in range(3)), encoding="utf-8"
        )
    (tests / "test_integracion.py").write_text(
        "import pytest\n\n@pytest.mark.integration\ndef test_con_base():\n    pass\n",
        encoding="utf-8",
    )
    return tmp_path


def test_the_real_collection_of_each_group_is_a_partition(project: Path) -> None:
    assert check(_workflow(), collect=True, root=project) == []


def test_a_file_dropped_from_one_group_is_detected(project: Path) -> None:
    """Mutación: un ``conftest`` saca ``test_c.py`` del grupo que le toca; la recolección real de
    los grupos ya no cubre la colección completa y la comprobación lo dice prueba a prueba."""
    with (project / "tests" / "conftest.py").open("a", encoding="utf-8") as conftest:
        conftest.write(
            "import pytest\n\n"
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    if config.getoption('group', None):\n"
            "        items[:] = [i for i in items if 'test_c.py' not in i.nodeid]\n"
        )
    assert check(_workflow(), collect=True, root=project) == [
        f"ningún grupo corre tests/test_c.py::test_c{n}" for n in range(3)
    ]


def test_property_ids_do_not_depend_on_the_session_seed() -> None:
    """Dos procesos con semillas de sesión distintas recolectan los mismos identificadores
    (``seed=sesion``, no el valor): si no, cada grupo de pytest-split reparte distinto."""
    module = "tests/unit/test_verify_engine.py"
    first, second = (_collect_module(module, f"--hypothesis-seed={value}") for value in (111, 222))
    assert first == second
    assert any(test.endswith("[seed=sesion]") for test in first), first
    assert any(test.endswith(f"[seed={CI_FIXED_SEED}]") for test in first), first


def _collect_module(module: str, *options: str) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--hypothesis-profile=ci",
            *options,
            module,
            "--collect-only",
            "-q",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return parse_collection(result.stdout)


def test_a_collection_that_fails_is_an_error(project: Path) -> None:
    (project / "tests" / "test_roto.py").write_text("import modulo_que_no_existe\n")
    with pytest.raises(SplitError, match="la recolección terminó en"):
        check(_workflow(), collect=True, root=project)
