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

**Composición de U-04** (TASK-346, LC-LAZ-25): el verificador que viaja dentro de cada paquete
exportado, ``resources/verifier/vigia_verify.py``, es el mismo archivo único con un módulo puro
más detrás de los cuatro de U-02: ``vigia_platform.accreditation.export_verifier`` (el
endurecimiento de PAT-LAZ-SEG-02). Valen para él las mismas reglas (solo biblioteca estándar o
módulos anteriores de la lista, sin nombres repetidos) y solo el último módulo conserva su bloque
``if __name__ == "__main__"``. ``tools/vigia_verify.py`` no cambia: sus bytes son los de antes.

Uso:

- ``uv run python tools/build_verifier.py``: escribe ``tools/vigia_verify.py`` y
  ``resources/verifier/vigia_verify.py`` e imprime sus SHA-256 (el primero es el que se publica en
  ``/.well-known/vigia-verifier``; el segundo, el que viaja en cada paquete exportado).
- ``uv run python tools/build_verifier.py --check``: regenera en memoria y termina en 1 si alguno
  de los dos archivos versionados difiere (por ejemplo, porque se editó a mano o porque cambió un
  módulo y no se regeneró); ``tests/unit/test_build_verifier.py`` lo ejecuta en cada corrida.
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
BUNDLE_OUTPUT = BACKEND / "resources" / "verifier" / "vigia_verify.py"
"""El verificador que viaja dentro de cada paquete exportado de U-04 (LC-LAZ-25)."""
ACCREDITATION_PACKAGE = "vigia_platform.accreditation"
ACCREDITATION_DIRECTORY = BACKEND / "src" / "vigia_platform" / "accreditation"
BUNDLE_MODULES = ("export_verifier",)
"""Módulos puros de U-04 que van detrás de los de U-02 en el verificador del paquete."""

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

BUNDLE_HEADER = '''\
#!/usr/bin/env python3
# ARCHIVO GENERADO por tools/build_verifier.py: no se edita a mano.
# Fuente: {modules}.
# `uv run python tools/build_verifier.py --check` falla si difiere de lo generado.
"""Verificador de paquetes exportados de Vigía (LC-LAZ-25, LC-NUC-18, NFR-LAZ-53, NFR-NUC-53).

Un solo archivo para Python 3.10 o superior, sin dependencias, que viaja dentro de cada paquete
exportado: verifica sin red, sin acceso a la plataforma y sin ningún secreto del proveedor que el
paquete es el que la plataforma produjo. Rechaza rutas con «..», rutas absolutas, enlaces
simbólicos, expansiones de más de 100 veces o de más de 2 GB y paquetes con demasiadas entradas;
comprueba el hash de cada artefacto **antes de extraer nada**, las firmas de los puntos de
control con las claves públicas incluidas y las cadenas.

Uso: ``python vigia_verify.py <paquete> [--manifest-sha256 HEX] [--extract DIRECTORIO]
[--out resultado.json]``, con ``<paquete>`` el directorio de ``vol-01.zip``, ``vol-02.zip``… o la
ruta de ``vol-01.zip``. Código de salida: 0 si el paquete está íntegro; 1 si está roto o no se
puede leer; 2 si la orden es incorrecta. ``python vigia_verify.py --help`` muestra la ayuda.
"""

'''


class BuildError(Exception):
    """Un módulo rompe las reglas del verificador de un archivo."""


@dataclass(frozen=True)
class SourceModule:
    """Un módulo puro de la composición: su paquete, su directorio y su nombre."""

    package: str
    directory: Path
    name: str

    @property
    def dotted(self) -> str:
        return f"{self.package}.{self.name}"


@dataclass(frozen=True)
class _Module:
    name: str
    dotted: str
    source: str
    tree: ast.Module


def _read(module: SourceModule) -> _Module:
    path = module.directory / f"{module.name}.py"
    source = path.read_text(encoding="utf-8")
    return _Module(module.name, module.dotted, source, ast.parse(source, filename=str(path)))


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


def _is_main_guard(node: ast.stmt) -> bool:
    """``if __name__ == "__main__":`` de primer nivel."""
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _check_import(module: _Module, node: ast.Import | ast.ImportFrom, earlier: set[str]) -> None:
    if isinstance(node, ast.ImportFrom):
        if node.level:
            raise BuildError(f"{module.name}: importación relativa")
        if node.module == "__future__":
            return
        if node.module is not None and node.module.startswith("vigia_platform."):
            source = node.module.rsplit(".", 1)[-1]
            if node.module not in earlier:
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
        and (node.module == "__future__" or node.module.startswith("vigia_platform."))
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


def build(
    directory: Path = SOURCE_DIRECTORY,
    modules: Sequence[str] = MODULES,
    *,
    extra: Sequence[SourceModule] = (),
    header: str = HEADER,
) -> str:
    """Texto de ``vigia_verify.py`` a partir de los módulos de ``directory`` y, detrás, ``extra``.

    Solo el último módulo conserva su ``if __name__ == "__main__"``.
    """
    sources = [SourceModule(PACKAGE, directory, name) for name in modules] + list(extra)
    stdlib_imports = _Imports(set(), {})
    bodies: list[str] = []
    defined: dict[str, str] = {}
    earlier: set[str] = set()
    for index, module in enumerate(_read(source) for source in sources):
        last = index == len(sources) - 1
        lines = module.source.splitlines()
        dropped: set[int] = set()
        for position, node in enumerate(module.tree.body):
            span = range(node.lineno - 1, (node.end_lineno or node.lineno))
            if not last and _is_main_guard(node):
                dropped.update(span)
                continue
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
        bodies.append(f"# {'-' * 20} {module.dotted} {'-' * 20}\n\n{text}\n")
        earlier.add(module.dotted)
    names = ", ".join(modules)
    if extra:
        groups: dict[str, list[str]] = {}
        for source in sources:
            groups.setdefault(source.package, []).append(source.name)
        names = "; ".join(f"{package} ({', '.join(items)})" for package, items in groups.items())
    return header.format(modules=names) + stdlib_imports.render() + "\n\n\n" + "\n\n".join(bodies)


def build_bundle() -> str:
    """El verificador que viaja en cada paquete exportado: U-02 más ``export_verifier``."""
    extra = [
        SourceModule(ACCREDITATION_PACKAGE, ACCREDITATION_DIRECTORY, name)
        for name in BUNDLE_MODULES
    ]
    return build(extra=extra, header=BUNDLE_HEADER)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _label(output: Path) -> str:
    """``tools/vigia_verify.py`` o ``verifier/vigia_verify.py``: los dos se llaman igual."""
    return f"{output.parent.name}/{output.name}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera tools/vigia_verify.py (LC-NUC-18).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="no escribe: termina en 1 si un archivo versionado difiere de lo generado",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT, help=argparse.SUPPRESS)
    parser.add_argument("--bundle-output", type=Path, default=BUNDLE_OUTPUT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        targets: list[tuple[Path, str]] = [
            (arguments.output, build()),
            (arguments.bundle_output, build_bundle()),
        ]
    except BuildError as error:
        print(f"build_verifier: {error}", file=sys.stderr)
        return 1
    status = 0
    for output, generated in targets:
        label = _label(output)
        if not arguments.check:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(generated, encoding="utf-8", newline="\n")
            print(f"build_verifier: escrito {label} (sha256 {sha256_text(generated)})")
            continue
        # Byte a byte: el SHA-256 publicado es el de los bytes (un CRLF también cuenta).
        current = output.read_bytes() if output.exists() else b""
        if current != generated.encode("utf-8"):
            diff = difflib.unified_diff(
                current.decode("utf-8", "replace").splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile=f"{label} (versionado)",
                tofile=f"{label} (generado)",
                n=1,
            )
            sys.stderr.writelines(list(diff)[:60])
            print(
                f"build_verifier: {label} no coincide con lo generado; "
                "ejecuta `uv run python tools/build_verifier.py`",
                file=sys.stderr,
            )
            status = 1
            continue
        print(f"build_verifier: {label} al día (sha256 {sha256_text(generated)})")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
