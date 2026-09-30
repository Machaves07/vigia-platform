"""Genera ``backend/openapi/app.yaml`` desde las rutas de FastAPI (NFR-NUC-52; LC-NUC-19).

Uso (desde ``backend/``)::

    uv run python -m vigia_platform.shared.api.export_openapi          # escribe el archivo
    uv run python -m vigia_platform.shared.api.export_openapi --check  # 0 si no hay diferencia

La especificación sale de ``build_openapi_app()``: la misma aplicación que ``create_app`` (mismas
unidades, rutas y matriz de permisos), sin abrir conexiones. ``--check`` compara byte a byte con
el archivo versionado y termina en 1 si difiere o falta: todo cambio de ruta es un cambio de
contrato interno que se regenera y se revisa (U-05 genera su cliente desde este archivo).

El YAML se escribe con un emisor propio, determinista y sin dependencias: bloques con dos
espacios, claves en el orden de FastAPI y todo escalar de texto entre comillas dobles con los
escapes de JSON (que YAML 1.2 lee igual), así que ningún valor se reinterpreta.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from vigia_platform.shared.api.app import build_openapi_app

__all__ = ["DEFAULT_OUTPUT", "main", "render", "to_yaml"]

DEFAULT_OUTPUT: Final = Path(__file__).resolve().parents[4] / "openapi/app.yaml"
"""``backend/openapi/app.yaml``."""
_HEADER: Final = (
    "# Generado por `uv run python -m vigia_platform.shared.api.export_openapi` (NFR-NUC-52).\n"
    "# No se edita a mano: la canalización exige `--check` sin diferencia.\n"
)


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("un número no finito no es JSON")
        # Con punto decimal siempre: sin él, un lector de YAML 1.1 lee ``5e-324`` como texto.
        text = repr(value)
        mantissa, _, exponent = text.partition("e")
        if "." not in mantissa:
            mantissa += ".0"
        return mantissa + ("e" + exponent if exponent else "")
    if isinstance(value, str):
        return _NON_VERBATIM.sub(_escape, json.dumps(value, ensure_ascii=False))
    raise TypeError(f"valor no representable en la especificación: {type(value).__name__}")


_VERBATIM: Final = (
    (0x20, 0x7E),
    (0xA0, 0x2027),
    (0x202A, 0xD7FF),
    (0xE000, 0xFEFE),
    (0xFF00, 0xFFFD),
    (0x10000, 0x10FFFF),
)
"""Lo que va sin escapar dentro de las comillas: el conjunto imprimible de YAML menos sus saltos
de línea (``U+0085``, ``U+2028``, ``U+2029``) y la marca de orden de bytes."""
_NON_VERBATIM: Final = re.compile(
    "[^" + "".join(f"\\U{low:08x}-\\U{high:08x}" for low, high in _VERBATIM) + "]"
)


def _escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group()):04x}"


def _emit(value: object, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            name = _scalar(str(key))
            if isinstance(item, dict | list) and item:
                lines.append(f"{pad}{name}:")
                _emit(item, indent + 2, lines)
            else:
                lines.append(f"{pad}{name}: {_inline(item)}")
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict | list) and item:
                nested: list[str] = []
                _emit(item, indent + 2, nested)
                first = nested[0][indent + 2 :]
                lines.append(f"{pad}- {first}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_inline(item)}")
        return
    lines.append(f"{pad}{_scalar(value)}")


def _inline(value: object) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _scalar(value)


def to_yaml(document: dict[str, object]) -> str:
    """YAML en bloque, determinista, de un documento JSON."""
    lines: list[str] = []
    _emit(document, 0, lines)
    return _HEADER + "\n".join(lines) + "\n"


def render() -> str:
    """El contenido de ``app.yaml`` para las rutas actuales."""
    return to_yaml(build_openapi_app().openapi())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export_openapi",
        description="Genera o comprueba backend/openapi/app.yaml desde las rutas de la aplicación.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="no escribe: termina en 1 si el archivo versionado difiere de lo generado",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="ruta del archivo (por defecto, backend/openapi/app.yaml)",
    )
    arguments = parser.parse_args(argv)
    content = render().encode("utf-8")
    output: Path = arguments.output
    if arguments.check:
        try:
            current = output.read_bytes()
        except OSError:
            print(f"falta {output}: genéralo sin --check", file=sys.stderr)
            return 1
        if current != content:
            print(
                f"{output} no coincide con las rutas actuales: regenéralo con "
                "`uv run python -m vigia_platform.shared.api.export_openapi` y revisa el cambio",
                file=sys.stderr,
            )
            return 1
        print(f"{output} coincide con las rutas actuales")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    print(f"escrito {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
