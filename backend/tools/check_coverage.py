"""Umbrales de cobertura de líneas de ``ci.yml`` (NFR-NUC-45, NFR-CTR-32 y 33; TASK-143).

Lee el informe JSON de coverage.py (``coverage json``, con ``relative_files``: rutas
``src/vigia_platform/…``) de la suite completa (los dos trabajos «backend» combinados) y exige:

- **≥ 90 %** en el dominio y la aplicación de ``identity`` y de ``ledger``, cada módulo por
  separado ``[objetivos propios]``. Dominio y aplicación es todo el módulo **salvo sus
  adaptadores** (``adapters/``: HTTP y PostgreSQL, la frontera de puertos y adaptadores de
  tech-stack-decisions.md §7): ``domain/`` y ``application/``, y también los módulos críticos
  que viven fuera de esas carpetas (``identity.auth``, ``identity.authz``, ``ledger.chain``…).
- **≥ 80 %** en el total del paquete ``vigia_platform``.

Un área sin ninguna sentencia medida falla: un informe que no cubre el módulo no demuestra nada.

Uso::

    uv run coverage combine && uv run coverage json -o coverage.json
    uv run python tools/check_coverage.py coverage.json

Termina en 0 si todo cumple, en 1 si algún umbral no se alcanza y en 2 si el informe no se puede
leer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Final

__all__ = ["AREAS", "GLOBAL_MINIMUM", "Area", "AreaResult", "CoverageError", "evaluate", "main"]

PACKAGE_PREFIX: Final = "src/vigia_platform/"


@dataclass(frozen=True, slots=True)
class Area:
    """Un conjunto de archivos del informe con su umbral."""

    name: str
    include: str
    """Prefijo de ruta (relativo a ``backend/``) de los archivos del área."""
    exclude: tuple[str, ...]
    minimum: Decimal


AREAS: Final = (
    Area(
        name="identity (dominio y aplicación)",
        include=f"{PACKAGE_PREFIX}identity/",
        exclude=(f"{PACKAGE_PREFIX}identity/adapters/",),
        minimum=Decimal(90),
    ),
    Area(
        name="ledger (dominio y aplicación)",
        include=f"{PACKAGE_PREFIX}ledger/",
        exclude=(f"{PACKAGE_PREFIX}ledger/adapters/",),
        minimum=Decimal(90),
    ),
)
GLOBAL_MINIMUM: Final = Area(
    name="global (vigia_platform)", include=PACKAGE_PREFIX, exclude=(), minimum=Decimal(80)
)


class CoverageError(Exception):
    """El informe no se puede leer o no tiene la forma de ``coverage json`` (código 2)."""


@dataclass(frozen=True, slots=True)
class AreaResult:
    area: Area
    covered: int
    statements: int

    @property
    def percent(self) -> Decimal:
        """Porcentaje truncado a dos decimales: nunca se redondea hacia el umbral."""
        if self.statements == 0:
            return Decimal(0)
        value = Decimal(self.covered) * 100 / Decimal(self.statements)
        return value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)

    @property
    def passed(self) -> bool:
        return self.statements > 0 and self.percent >= self.area.minimum

    def __str__(self) -> str:
        verdict = "cumple" if self.passed else "NO CUMPLE"
        return (
            f"{self.area.name}: {self.percent} % ({self.covered}/{self.statements} líneas;"
            f" mínimo {self.area.minimum} %) {verdict}"
        )


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _file_lines(report: Any) -> Mapping[str, tuple[int, int]]:
    if not isinstance(report, dict) or not isinstance(report.get("files"), dict):
        raise CoverageError("el informe no tiene la clave «files» de coverage json")
    lines: dict[str, tuple[int, int]] = {}
    for path, data in report["files"].items():
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, dict):
            raise CoverageError(f"{path}: sin «summary»")
        covered, statements = summary.get("covered_lines"), summary.get("num_statements")
        if (
            not isinstance(covered, int)
            or not isinstance(statements, int)
            or isinstance(covered, bool)
            or isinstance(statements, bool)
            or not 0 <= covered <= statements
        ):
            raise CoverageError(f"{path}: «covered_lines» y «num_statements» no son coherentes")
        lines[_normalize(str(path))] = (covered, statements)
    return lines


def _measure(area: Area, lines: Mapping[str, tuple[int, int]]) -> AreaResult:
    covered = statements = 0
    for path, (file_covered, file_statements) in lines.items():
        if path.startswith(area.include) and not path.startswith(area.exclude):
            covered += file_covered
            statements += file_statements
    return AreaResult(area=area, covered=covered, statements=statements)


def evaluate(report: Any) -> list[AreaResult]:
    """El resultado de cada área y del total, en ese orden."""
    lines = _file_lines(report)
    return [_measure(area, lines) for area in (*AREAS, GLOBAL_MINIMUM)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aplica los umbrales de cobertura de NFR-NUC-45 a un informe coverage json."
    )
    parser.add_argument("report", type=Path, help="salida de «coverage json»")
    args = parser.parse_args(argv)
    try:
        try:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CoverageError(f"no se pudo leer {args.report}: {error}") from None
        results = evaluate(report)
    except CoverageError as error:
        print(f"check_coverage: {error}", file=sys.stderr)
        return 2
    for result in results:
        print(f"  {result}")
    failed = [result for result in results if not result.passed]
    if failed:
        print(f"check_coverage: {len(failed)} umbral(es) sin alcanzar", file=sys.stderr)
        return 1
    print("check_coverage: todos los umbrales se cumplen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
