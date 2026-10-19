"""``tools/check_test_split.py``: los grupos de pytest-split de ``ci.yml`` y ``nightly.yml``
reparten su colección.

Criterios de aceptación de VIG-198 y VIG-199: la unión de los grupos de pytest-split es la
colección completa de cada trabajo repartido (``-m "not integration"`` y ``-m integration
--ignore=tests/load`` en ``ci.yml``; ``-m "not integration" tests/properties`` y ``-m integration
tests/properties`` en ``nightly.yml``), sin pruebas fuera ni duplicadas, y la comprobación falla si
un archivo queda fuera. Aquí se fijan:

- la comprobación estática sobre los flujos reales y, en negativo, sobre flujos sintéticos que
  sacan pruebas antes del reparto, nombran otras rutas o numeran mal la matriz;
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
    CI_INTEGRATION_SPEC,
    NIGHTLY,
    NIGHTLY_INTEGRATION_SPEC,
    NIGHTLY_SUITE_SPEC,
    SPECS,
    SplitError,
    SplitSpec,
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
_INTEGRATION_COMMAND = (
    'uv run --frozen coverage run -m pytest -q -p no:cacheprovider --hypothesis-profile="$PERFIL" '
    '-m integration --ignore=tests/load --splits=4 --group="$GRUPO" '
    "--splitting-algorithm=least_duration --durations-path=.test_durations_integracion"
)
_NIGHTLY_COMMAND = (
    "uv run --frozen pytest -q -p no:cacheprovider --hypothesis-profile=nightly -m integration "
    '--durations=15 --splits=5 --group="$GRUPO" --splitting-algorithm=least_duration '
    "--durations-path=.test_durations_nightly --store-durations --clean-durations tests/properties"
)


def _workflow(
    command: str = _COMMAND,
    groups: str = "[1, 2, 3, 4]",
    env: str = "GRUPO: ${{ matrix.grupo }}",
    job_id: str = "backend-pruebas",
) -> str:
    return (
        "jobs:\n"
        f"  {job_id}:\n"
        "    strategy:\n"
        f"      matrix: {{grupo: {groups}}}\n"
        "    steps:\n"
        "      - run: uv sync --frozen\n"
        "      - env:\n"
        f"          {env}\n"
        f"        run: {json.dumps(command)}\n"
    )


def _nightly_workflow(command: str = _NIGHTLY_COMMAND, groups: str = "[1, 2, 3, 4, 5]") -> str:
    return _workflow(command, groups, job_id=NIGHTLY_INTEGRATION_SPEC.job_id)


def _integration_workflow(command: str = _INTEGRATION_COMMAND) -> str:
    return _workflow(command, job_id=CI_INTEGRATION_SPEC.job_id)


# --- Los flujos del repositorio -----------------------------------------------------------------


def test_the_ci_workflow_splits_without_excluding_anything() -> None:
    job = read_job(CI.read_text(encoding="utf-8"))
    assert static_problems(job) == []
    assert job.groups == (1, 2, 3, 4)
    assert job.option("--splits") == "4"
    assert job.for_group(3)[job.arguments.index("--group=$GRUPO")] == "--group=3"


def test_the_ci_integration_groups_split_everything_but_the_load_suite() -> None:
    job = read_job(CI.read_text(encoding="utf-8"), CI_INTEGRATION_SPEC.job_id)
    assert static_problems(job, CI_INTEGRATION_SPEC) == []
    assert job.groups == (1, 2, 3, 4)
    assert job.option("--ignore") == "tests/load"
    assert job.option("--durations-path") == ".test_durations_integracion"
    # El perfil de la orden es el de la variable del paso; la recolección lo sustituye.
    assert job.option("--hypothesis-profile") == "$PERFIL"
    assert "--hypothesis-profile=pr" in job.for_group(2, "pr")
    assert "--group=2" in job.for_group(2, "pr")


@pytest.mark.parametrize("spec", [NIGHTLY_SUITE_SPEC, NIGHTLY_INTEGRATION_SPEC])
def test_the_nightly_property_jobs_split_tests_properties_without_excluding_anything(
    spec: SplitSpec,
) -> None:
    job = read_job(NIGHTLY.read_text(encoding="utf-8"), spec.job_id)
    assert static_problems(job, spec) == []
    assert job.groups == (1, 2, 3, 4, 5)
    assert job.option("--durations-path") == ".test_durations_nightly"
    assert (BACKEND / spec.durations).is_file()


def test_the_four_split_jobs_are_checked() -> None:
    assert [(spec.workflow, spec.job_id) for spec in SPECS] == [
        (CI, "backend-pruebas"),
        (CI, "backend-integracion-grupos"),
        (NIGHTLY, "propiedades-suite"),
        (NIGHTLY, "propiedades-integracion"),
    ]


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


def test_the_synthetic_nightly_workflow_passes() -> None:
    job = read_job(_nightly_workflow(), NIGHTLY_INTEGRATION_SPEC.job_id)
    assert static_problems(job, NIGHTLY_INTEGRATION_SPEC) == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            _NIGHTLY_COMMAND.removesuffix(" tests/properties"),
            "la orden debe nombrar exactamente ['tests/properties']: []",
        ),
        (
            _NIGHTLY_COMMAND.replace("tests/properties", "tests/properties/gob"),
            "la orden debe nombrar exactamente ['tests/properties']: ['tests/properties/gob']",
        ),
        (
            f"{_NIGHTLY_COMMAND} tests/integration",
            "la orden debe nombrar exactamente ['tests/properties']: "
            "['tests/properties', 'tests/integration']",
        ),
        (
            f"{_NIGHTLY_COMMAND} --ignore=tests/properties/gob/test_pr_gob_29_node_rate_limits.py",
            "la orden saca pruebas antes del reparto: "
            "--ignore=tests/properties/gob/test_pr_gob_29_node_rate_limits.py",
        ),
        (
            _NIGHTLY_COMMAND.replace("-m integration", '-m "not integration"'),
            "la orden debe seleccionar exactamente -m \"integration\": ['not integration']",
        ),
        (
            _NIGHTLY_COMMAND.replace(".test_durations_nightly", ".test_durations"),
            "la orden debe leer los tiempos de --durations-path=.test_durations_nightly",
        ),
        (
            _NIGHTLY_COMMAND.replace("--splits=5", "--splits=4"),
            "la matriz debe numerar los grupos 1..4: [1, 2, 3, 4, 5]",
        ),
    ],
)
def test_a_broken_nightly_split_fails(command: str, expected: str) -> None:
    job = read_job(_nightly_workflow(command), NIGHTLY_INTEGRATION_SPEC.job_id)
    assert expected in static_problems(job, NIGHTLY_INTEGRATION_SPEC)


def test_the_synthetic_integration_workflow_passes() -> None:
    job = read_job(_integration_workflow(), CI_INTEGRATION_SPEC.job_id)
    assert static_problems(job, CI_INTEGRATION_SPEC) == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            _INTEGRATION_COMMAND.replace("--ignore=tests/load ", ""),
            "la orden debe llevar exactamente --ignore=tests/load: []",
        ),
        (
            f"{_INTEGRATION_COMMAND} --ignore=tests/properties",
            "la orden saca pruebas antes del reparto: --ignore=tests/properties",
        ),
        (
            f"{_INTEGRATION_COMMAND} tests/properties",
            "la orden corre todo testpaths, sin rutas: tests/properties",
        ),
        (
            _INTEGRATION_COMMAND.replace("-m integration", '-m "not integration"'),
            "la orden debe seleccionar exactamente -m \"integration\": ['not integration']",
        ),
        (
            _INTEGRATION_COMMAND.replace('"$PERFIL"', "nightly"),
            'la orden debe pasar --hypothesis-profile=ci o --hypothesis-profile="$PERFIL"',
        ),
        (
            _INTEGRATION_COMMAND.replace(".test_durations_integracion", ".test_durations"),
            "la orden debe leer los tiempos de --durations-path=.test_durations_integracion",
        ),
    ],
)
def test_a_broken_integration_split_fails(command: str, expected: str) -> None:
    job = read_job(_integration_workflow(command), CI_INTEGRATION_SPEC.job_id)
    assert expected in static_problems(job, CI_INTEGRATION_SPEC)


def test_main_checks_every_split_job_of_a_given_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--workflow`` comprueba los trabajos repartidos que contiene, cada uno con su forma."""
    workflow = tmp_path / "nightly.yml"
    broken = _nightly_workflow(_NIGHTLY_COMMAND.replace("tests/properties", "tests/properties/gob"))
    workflow.write_text(broken, encoding="utf-8")
    assert main(["--workflow", str(workflow)]) == 1
    assert "propiedades-integracion: la orden debe nombrar" in capsys.readouterr().err
    workflow.write_text(_nightly_workflow(), encoding="utf-8")
    assert main(["--workflow", str(workflow)]) == 0


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
        ("jobs: [\n", "el flujo no es YAML válido"),
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
    broken.write_text("jobs: [\n", encoding="utf-8")
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


@pytest.fixture
def nightly_project(project: Path) -> Path:
    """El proyecto sintético con un ``tests/properties`` de pruebas con y sin base, fuera de él
    otras pruebas con base (que los grupos no deben correr) y un archivo de tiempos."""
    (project / "tests" / "conftest.py").write_text(
        "from hypothesis import settings\n"
        "settings.register_profile('ci')\n"
        "settings.register_profile('nightly')\n",
        encoding="utf-8",
    )
    properties = project / "tests" / "properties"
    properties.mkdir()
    for name in ("p", "q", "r", "s", "t", "u"):
        (properties / f"test_{name}.py").write_text(
            "import pytest\n\n"
            + "".join(
                f"@pytest.mark.integration\ndef test_{name}{n}():\n    pass\n\n" for n in range(2)
            )
            + f"def test_{name}_sin_base():\n    pass\n",
            encoding="utf-8",
        )
    durations = {f"tests/properties/test_p.py::test_p{n}": 100.0 + n for n in range(2)}
    (project / ".test_durations_nightly").write_text(json.dumps(durations), encoding="utf-8")
    return project


@pytest.fixture
def integration_project(project: Path) -> Path:
    """El proyecto sintético con pruebas de integración en varios directorios y un ``tests/load``
    de integración que los grupos no deben correr (va en «backend (carga ci)»)."""
    for directory in ("integration", "properties", "load"):
        (project / "tests" / directory).mkdir()
        for name in ("m", "n", "o"):
            (project / "tests" / directory / f"test_{directory}_{name}.py").write_text(
                "import pytest\n\n"
                + "".join(
                    f"@pytest.mark.integration\ndef test_{name}{n}():\n    pass\n\n"
                    for n in range(2)
                )
                + f"def test_{name}_sin_base():\n    pass\n",
                encoding="utf-8",
            )
    return project


def test_the_integration_groups_are_a_partition_without_the_load_suite(
    integration_project: Path,
) -> None:
    workflow = _integration_workflow()
    assert check(workflow, collect=True, root=integration_project, spec=CI_INTEGRATION_SPEC) == []


def test_a_file_dropped_from_the_integration_groups_is_detected(
    integration_project: Path,
) -> None:
    """Mutación: un ``conftest`` saca ``tests/properties/test_properties_n.py`` del grupo que le
    toca; la comprobación lo dice prueba a prueba."""
    with (integration_project / "tests" / "conftest.py").open("a", encoding="utf-8") as conftest:
        conftest.write(
            "import pytest\n\n"
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    if config.getoption('group', None):\n"
            "        items[:] = [i for i in items if 'test_properties_n.py' not in i.nodeid]\n"
        )
    assert check(
        _integration_workflow(), collect=True, root=integration_project, spec=CI_INTEGRATION_SPEC
    ) == [f"ningún grupo corre tests/properties/test_properties_n.py::test_n{n}" for n in range(2)]


def _nightly_ids(*names: str) -> list[str]:
    return [f"tests/properties/test_{name}.py::test_{name}{n}" for name in names for n in range(2)]


def test_the_nightly_groups_are_a_partition_and_collection_keeps_the_durations(
    nightly_project: Path,
) -> None:
    durations = (nightly_project / ".test_durations_nightly").read_bytes()
    assert (
        check(
            _nightly_workflow(), collect=True, root=nightly_project, spec=NIGHTLY_INTEGRATION_SPEC
        )
        == []
    )
    # La recolección quita --store-durations: sin eso reescribiría el archivo de tiempos.
    assert (nightly_project / ".test_durations_nightly").read_bytes() == durations


def test_a_property_file_dropped_from_the_nightly_groups_is_detected(
    nightly_project: Path,
) -> None:
    """Mutación (VIG-199): un ``conftest`` saca ``tests/properties/test_r.py`` del grupo que le
    toca; la comprobación lo dice prueba a prueba."""
    with (nightly_project / "tests" / "conftest.py").open("a", encoding="utf-8") as conftest:
        conftest.write(
            "import pytest\n\n"
            "@pytest.hookimpl(trylast=True)\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    if config.getoption('group', None):\n"
            "        items[:] = [i for i in items if 'test_r.py' not in i.nodeid]\n"
        )
    assert check(
        _nightly_workflow(), collect=True, root=nightly_project, spec=NIGHTLY_INTEGRATION_SPEC
    ) == [f"ningún grupo corre {test}" for test in _nightly_ids("r")]


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
