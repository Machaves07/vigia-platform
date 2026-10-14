"""Reparto de las pruebas sin integración de ``ci.yml`` en grupos (pytest-split; VIG-198).

El trabajo «backend (pruebas sin integración, grupo N de 4)» es una matriz: cada grupo corre
``pytest … -m "not integration" --splits=4 --group=N`` con los tiempos de ``.test_durations``.
Esta herramienta comprueba que **la unión de los grupos es exactamente la colección completa** de
``-m "not integration"``: ninguna prueba fuera, ninguna en dos grupos, ninguna que no esté en la
colección completa.

Hace dos cosas:

- **Estática** (sin pytest): lee ``ci.yml`` y exige que la matriz numere los grupos ``1..N`` con
  el mismo ``N`` que ``--splits``, que la orden de cada grupo seleccione ``-m "not integration"``
  sin rutas, ``--ignore``, ``--deselect`` ni ``-k`` (nada que saque pruebas antes del reparto), y
  que pase el grupo de la matriz. La fijan las pruebas de ``tests/unit/test_check_test_split.py``.
- **Dinámica** (``--collect``): recolecta con pytest la colección completa y la de cada grupo con
  **la orden real de ``ci.yml``** (``--collect-only``, con ``$GRUPO`` sustituido), y compara. La
  ejecuta el trabajo «backend (lint y tipos)» en cada PR.

Uso (desde ``backend/``)::

    uv run python tools/check_test_split.py            # solo la comprobación estática
    uv run python tools/check_test_split.py --collect  # y la de las colecciones reales

Termina en 0 si el reparto es correcto, en 1 si no y en 2 si ``ci.yml`` o pytest no se pueden
leer o ejecutar.
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
    "GROUP_VARIABLE",
    "JOB_ID",
    "MARKER",
    "SplitError",
    "SplitJob",
    "check",
    "main",
    "parse_collection",
    "partition_problems",
    "read_job",
    "static_problems",
]

BACKEND: Final = Path(__file__).resolve().parents[1]
CI: Final = BACKEND.parent / ".github" / "workflows" / "ci.yml"
JOB_ID: Final = "backend-pruebas"
MARKER: Final = "not integration"
GROUP_VARIABLE: Final = "GRUPO"
"""Variable de entorno del paso con el número de grupo de la matriz (``--group="$GRUPO"``)."""
COLLECT_TIMEOUT_SECONDS: Final = 900.0
"""Tope de cada recolección: holgado, recolectar ~3000 pruebas tarda segundos."""

_EXCLUDING = ("--ignore", "--ignore-glob", "--deselect", "-k")
_VERBOSITY = frozenset({"-q", "--quiet", "-qq", "-v", "--verbose", "-vv"})
_VALUE_OPTIONS = frozenset({"-m", "-p", "-k", "-o", "-c", "--ignore", "--deselect"})


class SplitError(Exception):
    """``ci.yml`` no tiene la forma esperada o pytest no se pudo ejecutar."""


@dataclass(frozen=True, slots=True)
class SplitJob:
    """El trabajo de la matriz: sus grupos y los argumentos de pytest de su orden."""

    groups: tuple[int, ...]
    arguments: tuple[str, ...]
    """Argumentos tras ``pytest``, con ``$GRUPO`` sin sustituir."""

    def option(self, name: str) -> str | None:
        """Valor de ``--name=valor`` (la forma que usa ``ci.yml``), o ``None``."""
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


def read_job(text: str) -> SplitJob:
    """El trabajo ``backend-pruebas`` de ``text`` (un ``ci.yml``)."""
    try:
        workflow = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise SplitError(f"ci.yml no es YAML válido: {error}") from None
    job = ((workflow.get("jobs") or {}) if isinstance(workflow, dict) else {}).get(JOB_ID)
    if not isinstance(job, dict):
        raise SplitError(f"falta el trabajo {JOB_ID}")
    matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("grupo")
    if not isinstance(matrix, list) or not all(
        isinstance(group, int) and not isinstance(group, bool) for group in matrix
    ):
        raise SplitError(f"{JOB_ID}: strategy.matrix.grupo debe ser una lista de enteros")
    calls = [
        (step, arguments)
        for step in job.get("steps") or []
        for arguments in _pytest_arguments(str(step.get("run") or ""))
    ]
    if len(calls) != 1:
        raise SplitError(f"{JOB_ID}: debe tener exactamente una orden pytest ({len(calls)})")
    step, arguments = calls[0]
    if str((step.get("env") or {}).get(GROUP_VARIABLE, "")).replace(" ", "") != (
        "${{matrix.grupo}}"
    ):
        raise SplitError(f"{JOB_ID}: el paso de pytest debe tener env {GROUP_VARIABLE}")
    return SplitJob(groups=tuple(matrix), arguments=tuple(arguments))


def static_problems(job: SplitJob) -> list[str]:
    """Lo que, sin ejecutar pytest, ya rompe el reparto."""
    problems: list[str] = []
    splits = job.option("--splits")
    if splits is None or not splits.isdigit() or int(splits) < 2:
        problems.append("la orden necesita --splits=N con N ≥ 2")
    elif list(job.groups) != list(range(1, int(splits) + 1)):
        problems.append(f"la matriz debe numerar los grupos 1..{splits}: {list(job.groups)}")
    if job.option("--group") != f"${GROUP_VARIABLE}":
        problems.append(f'la orden debe pasar --group="${GROUP_VARIABLE}"')
    markers = [
        value
        for option, value in zip(job.arguments, job.arguments[1:], strict=False)
        if option == "-m"
    ] + [arg.split("=", 1)[1] for arg in job.arguments if arg.startswith("-m=")]
    if markers != [MARKER]:
        problems.append(f'la orden debe seleccionar exactamente -m "{MARKER}": {markers}')
    previous = ""
    for argument in job.arguments:
        name = argument.split("=", 1)[0]
        if name in _EXCLUDING:
            problems.append(f"la orden saca pruebas antes del reparto: {argument}")
        elif not argument.startswith("-") and previous not in _VALUE_OPTIONS:
            problems.append(f"la orden corre todo testpaths, sin rutas: {argument}")
        previous = argument
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
    kept = [arg for arg in arguments if arg not in _VERBOSITY]
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


def check(text: str, *, collect: bool, root: Path = BACKEND) -> list[str]:
    """Problemas del reparto de ``text`` (un ``ci.yml``); con ``collect``, recolecta en ``root``."""
    job = read_job(text)
    problems = static_problems(job)
    if problems or not collect:
        return problems
    full = _collect(["-p", "no:cacheprovider", "--hypothesis-profile=ci", "-m", MARKER], root)
    groups = {group: _collect(job.for_group(group), root) for group in job.groups}
    for group, tests in groups.items():
        print(f"  grupo {group}: {len(tests)} pruebas")
    print(f"  colección completa: {len(full)} pruebas")
    return partition_problems(full, groups)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Comprueba que los grupos de pytest-split de ci.yml reparten la colección."
    )
    parser.add_argument("--workflow", type=Path, default=CI, help="ci.yml a comprobar")
    parser.add_argument(
        "--collect", action="store_true", help="recolecta con pytest la colección y cada grupo"
    )
    args = parser.parse_args(argv)
    try:
        text = args.workflow.read_text(encoding="utf-8")
        problems = check(text, collect=args.collect)
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
