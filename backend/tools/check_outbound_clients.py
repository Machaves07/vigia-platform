"""Metaprueba estática de la fábrica única de clientes de salida de U-04 (PR-LAZ-54; LC-LAZ-39).

Recorre el árbol de módulos de U-04 (``loop``, ``accreditation`` y ``notifications``) y falla si
alguno **construye un cliente de salida fuera de** ``loop/http/client_factory.py``:

- ``httpx.AsyncClient`` o ``httpx.Client``, con cualquier alias (``import httpx as h``,
  ``from httpx import AsyncClient as C``);
- un cliente de SES de boto3: ``<algo>.client("ses" | "sesv2")`` (también con
  ``service_name=``), y todo ``boto3.client(...)`` o ``Session(...).client(...)`` de boto3 cuyo
  servicio no es un literal (fallo cerrado: no se puede saber que no es SES);
- el cliente del proveedor del asistente: cualquier llamada a ``AsyncAnthropic`` o ``Anthropic``,
  importada o no (un reexport tampoco la oculta).

Es un análisis del árbol sintáctico, sin importar los módulos: un texto o un comentario que diga
``httpx.AsyncClient()`` no cuenta. ``tests/properties/laz/test_pr_laz_54_http_factory.py`` lo
ejecuta sobre el árbol real y sobre construcciones generadas.

Uso: ``uv run python tools/check_outbound_clients.py [raíz de src]``.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

U04_PACKAGES: Final = ("loop", "accreditation", "notifications")
FACTORY_MODULE: Final = Path("vigia_platform", "loop", "http", "client_factory.py")
DEFAULT_SOURCE_ROOT: Final = Path(__file__).resolve().parents[1] / "src"

_HTTPX_CLIENTS: Final = frozenset({"httpx.AsyncClient", "httpx.Client"})
_PROVIDER_CLIENTS: Final = frozenset({"AsyncAnthropic", "Anthropic"})
_SES_SERVICES: Final = frozenset({"ses", "sesv2"})
_BOTO3_CLIENT_FACTORIES: Final = frozenset({"boto3.client"})
_BOTO3_SESSIONS: Final = frozenset({"boto3.Session", "boto3.session.Session"})


@dataclass(frozen=True, slots=True)
class Violation:
    """Una construcción de cliente fuera de la fábrica."""

    path: str
    line: int
    construction: str


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.found: list[Violation] = []

    # --- Importaciones ------------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname is not None:
                self.aliases[alias.asname] = alias.name
            else:
                head = alias.name.split(".")[0]
                self.aliases[head] = head
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is not None and node.level == 0:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    # --- Llamadas -----------------------------------------------------------------------------

    def _dotted(self, node: ast.expr) -> str | None:
        """Nombre resuelto de ``node`` (``h.AsyncClient`` → ``httpx.AsyncClient``)."""
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            called = self._dotted(node.func)
            return None if called is None else f"{called}()"
        return None

    @staticmethod
    def _service(node: ast.Call) -> tuple[bool, str | None]:
        """(``True`` si el servicio es un literal, su valor)."""
        argument: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "service_name":
                argument = keyword.value
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return True, argument.value
        return False, None

    def _report(self, node: ast.Call, construction: str) -> None:
        self.found.append(Violation(self.path, node.lineno, construction))

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = self._dotted(func)
        last = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _HTTPX_CLIENTS:
            self._report(node, name)
        elif last in _PROVIDER_CLIENTS or (
            name is not None and name.split(".")[-1] in _PROVIDER_CLIENTS
        ):
            self._report(node, str(last))
        elif last == "client" or name in _BOTO3_CLIENT_FACTORIES:
            literal, service = self._service(node)
            from_boto3 = name in _BOTO3_CLIENT_FACTORIES or (
                isinstance(func, ast.Attribute)
                and (
                    (self._dotted(func.value) or "").removesuffix("()") in _BOTO3_SESSIONS
                    or (self._dotted(func.value) or "") == "boto3"
                )
            )
            if literal and service in _SES_SERVICES:
                self._report(node, f"client({service!r})")
            elif from_boto3 and not literal:
                self._report(node, "boto3 client() con servicio no literal")
        self.generic_visit(node)


def scan_source(source: str, path: str) -> list[Violation]:
    """Las construcciones de cliente de ``source`` (``path`` solo nombra el resultado)."""
    scanner = _Scanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.found


def u04_modules(source_root: Path) -> Iterable[Path]:
    """Los módulos de U-04 bajo ``source_root`` salvo la fábrica."""
    for package in U04_PACKAGES:
        for path in sorted((source_root / "vigia_platform" / package).rglob("*.py")):
            if path.relative_to(source_root) != FACTORY_MODULE:
                yield path


def scan_tree(source_root: Path = DEFAULT_SOURCE_ROOT) -> list[Violation]:
    """Toda construcción de cliente de salida en U-04 fuera de la fábrica."""
    found: list[Violation] = []
    for path in u04_modules(source_root):
        relative = path.relative_to(source_root).as_posix()
        found.extend(scan_source(path.read_text(encoding="utf-8"), relative))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    root = Path(arguments[0]) if arguments else DEFAULT_SOURCE_ROOT
    found = scan_tree(root)
    print(json.dumps([asdict(item) for item in found], ensure_ascii=False, indent=2))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
