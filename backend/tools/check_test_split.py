"""Reparto de las pruebas en grupos por tiempos (pytest-split; VIG-198 y VIG-199).

Tres trabajos son matrices de grupos de pytest-split; cada grupo corre la orden de su trabajo con
``--splits=N --group=<grupo>`` y los tiempos de su archivo:

- ``ci.yml``, «backend (pruebas sin integración, grupo N de 4)» (``backend-pruebas``):
  ``-m "not integration"`` sobre todo ``testpaths``, perfil ``ci``, tiempos de ``.test_durations``;
- ``nightly.yml``, «propiedades nightly (tests/properties, grupo N de M)» (``propiedades-suite``):
  ``-m "not integration" tests/properties``, perfil ``nightly``, tiempos de
  ``.test_durations_nightly``;
- ``nightly.yml``, «propiedades nightly (integración, grupo N de M)» (``propiedades-integracion``):
  ``-m integration tests/properties``, perfil ``nightly``, el mismo archivo de tiempos.

Esta herramienta comprueba, en cada uno, que **la unión de los grupos es exactamente la colección
completa** del trabajo: ninguna prueba fuera, ninguna en dos grupos, ninguna que no esté en la
colección completa.

Hace dos cosas:

- **Estática** (sin pytest): lee el flujo y exige que la matriz numere los grupos ``1..N`` con el
  mismo ``N`` que ``--splits``, que la orden de cada grupo seleccione exactamente el marcador y las
  rutas del trabajo, sin ``--ignore``, ``--deselect`` ni ``-k`` (nada que saque pruebas antes del
  reparto), que lea los tiempos de su archivo y que pase el grupo de la matriz. La fijan las
  pruebas de ``tests/unit/test_check_test_split.py``.
- **Dinámica** (``--collect``): recolecta con pytest la colección completa y la de cada grupo con
  **la orden real del flujo** (``--collect-only``, con ``$GRUPO`` sustituido y sin
  ``--store-durations``, que reescribiría el archivo de tiempos), y compara. La ejecuta el trabajo
  «backend (lint y tipos)» de ``ci.yml`` en cada PR, para los tres trabajos.

Uso (desde ``backend/``)::

    uv run python tools/check_test_split.py            # solo la comprobación estática
    uv run python tools/check_test_split.py --collect  # y la de las colecciones reales
    uv run python tools/check_test_split.py --workflow otro.yml  # los repartidos de otro.yml

Termina en 0 si el reparto es correcto, en 1 si no y en 2 si un flujo o pytest no se pueden leer
o ejecutar.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

__all__ = [
    "CI_SPEC",
    "GROUP_VARIABLE",
    "NIGHTLY_INTEGRATION_SPEC",
    "NIGHTLY_SUITE_SPEC",
    "SPECS",
    "SplitError",
    "SplitJob",
    "SplitSpec",
    "check",
    "main",
    "parse_collection",
    "partition_problems",
    "read_job",
    "static_problems",
]

BACKEND: Final = Path(__file__).resolve().parents[1]
WORKFLOWS: Final = BACKEND.parent / ".github" / "workflows"
CI: Final = WORKFLOWS / "ci.yml"
NIGHTLY: Final = WORKFLOWS / "nightly.yml"
GROUP_VARIABLE: Final = "GRUPO"
"""Variable de entorno del paso con el número de grupo de la matriz (``--group="$GRUPO"``)."""
COLLECT_TIMEOUT_SECONDS: Final = 900.0
"""Tope de cada recolección: holgado, recolectar ~6000 pruebas tarda segundos."""


@dataclass(frozen=True, slots=True)
class SplitSpec:
    """Un trabajo repartido: dónde está y qué colección deben repartir sus grupos."""

    workflow: Path
    job_id: str
    marker: str
    """El ``-m`` de la orden y de la colección completa."""
    paths: tuple[str, ...]
    """Las rutas de la orden (vacío: todo ``testpaths``)."""
    profile: str
    """Perfil de Hypothesis de la colección completa."""
    durations: str
    """Archivo de tiempos (``--durations-path``), relativo a ``backend/``."""

    def full_collection(self) -> list[str]:
        """Argumentos de pytest de la colección completa del trabajo."""
        return [
            "-p",
            "no:cacheprovider",
            f"--hypothesis-profile={self.profile}",
            "-m",
            self.marker,
            *self.paths,
        ]


CI_SPEC: Final = SplitSpec(
    workflow=CI,
    job_id="backend-pruebas",
    marker="not integration",
    paths=(),
    profile="ci",
    durations=".test_durations",
)
NIGHTLY_SUITE_SPEC: Final = SplitSpec(
    workflow=NIGHTLY,
    job_id="propiedades-suite",
    marker="not integration",
    paths=("tests/properties",),
    profile="nightly",
    durations=".test_durations_nightly",
)
NIGHTLY_INTEGRATION_SPEC: Final = SplitSpec(
    workflow=NIGHTLY,
    job_id="propiedades-integracion",
    marker="integration",
    paths=("tests/properties",),
    profile="nightly",
    durations=".test_durations_nightly",
)
SPECS: Final = (CI_SPEC, NIGHTLY_SUITE_SPEC, NIGHTLY_INTEGRATION_SPEC)

_EXCLUDING = ("--ignore", "--ignore-glob", "--deselect", "-k")
_STORING = frozenset({"--store-durations", "--clean-durations"})
"""Opciones que reescriben el archivo de tiempos al terminar: la recolección las quita."""
_VERBOSITY = frozenset({"-q", "--quiet", "-qq", "-v", "--verbose", "-vv"})
_VALUE_OPTIONS = frozenset({"-m", "-p", "-k", "-o", "-c", "--ignore", "--deselect"})


class SplitError(Exception):
    """El flujo no tiene la forma esperada o pytest no se pudo ejecutar."""


@dataclass(frozen=True, slots=True)
class SplitJob:
    """El trabajo de la matriz: sus grupos y los argumentos de pytest de su orden."""

    groups: tuple[int, ...]
    arguments: tuple[str, ...]
    """Argumentos tras ``pytest``, con ``$GRUPO`` sin sustituir."""

    def option(self, name: str) -> str | None:
        """Valor de ``--name=valor`` (la forma que usan los flujos), o ``None``."""
        values = [arg.split("=", 1)[1] for arg in self.arguments if arg.startswith(f"{name}=")]
        return values[-1] if values else None

    def for_group(self, group: int) -> list[str]:
        variable = f"${GROUP_VARIABLE}"
        return [arg.replace(variable, str(group)) for arg in self.arguments]


def _pytest_arguments(run: str) -> list[list[str]]:
    found = []
    for line in run.splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as error:
            raise SplitError(f"línea de run: ilegible: {line!r} ({error})") from None
        if "pytest" in tokens:
            found.append(tokens[tokens.index("pytest") + 1 :])
    return found


def read_job(text: str, job_id: str = CI_SPEC.job_id) -> SplitJob:
    """El trabajo ``job_id`` de ``text`` (un flujo de GitHub Actions)."""
    try:
        workflow = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise SplitError(f"el flujo no es YAML válido: {error}") from None
    job = ((workflow.get("jobs") or {}) if isinstance(workflow, dict) else {}).get(job_id)
    if not isinstance(job, dict):
        raise SplitError(f"falta el trabajo {job_id}")
    matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("grupo")
    if not isinstance(matrix, list) or not all(
        isinstance(group, int) and not isinstance(group, bool) for group in matrix
    ):
        raise SplitError(f"{job_id}: strategy.matrix.grupo debe ser una lista de enteros")
    calls = [
        (step, arguments)
        for step in job.get("steps") or []
        for arguments in _pytest_arguments(str(step.get("run") or ""))
    ]
    if len(calls) != 1:
        raise SplitError(f"{job_id}: debe tener exactamente una orden pytest ({len(calls)})")
    step, arguments = calls[0]
    if str((step.get("env") or {}).get(GROUP_VARIABLE, "")).replace(" ", "") != (
        "${{matrix.grupo}}"
    ):
        raise SplitError(f"{job_id}: el paso de pytest debe tener env {GROUP_VARIABLE}")
    return SplitJob(groups=tuple(matrix), arguments=tuple(arguments))


def static_problems(job: SplitJob, spec: SplitSpec = CI_SPEC) -> list[str]:
    """Lo que, sin ejecutar pytest, ya rompe el reparto de ``job`` frente a ``spec``."""
    problems: list[str] = []
    splits = job.option("--splits")
    if splits is None or not splits.isdigit() or int(splits) < 2:
        problems.append("la orden necesita --splits=N con N ≥ 2")
    elif list(job.groups) != list(range(1, int(splits) + 1)):
        problems.append(f"la matriz debe numerar los grupos 1..{splits}: {list(job.groups)}")
    if job.option("--group") != f"${GROUP_VARIABLE}":
        problems.append(f'la orden debe pasar --group="${GROUP_VARIABLE}"')
    if job.option("--durations-path") != spec.durations:
        problems.append(f"la orden debe leer los tiempos de --durations-path={spec.durations}")
    markers = [
        value
        for option, value in zip(job.arguments, job.arguments[1:], strict=False)
        if option == "-m"
    ] + [arg.split("=", 1)[1] for arg in job.arguments if arg.startswith("-m=")]
    if markers != [spec.marker]:
        problems.append(f'la orden debe seleccionar exactamente -m "{spec.marker}": {markers}')
    paths: list[str] = []
    previous = ""
    for argument in job.arguments:
        name = argument.split("=", 1)[0]
        if name in _EXCLUDING:
            problems.append(f"la orden saca pruebas antes del reparto: {argument}")
        elif not argument.startswith("-") and previous not in _VALUE_OPTIONS:
            paths.append(argument)
        previous = argument
    if not spec.paths:
        problems.extend(f"la orden corre todo testpaths, sin rutas: {path}" for path in paths)
    elif paths != list(spec.paths):
        problems.append(f"la orden debe nombrar exactamente {list(spec.paths)}: {paths}")
    return problems


def parse_collection(output: str) -> list[str]:
    """Identificadores de prueba de la salida de ``pytest --collect-only -q``.

    Son las líneas con ``::`` antes de la primera línea en blanco tras ellas (después vienen el
    recuento y los avisos); un identificador parametrizado puede llevar espacios.
    """
    tests: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            if tests:
                break
            continue
        if "::" in line:
            tests.append(line.rstrip())
    return tests


def partition_problems(full: Iterable[str], groups: Mapping[int, Sequence[str]]) -> list[str]:
    """Lo que impide que ``groups`` sea una partición exacta de ``full``."""
    expected = Counter(full)
    problems = [f"la colección completa repite {test}" for test, n in expected.items() if n > 1]
    seen: Counter[str] = Counter()
    for group, tests in sorted(groups.items()):
        if not tests:
            problems.append(f"el grupo {group} está vacío")
        seen.update(tests)
    for test in sorted(set(expected) - set(seen)):
        problems.append(f"ningún grupo corre {test}")
    for test, count in sorted(seen.items()):
        if test not in expected:
            problems.append(f"un grupo corre {test}, que no está en la colección completa")
        elif count > 1:
            problems.append(f"{test} corre {count} veces")
    return problems


def _collect(arguments: Sequence[str], root: Path) -> list[str]:
    # Exactamente un -q: con dos, --collect-only da recuentos por archivo, no identificadores.
    kept = [arg for arg in arguments if arg not in _VERBOSITY and arg not in _STORING]
    command = [sys.executable, "-m", "pytest", *kept, "--collect-only", "-q"]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SplitError(f"no se pudo recolectar ({shlex.join(command)}): {error}") from None
    if result.returncode != 0:
        raise SplitError(
            f"la recolección terminó en {result.returncode} ({shlex.join(command)}):\n"
            f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
        )
    return parse_collection(result.stdout)


def check(
    text: str, *, collect: bool, root: Path = BACKEND, spec: SplitSpec = CI_SPEC
) -> list[str]:
    """Problemas del reparto del trabajo ``spec`` en ``text``; con ``collect``, recolecta en
    ``root``."""
    job = read_job(text, spec.job_id)
    problems = static_problems(job, spec)
    if problems or not collect:
        return problems
    full = _collect(spec.full_collection(), root)
    groups = {group: _collect(job.for_group(group), root) for group in job.groups}
    for group, tests in groups.items():
        print(f"  {spec.job_id}, grupo {group}: {len(tests)} pruebas")
    print(f"  {spec.job_id}, colección completa: {len(full)} pruebas")
    return partition_problems(full, groups)


def _specs_for(workflow: Path | None) -> list[tuple[SplitSpec, str]]:
    """Cada trabajo a comprobar con el texto de su flujo; con ``workflow``, los trabajos
    repartidos que contiene (comprobados como si fuera su flujo)."""
    if workflow is None:
        texts = {path: path.read_text(encoding="utf-8") for path in {s.workflow for s in SPECS}}
        return [(spec, texts[spec.workflow]) for spec in SPECS]
    text = workflow.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise SplitError(f"el flujo no es YAML válido: {error}") from None
    jobs = (loaded.get("jobs") or {}) if isinstance(loaded, dict) else {}
    found = [(spec, text) for spec in SPECS if isinstance(jobs, dict) and spec.job_id in jobs]
    if not found:
        names = ", ".join(spec.job_id for spec in SPECS)
        raise SplitError(f"{workflow}: no tiene ningún trabajo repartido ({names})")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Comprueba que los grupos de pytest-split de ci.yml y nightly.yml reparten su colección"
        )
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=None,
        help="flujo a comprobar (por omisión, los trabajos repartidos de ci.yml y nightly.yml)",
    )
    parser.add_argument(
        "--collect", action="store_true", help="recolecta con pytest la colección y cada grupo"
    )
    args = parser.parse_args(argv)
    problems: list[str] = []
    try:
        for spec, text in _specs_for(args.workflow):
            problems.extend(
                f"{spec.job_id}: {problem}"
                for problem in check(text, collect=args.collect, spec=spec)
            )
    except (OSError, SplitError) as error:
        print(f"check_test_split: {error}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if problems:
        print(
            f"check_test_split: {len(problems)} problema(s) en el reparto. Si una prueba aparece "
            "con otro identificador en cada recolección (uuid4(), object(), una semilla), dale "
            "ids= estables: pytest-split ordena y reparte por identificador.",
            file=sys.stderr,
        )
        return 1
    print("check_test_split: los grupos reparten la colección sin huecos ni duplicados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
