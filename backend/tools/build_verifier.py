"""Genera el verificador de paquetes de un archivo ``tools/vigia_verify.py`` (LC-NUC-18).

PAT-NUC-MAN-08: concatena, en este orden, los módulos puros de ``vigia_platform.ledger.chain``
(``pure_rfc8785``, ``pure_ed25519``, ``chain_walk`` y ``package_verifier``) en un solo archivo
para Python 3.10 o superior sin dependencias, que U-04 incluye en cada paquete (NFR-NUC-53).
La plataforma usa esos mismos módulos, así que plataforma y verificador comparten el código.

La construcción es por árbol sintáctico, no por texto, y **falla** si un módulo rompe las reglas
del verificador:

- solo importa la biblioteca estándar (``sys.stdlib_module_names``) o un módulo anterior de la
  lista (``from vigia_platform.ledger.chain.<módulo> import ...``);
- ningún nombre de primer nivel se define en dos módulos (en el archivo único chocarían).

Las importaciones de la biblioteca estándar suben a la cabecera, sin repetir; las de un módulo
anterior y los ``__all__`` desaparecen (sus nombres ya están definidos en el archivo). El resto
del código se copia tal cual, docstrings y comentarios incluidos.

Uso:

- ``uv run python tools/build_verifier.py``: escribe ``tools/vigia_verify.py`` e imprime su SHA-256
  (el que se publica en ``/.well-known/vigia-verifier`` y dentro de cada paquete).
- ``uv run python tools/build_verifier.py --check``: regenera en memoria y termina en 1 si el
  archivo versionado difiere (por ejemplo, porque se editó a mano o porque cambió un módulo y no
  se regeneró); ``tests/unit/test_build_verifier.py`` lo ejecuta en cada corrida.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PACKAGE = "vigia_platform.ledger.chain"
SOURCE_DIRECTORY = BACKEND / "src" / "vigia_platform" / "ledger" / "chain"
MODULES = ("pure_rfc8785", "pure_ed25519", "chain_walk", "package_verifier")
"""Módulos puros en orden de dependencia: cada uno solo importa los anteriores."""
OUTPUT = BACKEND / "tools" / "vigia_verify.py"

HEADER = '''\
#!/usr/bin/env python3
# ARCHIVO GENERADO por tools/build_verifier.py: no se edita a mano.
# Fuente: vigia_platform.ledger.chain ({modules}).
# `uv run python tools/build_verifier.py --check` falla si difiere de lo generado.
"""Verificador de paquetes de Vigía (LC-NUC-18, BR-NUC-57, NFR-NUC-53).

Un solo archivo para Python 3.10 o superior, sin dependencias: verifica sin red, sin acceso a la
plataforma y sin ningún secreto del proveedor los hashes, los enlaces de cada cadena y las firmas
Ed25519 de los puntos de control de un paquete exportado. Formato del paquete:
``docs/package-format.md`` del repositorio ``vigia-platform``.

Uso: ``python vigia_verify.py <paquete> [--previous-checkpoint RUTA ...] [--out resultado.json]``.
Código de salida: 0 si el paquete está íntegro; 1 si está roto o no se puede leer; 2 si la orden
es incorrecta. ``python vigia_verify.py --help`` muestra la ayuda.
"""

'''


class BuildError(Exception):
    """Un módulo rompe las reglas del verificador de un archivo."""


@dataclass(frozen=True)
class _Module:
    name: str
    source: str
    tree: ast.Module


def _read(name: str, directory: Path) -> _Module:
    path = directory / f"{name}.py"
    source = path.read_text(encoding="utf-8")
    return _Module(name, source, ast.parse(source, filename=str(path)))


def _top_level_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _is_dunder_all(node: ast.stmt) -> bool:
    return "__all__" in _top_level_names(node)


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _check_import(module: _Module, node: ast.Import | ast.ImportFrom, earlier: set[str]) -> None:
    if isinstance(node, ast.ImportFrom):
        if node.level:
            raise BuildError(f"{module.name}: importación relativa")
        if node.module == "__future__":
            return
        if node.module is not None and node.module.startswith(f"{PACKAGE}."):
            source = node.module.removeprefix(f"{PACKAGE}.")
            if source not in earlier:
                raise BuildError(f"{module.name}: importa {node.module}, que no va antes")
            if any(alias.asname is not None or alias.name == "*" for alias in node.names):
                raise BuildError(f"{module.name}: importa de {source} con alias o con *")
            return
        roots = [node.module or ""]
    else:
        roots = [alias.name for alias in node.names]
    for root in roots:
        top = root.split(".")[0]
        if top not in sys.stdlib_module_names:
            raise BuildError(f"{module.name}: importa {root!r}, que no es biblioteca estándar")


def _is_internal(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.module is not None
        and (node.module == "__future__" or node.module.startswith(f"{PACKAGE}."))
    )


def _alias(alias: ast.alias) -> str:
    return alias.name if alias.asname is None else f"{alias.name} as {alias.asname}"


@dataclass
class _Imports:
    """Importaciones de la biblioteca estándar de todos los módulos, sin repetir."""

    plain: set[str]
    names: dict[str, set[str]]

    def add(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            self.plain.update(_alias(alias) for alias in node.names)
        else:
            self.names.setdefault(node.module or "", set()).update(map(_alias, node.names))

    def render(self) -> str:
        lines = ["from __future__ import annotations", ""]
        lines += [f"import {name}" for name in sorted(self.plain)]
        lines += [
            f"from {module} import {', '.join(sorted(names))}"
            for module, names in sorted(self.names.items())
        ]
        return "\n".join(lines)


def build(directory: Path = SOURCE_DIRECTORY, modules: Sequence[str] = MODULES) -> str:
    """Texto de ``vigia_verify.py`` a partir de los módulos de ``directory``."""
    stdlib_imports = _Imports(set(), {})
    bodies: list[str] = []
    defined: dict[str, str] = {}
    earlier: set[str] = set()
    for module in (_read(name, directory) for name in modules):
        lines = module.source.splitlines()
        dropped: set[int] = set()
        for position, node in enumerate(module.tree.body):
            span = range(node.lineno - 1, (node.end_lineno or node.lineno))
            if isinstance(node, ast.Import | ast.ImportFrom):
                _check_import(module, node, earlier)
                if not _is_internal(node):
                    stdlib_imports.add(node)
                dropped.update(span)
                continue
            if position == 0 and _is_docstring(node):
                continue
            if _is_dunder_all(node):
                dropped.update(span)
                continue
            for name in _top_level_names(node):
                if name in defined and name != "__all__":
                    raise BuildError(f"{module.name}: {name!r} ya está definido en {defined[name]}")
                defined[name] = module.name
        kept = [line for index, line in enumerate(lines) if index not in dropped]
        text = "\n".join(kept).strip("\n")
        # Una línea en blanco de sobra tras quitar importaciones: se compactan las series de 3+.
        while "\n\n\n\n" in text:
            text = text.replace("\n\n\n\n", "\n\n\n")
        bodies.append(f"# {'-' * 20} {PACKAGE}.{module.name} {'-' * 20}\n\n{text}\n")
        earlier.add(module.name)
    header = HEADER.format(modules=", ".join(modules))
    return header + stdlib_imports.render() + "\n\n\n" + "\n\n".join(bodies)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera tools/vigia_verify.py (LC-NUC-18).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="no escribe: termina en 1 si el archivo versionado difiere de lo generado",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        generated = build()
    except BuildError as error:
        print(f"build_verifier: {error}", file=sys.stderr)
        return 1
    output: Path = arguments.output
    if arguments.check:
        # Byte a byte: el SHA-256 publicado es el de los bytes (un CRLF también cuenta).
        current = output.read_bytes() if output.exists() else b""
        if current != generated.encode("utf-8"):
            diff = difflib.unified_diff(
                current.decode("utf-8", "replace").splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile=f"{output.name} (versionado)",
                tofile=f"{output.name} (generado)",
                n=1,
            )
            sys.stderr.writelines(list(diff)[:60])
            print(
                f"build_verifier: {output.name} no coincide con lo generado; "
                "ejecuta `uv run python tools/build_verifier.py`",
                file=sys.stderr,
            )
            return 1
        print(f"build_verifier: {output.name} al día (sha256 {sha256_text(generated)})")
        return 0
    output.write_text(generated, encoding="utf-8", newline="\n")
    print(f"build_verifier: escrito {output.name} (sha256 {sha256_text(generated)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
