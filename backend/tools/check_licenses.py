"""Licencias de las dependencias de vigia-platform (NFR-NUC-24, NFR-CTR-26; TASK-143).

Aplica la lista permitida (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF y MPL-2.0) a
cada distribución instalada en el entorno en que corre (``uv sync --frozen``: las dependencias de
ejecución y las del grupo ``dev``). Es la versión para Python de ``scripts/check_licenses.py`` de
vigia-contracts, con las mismas reglas:

- La licencia de una distribución sale de ``License-Expression`` (PEP 639); si no la declara, de
  sus clasificadores ``License ::`` (todos exigidos); si tampoco, del campo ``License``.
- ``A OR B`` basta con que una alternativa esté permitida; ``A AND B`` exige todas.
- Cualquier otra licencia (GPL, LGPL, AGPL, propietaria, desconocida o sin declarar) falla y
  nombra el paquete, salvo que ``LICENSE-EXCEPTIONS.md`` registre una excepción vigente para ese
  paquete y esa licencia. Una excepción cuya fecha de revisión pasó también falla.
- Una excepción cuyo alcance empieza por ``herramienta de CI`` (A-43) solo vale mientras el
  paquete no sea una dependencia de ejecución de la imagen (el cierre de ``vigia-platform`` sin
  grupos en ``uv.lock``); si llega a serlo, falla.

Uso::

    uv run python tools/check_licenses.py [--exceptions ARCHIVO] [--lock ARCHIVO]
        [--today AAAA-MM-DD]

Termina en 0 si todo cumple, en 1 si hay licencias no permitidas o excepciones vencidas y en 2 si
el registro de excepciones o el bloqueo no se pudieron leer.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

__all__ = [
    "Dependency",
    "LicenseException",
    "check",
    "installed_dependencies",
    "license_leaves",
    "load_exceptions",
    "main",
    "runtime_closure",
]

BACKEND: Final = Path(__file__).resolve().parents[1]
EXCEPTIONS_FILE: Final = BACKEND / "LICENSE-EXCEPTIONS.md"
LOCK_FILE: Final = BACKEND / "uv.lock"
ROOT_PACKAGE: Final = "vigia-platform"
CI_TOOL_SCOPE: Final = "herramienta de ci"
OWN_PACKAGES: Final = frozenset({"vigia-platform", "vigia-contracts"})
"""Paquetes del proyecto: su licencia es la propietaria del dueño."""

ALLOWED: Final = frozenset(
    {"MIT", "BSD-2-Clause", "BSD-3-Clause", "BSD", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0"}
)
"""Identificadores permitidos tras normalizar (``BSD``: clasificador sin variante)."""

ALIASES: Final[Mapping[str, str]] = {
    "mit": "MIT",
    "mit license": "MIT",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "bsd": "BSD",
    "bsd license": "BSD",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc": "ISC",
    "isc license (iscl)": "ISC",
    "psf-2.0": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "other/proprietary license": "LicenseRef-Proprietary",
}
"""Nombres que publica PyPI, en minúsculas, y su identificador SPDX."""

_TOKEN: Final = re.compile(r"\(|\)|[^\s()]+")
_ROW: Final = re.compile(r"^\|(?P<cells>.+)\|\s*$")
_EXCEPTION_ID: Final = re.compile(r"^EX-\d{2,}$")
_NAME_SEPARATORS: Final = re.compile(r"[-_.]+")


class CheckError(Exception):
    """El registro de excepciones o el bloqueo no se pudo leer (código 2)."""


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    version: str
    license: str


@dataclass(frozen=True, slots=True)
class LicenseException:
    identifier: str
    package: str
    license: str
    review: date
    scope: str = ""

    @property
    def ci_tool_only(self) -> bool:
        return self.scope.lower().startswith(CI_TOOL_SCOPE)


def normalize_name(name: str) -> str:
    """PEP 503: sin distinguir mayúsculas ni ``-``, ``_`` y ``.``."""
    return _NAME_SEPARATORS.sub("-", name).lower()


def normalize_license(identifier: str) -> str:
    cleaned = identifier.strip()
    return ALIASES.get(cleaned.lower(), cleaned)


# --- Expresiones -------------------------------------------------------------------------------


class _Parser:
    """Descenso recursivo de ``expr := term (OR term)*``, ``term := atom (AND atom)*``."""

    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = list(tokens)
        self._position = 0

    def parse(self) -> list[list[str]]:
        result = self._expression()
        if self._position != len(self._tokens):
            raise ValueError("sobran símbolos")
        return result

    def _peek(self) -> str | None:
        return self._tokens[self._position] if self._position < len(self._tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("expresión incompleta")
        self._position += 1
        return token

    def _expression(self) -> list[list[str]]:
        result = self._term()
        while self._peek() == "OR":
            self._take()
            result = result + self._term()
        return result

    def _term(self) -> list[list[str]]:
        result = self._atom()
        while self._peek() == "AND":
            self._take()
            right = self._atom()
            result = [left + other for left in result for other in right]
        return result

    def _atom(self) -> list[list[str]]:
        symbol = self._take()
        if symbol == "(":
            result = self._expression()
            if self._take() != ")":
                raise ValueError("paréntesis sin cerrar")
            return result
        if symbol in {")", "AND", "OR", "WITH"}:
            raise ValueError(f"símbolo inesperado {symbol!r}")
        if self._peek() == "WITH":
            self._take()
            return [[f"{symbol} WITH {self._take()}"]]
        return [[symbol]]


def license_leaves(text: str) -> list[list[str]]:
    """Forma normal disyuntiva: alternativas, cada una con las licencias que exige.

    Las partes separadas por ``;`` (clasificadores) se exigen todas. Una expresión mal formada es
    un único nombre desconocido, que no está permitido.
    """
    parts = [part.strip() for part in text.split(";") if part.strip()]
    if not parts:
        return [[""]]
    alternatives: list[list[str]] = [[]]
    for part in parts:
        parsed: list[list[str]]
        if re.search(r"\s(?:AND|OR|WITH)\s", part) or part.startswith("("):
            try:
                parsed = _Parser(_TOKEN.findall(part)).parse()
            except ValueError:
                parsed = [[part]]
        else:
            parsed = [[part]]
        alternatives = [left + right for left in alternatives for right in parsed]
    return [[normalize_license(leaf) for leaf in option] for option in alternatives]


# --- Entradas ----------------------------------------------------------------------------------


def declared_license(metadata: Any) -> str:
    """``License-Expression``; si no, los clasificadores ``License ::``; si no, ``License``."""
    expression = metadata.get("License-Expression")
    if expression:
        return str(expression).strip()
    classifiers = [
        str(c).split("::")[-1].strip()
        for c in metadata.get_all("Classifier") or []
        if str(c).startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    text = str(metadata.get("License") or "").strip()
    # Un campo con el texto entero de la licencia no es un identificador: cuenta como desconocido.
    return text if text and "\n" not in text and len(text) <= 64 else ""


def installed_dependencies() -> list[Dependency]:
    found = {}
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        name = str(metadata.get("Name") or "")
        if name:
            found[normalize_name(name)] = Dependency(
                name=name, version=distribution.version, license=declared_license(metadata)
            )
    return [found[key] for key in sorted(found)]


def load_exceptions(path: Path) -> list[LicenseException]:
    """Filas ``| EX-nn | paquete | licencia(s) | revisión | alcance | motivo |``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CheckError(f"no se pudo leer {path} ({error.strerror})") from None
    exceptions: list[LicenseException] = []
    for number, line in enumerate(text.splitlines(), start=1):
        row = _ROW.match(line.strip())
        if row is None:
            continue
        cells = [cell.strip().strip("`") for cell in row["cells"].split("|")]
        if not _EXCEPTION_ID.match(cells[0]):
            continue
        where = f"{path.name}:{number}"
        if len(cells) < 4:
            raise CheckError(f"{where}: la fila necesita id, paquete, licencia y revisión")
        identifier, package, licenses, review = cells[:4]
        names = [name.strip().strip("`") for name in licenses.split(",")]
        if not package or not all(names):
            raise CheckError(f"{where}: faltan el paquete o la licencia")
        try:
            review_date = date.fromisoformat(review)
        except ValueError:
            raise CheckError(f"{where}: fecha {review!r} no es AAAA-MM-DD") from None
        scope = cells[4] if len(cells) > 4 else ""
        exceptions.extend(
            LicenseException(
                identifier, normalize_name(package), normalize_license(n), review_date, scope
            )
            for n in names
        )
    return exceptions


def runtime_closure(lock_text: str) -> set[str]:
    """Paquetes que instala ``uv sync --no-dev``: el cierre de ``vigia-platform`` sin grupos."""
    try:
        lock = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as error:
        raise CheckError(f"uv.lock no es TOML: {error}") from None
    packages = {normalize_name(p["name"]): p for p in lock.get("package", []) if "name" in p}
    if ROOT_PACKAGE not in packages:
        raise CheckError(f"uv.lock no tiene el paquete {ROOT_PACKAGE}")
    visited: set[tuple[str, tuple[str, ...]]] = set()
    pending: list[tuple[str, tuple[str, ...]]] = [(ROOT_PACKAGE, ())]
    while pending:
        node = pending.pop()
        name, extras = node
        if node in visited or name not in packages:
            continue
        visited.add(node)
        package = packages[name]
        edges = list(package.get("dependencies", []))
        for extra in extras:
            edges.extend(package.get("optional-dependencies", {}).get(extra, []))
        pending.extend(
            (normalize_name(edge["name"]), tuple(sorted(edge.get("extra", ())))) for edge in edges
        )
    return {name for name, _ in visited}


# --- Verificación ------------------------------------------------------------------------------


def _satisfy(
    option: Sequence[str], candidates: Sequence[LicenseException]
) -> list[LicenseException] | None:
    needed = []
    for leaf in option:
        if leaf in ALLOWED:
            continue
        exception = next((e for e in candidates if e.license == leaf), None)
        if exception is None:
            return None
        needed.append(exception)
    return needed


def check(
    dependencies: Iterable[Dependency],
    exceptions: Sequence[LicenseException],
    runtime: set[str],
    today: date,
) -> tuple[list[str], list[str]]:
    """Problemas (fallan la orden) y excepciones aplicadas (se informan)."""
    problems: list[str] = []
    applied: list[str] = []
    for dependency in dependencies:
        name = normalize_name(dependency.name)
        if name in OWN_PACKAGES:
            continue
        candidates = [e for e in exceptions if e.package == name]
        admissible = [
            needed
            for option in license_leaves(dependency.license)
            if (needed := _satisfy(option, candidates)) is not None
        ]
        label = f"{dependency.name} {dependency.version}"
        if not admissible:
            shown = dependency.license or "sin licencia declarada"
            problems.append(f"{label}: licencia no permitida ({shown})")
            continue
        for exception in min(admissible, key=len):
            if exception.review < today:
                problems.append(
                    f"{label}: la excepción {exception.identifier} ({exception.license}) venció"
                    f" el {exception.review.isoformat()}; revísela en LICENSE-EXCEPTIONS.md"
                )
            elif exception.ci_tool_only and name in runtime:
                problems.append(
                    f"{label}: la excepción {exception.identifier} solo vale como herramienta de"
                    " CI y el paquete es una dependencia de ejecución de la imagen"
                )
            else:
                applied.append(
                    f"{label}: {exception.license} por la excepción {exception.identifier}"
                    f" (revisión {exception.review.isoformat()})"
                )
    return problems, applied


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aplica la lista de licencias permitidas a las dependencias instaladas."
    )
    parser.add_argument("--exceptions", type=Path, default=EXCEPTIONS_FILE, metavar="ARCHIVO")
    parser.add_argument("--lock", type=Path, default=LOCK_FILE, metavar="ARCHIVO")
    parser.add_argument("--today", type=date.fromisoformat, metavar="AAAA-MM-DD")
    args = parser.parse_args(argv)
    today = args.today or date.today()  # noqa: DTZ011, TID251 - herramienta de CI, no el núcleo
    try:
        exceptions = load_exceptions(args.exceptions)
        try:
            runtime = runtime_closure(args.lock.read_text(encoding="utf-8"))
        except OSError as error:
            raise CheckError(f"no se pudo leer {args.lock} ({error.strerror})") from None
    except CheckError as error:
        print(f"check_licenses: {error}", file=sys.stderr)
        return 2
    dependencies = installed_dependencies()
    problems, applied = check(dependencies, exceptions, runtime, today)
    for line in applied:
        print(f"  excepción  {line}")
    for line in problems:
        print(f"  FALLA      {line}", file=sys.stderr)
    summary = f"{len(dependencies)} distribuciones ({len(runtime)} de ejecución)"
    if problems:
        print(f"check_licenses: {len(problems)} problema(s) en {summary}", file=sys.stderr)
        return 1
    print(f"check_licenses: {summary} con licencia permitida")
    return 0


if __name__ == "__main__":
    sys.exit(main())
