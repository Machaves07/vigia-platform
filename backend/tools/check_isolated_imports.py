"""Prueba de importación aislada de los módulos críticos (NFR-NUC-25, SECURITY-11).

``identity.auth``, ``identity.authz``, ``ledger.chain``, ``shared.signing`` y ``shared.crypto``
deben importarse sin FastAPI ni SQLAlchemy. Este guion lo demuestra en un intérprete nuevo:

1. Instala en ``sys.meta_path[0]`` un buscador que hace fallar con ``ImportError`` cualquier
   importación de los paquetes bloqueados y de sus submódulos (``fastapi``, ``starlette``,
   ``sqlalchemy``, ``asyncpg``, ``alembic``), estén o no instalados en el entorno.
2. Comprueba que el bloqueo es real: importar un paquete bloqueado falla.
3. Importa cada módulo crítico y confirma que ninguno de los paquetes bloqueados aparece en
   ``sys.modules`` al terminar.

Es una prueba real porque el bloqueo actúa antes que cualquier buscador de ``site-packages``:
una importación directa o transitiva (también dentro de una función ejecutada al importar)
termina en ``ImportError`` y hace fallar el guion. Se ejecuta siempre en un subproceso para que
no haya módulos ya cargados. ``tests/unit/test_critical_modules_isolation.py`` lo lanza.

Uso: ``uv run python tools/check_isolated_imports.py [--modules m ...] [--blocked p ...]``.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.abc
import importlib.machinery
import json
import sys
from collections.abc import Sequence
from types import ModuleType

CRITICAL_MODULES: tuple[str, ...] = (
    "vigia_platform.identity.auth",
    "vigia_platform.identity.authz",
    "vigia_platform.ledger.chain",
    "vigia_platform.shared.signing",
    "vigia_platform.shared.crypto",
)
BLOCKED_PACKAGES: tuple[str, ...] = ("fastapi", "starlette", "sqlalchemy", "asyncpg", "alembic")


class BlockedImportError(ImportError):
    """Importación de un paquete bloqueado por la prueba de aislamiento."""


class BlockingFinder(importlib.abc.MetaPathFinder):
    """Hace fallar la importación de los paquetes bloqueados y de sus submódulos."""

    def __init__(self, blocked: Sequence[str]) -> None:
        self.blocked = tuple(blocked)
        self.attempts: list[str] = []

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        head = fullname.split(".")[0]
        if head in self.blocked:
            self.attempts.append(fullname)
            raise BlockedImportError(
                f"bloqueado: {fullname!r} no puede importarse desde un módulo crítico (NFR-NUC-25)"
            )
        return None


def run(modules: Sequence[str], blocked: Sequence[str]) -> dict[str, object]:
    """Ejecuta la prueba en este intérprete y devuelve el resultado como diccionario."""
    for name in list(sys.modules):
        if name.split(".")[0] in blocked:
            del sys.modules[name]
    finder = BlockingFinder(blocked)
    sys.meta_path.insert(0, finder)

    block_works = False
    try:
        importlib.import_module(blocked[0])
    except ImportError:
        block_works = True

    failures: dict[str, str] = {}
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as error:  # El motivo, sea cual sea, es el resultado.
            failures[module] = f"{type(error).__name__}: {error}"

    leaked = sorted(name for name in sys.modules if name.split(".")[0] in blocked)
    return {
        "modules": list(modules),
        "blocked": list(blocked),
        "block_works": block_works,
        "failures": failures,
        "leaked": leaked,
        "ok": block_works and not failures and not leaked,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Importa los módulos críticos con FastAPI y SQLAlchemy bloqueados."
    )
    parser.add_argument("--modules", nargs="+", default=list(CRITICAL_MODULES))
    parser.add_argument("--blocked", nargs="+", default=list(BLOCKED_PACKAGES))
    arguments = parser.parse_args(argv)
    result = run(arguments.modules, arguments.blocked)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
