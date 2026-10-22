"""Guarda de U-04: el diff exclusivo de vigia-loop no puede incluir otra pila.

La CLI de CDK debe terminar con éxito antes de llamar a esta guarda. Se usa
``--exclusively --no-change-set --no-color``; sin cabecera reconocible falla cerrado.
No accede a AWS ni ejecuta el diff: valida el informe que se adjunta al release.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

_STACK = re.compile(r"^Stack ([^\s()]+)(?: \(([^()]+)\))?$")


def validate(text: str, expected: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("Stack ")]
    headers = [match for line in lines if (match := _STACK.fullmatch(line))]
    if not headers:
        return ["sin cabecera Stack reconocible: no se demuestra el aislamiento de vigia-loop"]
    problems = [
        f"cabecera Stack no reconocible: {line}" for line in lines if not _STACK.fullmatch(line)
    ]
    for header in headers:
        if header[1] != expected or (header[2] is not None and header[2] != expected):
            problems.append(f"el diff de {expected} incluye otra pila: {header[0]}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--stack", required=True)
    args = parser.parse_args(argv)
    try:
        problems = validate(args.report.read_text(encoding="utf-8"), args.stack)
    except (OSError, UnicodeError) as error:
        problems = [f"informe de diff ilegible: {type(error).__name__}"]
    for problem in problems:
        print(problem)
    if not problems:
        print(f"Diff aislado en {args.stack}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
