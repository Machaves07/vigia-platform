"""Reglas de lint propias de vigia-platform, donde ruff no alcanza.

Ruff ya bloquea ``pickle``, ``marshal``, ``yaml.load`` inseguro y ``eval`` (S301, S302, S307,
S506 y TID251), los ``datetime`` sin zona (DTZ) y la hora del sistema fuera de
``vigia_platform.shared.clock`` (TID251 con excepción por archivo). Este módulo añade lo que ruff
no expresa:

- ``VIG001`` — ``text()`` con f-string, ``%``, ``.format()`` o concatenación (SQL por formato,
  NFR-NUC-19). Las sentencias van con parámetros: ``text("... :x").bindparams(x=...)``.
- ``VIG002`` — cliente o petición de httpx sin ``timeout=`` (PAT-NUC-RES-03, NFR-NUC-36).
- ``VIG003`` — cliente o recurso de boto3 sin ``config=`` o ``botocore.config.Config`` sin
  ``connect_timeout`` y ``read_timeout`` (PAT-NUC-RES-03, NFR-NUC-36).
- ``VIG004`` — primer argumento no constante en ``get_logger(...)``, en
  ``log.debug/info/warning/error/exception/critical(...)`` sobre un registrador de
  ``get_logger`` y en ``start_as_current_span``, ``start_span``, ``update_name`` y ``add_event``
  (NFR-NUC-17, NFR-NUC-41, PR-NUC-54). Vale un literal de cadena o una constante: un nombre del
  módulo anotado ``Final`` o un nombre en mayúsculas importado (``from m import NOMBRE`` o
  ``m.NOMBRE``). Un f-string, una concatenación, una variable o un parámetro no valen: por ahí
  entran texto libre y secretos. En ejecución, ``shared.observability`` lo vuelve a comprobar.

Una línea con ``# noqa: VIGnnn`` omite esa regla en esa línea (solo para pruebas hostiles que
inyectan datos a propósito, con el motivo en el comentario).

Uso: ``uv run python tools/lint_rules.py [ruta ...]`` (por defecto ``src``, ``tests`` y
``tools``). Imprime ``archivo:línea:columna: VIGnnn mensaje`` y termina en 1 si hay alguna
violación. ``tests/unit/test_lint_rules.py`` lo ejecuta sobre el árbol en cada corrida de pytest.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Violation", "check_paths", "check_source", "main"]

DEFAULT_PATHS = ("src", "tests", "tools")

SQL_TEXT_FUNCTIONS = frozenset(
    {"sqlalchemy.text", "sqlalchemy.sql.text", "sqlalchemy.sql.expression.text"}
)
"""Nombres calificados de ``text()``; un ``text`` sin importar también cuenta (conservador)."""

HTTPX_CALLS = frozenset(
    {
        "httpx.Client",
        "httpx.AsyncClient",
        "httpx.request",
        "httpx.stream",
        "httpx.get",
        "httpx.options",
        "httpx.head",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
    }
)
BOTO3_FACTORIES = frozenset({"boto3.client", "boto3.resource"})
BOTO3_SESSION_METHODS = frozenset({"client", "resource"})
BOTOCORE_CONFIG = frozenset({"botocore.config.Config", "botocore.client.Config"})
BOTOCORE_TIMEOUT_KEYWORDS = ("connect_timeout", "read_timeout")
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
SPAN_METHODS = frozenset({"start_as_current_span", "start_span", "update_name", "add_event"})
_NOQA = re.compile(r"#\s*noqa:\s*(?P<codes>[A-Z0-9, ]+)")
_CONSTANT_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Violation:
    """Una violación con su regla, posición y mensaje."""

    filename: str
    line: int
    column: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}:{self.column}: {self.rule} {self.message}"


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Nombre local → nombre calificado, según las importaciones del módulo."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    head = alias.name.split(".")[0]
                    aliases[head] = head
                else:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _qualified_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in aliases:
        return None
    return ".".join([aliases[node.id], *reversed(parts)])


def _is_formatted_string(node: ast.expr) -> bool:
    """f-string, ``%``, ``.format()`` o concatenación con ``+``."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    )


def _is_sql_text_call(call: ast.Call, aliases: dict[str, str]) -> bool:
    qualified = _qualified_name(call.func, aliases)
    if qualified is not None:
        return qualified in SQL_TEXT_FUNCTIONS
    # ``text`` sin importación resoluble (p. ej. con ``import *``): se trata como el de SQLAlchemy.
    return isinstance(call.func, ast.Name) and call.func.id == "text"


def _first_argument(call: ast.Call, keyword: str) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name or kw.arg is None for kw in call.keywords)


def _check_call(call: ast.Call, aliases: dict[str, str], filename: str) -> list[Violation]:
    found: list[Violation] = []
    if _is_sql_text_call(call, aliases):
        argument = _first_argument(call, "text")
        if argument is not None and _is_formatted_string(argument):
            found.append(
                Violation(
                    filename,
                    argument.lineno,
                    argument.col_offset + 1,
                    "VIG001",
                    "text() con f-string, formato o concatenación (SQL por formato, NFR-NUC-19); "
                    "usa parámetros: text('... :x').bindparams(x=...)",
                )
            )
    qualified = _qualified_name(call.func, aliases)
    if qualified in HTTPX_CALLS and not _has_keyword(call, "timeout"):
        found.append(
            Violation(
                filename,
                call.lineno,
                call.col_offset + 1,
                "VIG002",
                f"{qualified}() sin timeout= (PAT-NUC-RES-03, NFR-NUC-36)",
            )
        )
    if qualified in BOTO3_FACTORIES and not _has_keyword(call, "config"):
        found.append(
            Violation(
                filename,
                call.lineno,
                call.col_offset + 1,
                "VIG003",
                f"{qualified}() sin config=botocore.config.Config(connect_timeout=..., "
                "read_timeout=...) (PAT-NUC-RES-03, NFR-NUC-36)",
            )
        )
    elif (
        qualified is None
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in BOTO3_SESSION_METHODS
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and not _has_keyword(call, "config")
    ):
        # ``session.client("s3")`` de una ``boto3.Session``: mismo requisito que la fábrica.
        found.append(
            Violation(
                filename,
                call.lineno,
                call.col_offset + 1,
                "VIG003",
                f".{call.func.attr}({call.args[0].value!r}) sin config=botocore.config.Config("
                "connect_timeout=..., read_timeout=...) (PAT-NUC-RES-03, NFR-NUC-36)",
            )
        )
    if qualified in BOTOCORE_CONFIG:
        missing = [k for k in BOTOCORE_TIMEOUT_KEYWORDS if not _has_keyword(call, k)]
        if missing:
            found.append(
                Violation(
                    filename,
                    call.lineno,
                    call.col_offset + 1,
                    "VIG003",
                    f"{qualified}() sin {' ni '.join(missing)} (PAT-NUC-RES-03, NFR-NUC-36)",
                )
            )
    return found


def _is_get_logger(node: ast.expr, aliases: dict[str, str]) -> bool:
    qualified = _qualified_name(node, aliases)
    if qualified is not None:
        return qualified == "get_logger" or qualified.endswith(".get_logger")
    return isinstance(node, ast.Name) and node.id == "get_logger"


def _target_key(node: ast.expr) -> str | None:
    """``log`` para un nombre, ``self.log`` para un atributo de un nombre."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _logger_targets(tree: ast.AST, aliases: dict[str, str]) -> set[str]:
    """Nombres y atributos a los que se asigna el resultado de ``get_logger(...)``."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        names: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, names = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value, names = node.value, [node.target]
        if isinstance(value, ast.Call) and _is_get_logger(value.func, aliases):
            targets.update(key for name in names if (key := _target_key(name)) is not None)
    return targets


def _is_final(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    if isinstance(annotation, ast.Name):
        return annotation.id == "Final"
    return isinstance(annotation, ast.Attribute) and annotation.attr == "Final"


def _constants(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Constantes ``Final`` del módulo y nombres en mayúsculas importados; módulos importados."""
    constants: set[str] = set()
    modules: set[str] = set()
    for statement in tree.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and _is_final(statement.annotation)
        ):
            constants.add(statement.target.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                if _CONSTANT_NAME.fullmatch(alias.name):
                    constants.add(local)
                else:
                    modules.add(local)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.asname or alias.name.split(".")[0])
    return constants, modules


def _is_constant_argument(node: ast.expr, constants: set[str], modules: set[str]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.Name):
        return node.id in constants
    return (
        isinstance(node, ast.Attribute)
        and _CONSTANT_NAME.fullmatch(node.attr) is not None
        and isinstance(node.value, ast.Name)
        and node.value.id in modules
    )


def _check_constant_names(
    tree: ast.Module, aliases: dict[str, str], filename: str
) -> list[Violation]:
    loggers = _logger_targets(tree, aliases)
    constants, modules = _constants(tree)
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        what: str | None = None
        argument: ast.expr | None = None
        func = node.func
        if _is_get_logger(func, aliases):
            what, argument = "get_logger", _first_argument(node, "component")
        elif isinstance(func, ast.Attribute) and func.attr in SPAN_METHODS:
            what, argument = func.attr, _first_argument(node, "name")
        elif isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
            receiver = func.value
            is_logger = _target_key(receiver) in loggers or (
                isinstance(receiver, ast.Call) and _is_get_logger(receiver.func, aliases)
            )
            if is_logger:
                what, argument = func.attr, _first_argument(node, "message")
        if what is None or argument is None:
            continue
        if not _is_constant_argument(argument, constants, modules):
            found.append(
                Violation(
                    filename,
                    argument.lineno,
                    argument.col_offset + 1,
                    "VIG004",
                    f"{what}() con primer argumento no constante: usa un literal o una constante"
                    " Final; por ahí entran texto libre y secretos (NFR-NUC-17, PR-NUC-54)",
                )
            )
    return found


def _suppressed(source: str) -> dict[int, set[str]]:
    lines: dict[int, set[str]] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        match = _NOQA.search(line)
        if match:
            lines[number] = {code.strip() for code in match["codes"].split(",")}
    return lines


def check_source(source: str, filename: str) -> list[Violation]:
    """Violaciones de un módulo, ordenadas por posición (sin las omitidas con ``# noqa``)."""
    tree = ast.parse(source, filename=filename)
    aliases = _aliases(tree)
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.extend(_check_call(node, aliases, filename))
    found.extend(_check_constant_names(tree, aliases, filename))
    suppressed = _suppressed(source)
    kept = [v for v in found if v.rule not in suppressed.get(v.line, set())]
    return sorted(kept, key=lambda v: (v.line, v.column, v.rule))


def _python_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.py") if ".venv" not in p.parts)


def check_paths(paths: list[Path], *, root: Path | None = None) -> list[Violation]:
    """Violaciones de todos los archivos ``.py`` bajo ``paths``; rutas relativas a ``root``."""
    violations: list[Violation] = []
    for path in paths:
        for file in _python_files(path):
            display = file
            if root is not None and file.resolve().is_relative_to(root.resolve()):
                display = file.resolve().relative_to(root.resolve())
            violations += check_source(file.read_text(encoding="utf-8"), display.as_posix())
    return violations


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: imprime las violaciones y devuelve 1 si hay alguna."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(a) for a in arguments] or [Path(p) for p in DEFAULT_PATHS if Path(p).exists()]
    violations = check_paths(paths, root=Path.cwd())
    for violation in violations:
        print(violation)
    if violations:
        print(f"lint_rules: {len(violations)} violación(es)", file=sys.stderr)
        return 1
    print(f"lint_rules: sin violaciones en {', '.join(p.as_posix() for p in paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
