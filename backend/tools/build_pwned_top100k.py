"""Genera ``resources/pwned-top100k.txt``, el respaldo local de filtradas (NFR-NUC-26, R8).

El archivo lleva el SHA-1 (hexadecimal en mayúsculas, uno por línea, ordenados, LF) de las
``--count`` contraseñas más filtradas. Se genera en la construcción de la imagen, no se guarda
en el repositorio (``.gitignore``): así se actualiza en cada construcción (tech-stack §3).
``identity.adapters.hibp.LocalBreachList.from_file`` lo carga al arrancar.

Fuentes admitidas (``--format``):

- ``hibp``: volcado del descargador oficial de Pwned Passwords (``PwnedPasswordsDownloader``),
  una línea ``<SHA-1>:<cuenta>`` con el hash completo de 40 caracteres, ordenadas por hash
  (como lo entrega el descargador; se lee en flujo, con memoria constante). Se eligen las
  ``--count`` de mayor cuenta; a igual cuenta, el hash menor. Es la fuente preferida: "más
  filtradas" según el mismo servicio.
- ``plaintext``: lista de contraseñas ordenada de más a menos frecuente (una por línea, UTF-8),
  p. ej. las listas de 100 000 de SecLists (MIT). Se toman las ``--count`` primeras distintas y
  se guardan solo sus SHA-1: el archivo no contiene ninguna contraseña en claro.

Si la fuente no alcanza ``--count`` entradas distintas, termina en 1 sin escribir nada.
``--check`` compara con el archivo existente en lugar de escribirlo.

Uso: ``uv run python tools/build_pwned_top100k.py --source <ruta> --format hibp|plaintext
[--count 100000] [--output resources/pwned-top100k.txt] [--check]``.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Final

BACKEND: Final = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT: Final = BACKEND / "resources" / "pwned-top100k.txt"
DEFAULT_COUNT: Final = 100_000

_HIBP_LINE: Final = re.compile(r"([0-9A-Fa-f]{40}):([0-9]{1,20})")


class SourceError(ValueError):
    """La fuente no tiene el formato pedido o no alcanza el número de entradas."""


def top_from_hibp(lines: Iterable[str], count: int) -> list[str]:
    """Los ``count`` SHA-1 de mayor cuenta de un volcado ``<SHA-1>:<cuenta>``.

    El volcado oficial viene ordenado por hash y tiene del orden de 10⁹ líneas: la memoria es
    constante salvo el montículo de ``count`` entradas. Los repetidos se detectan por
    adyacencia, y una línea fuera de orden se rechaza (sin orden no se podrían detectar).
    """
    best: list[tuple[int, str]] = []
    previous = ""
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        match = _HIBP_LINE.fullmatch(line)
        if match is None:
            raise SourceError(f"línea {number}: no es <SHA-1>:<cuenta>")
        digest, occurrences = match[1].upper(), int(match[2])
        if digest == previous:
            raise SourceError(f"línea {number}: hash repetido")
        if digest < previous:
            raise SourceError(f"línea {number}: el volcado no está ordenado por hash")
        previous = digest
        # Montículo de mínimos por (cuenta, hash invertido): la raíz es la peor entrada (menor
        # cuenta y, a igual cuenta, hash mayor); se queda con las ``count`` mejores.
        entry = (occurrences, _invert(digest))
        if len(best) < count:
            heapq.heappush(best, entry)
        elif entry > best[0]:
            heapq.heapreplace(best, entry)
    if len(best) < count:
        raise SourceError(f"la fuente tiene {len(best)} hashes; se piden {count}")
    return sorted(_invert(inverted) for _, inverted in best)


_HEX: Final = "0123456789ABCDEF"
_INVERTED: Final = str.maketrans(_HEX, _HEX[::-1])


def _invert(digest: str) -> str:
    """Hash con cada dígito complementado: invierte el orden lexicográfico (involución)."""
    return digest.translate(_INVERTED)


def top_from_plaintext(lines: Iterable[str], count: int) -> list[str]:
    """SHA-1 de las ``count`` primeras contraseñas distintas de una lista ordenada."""
    digests: set[str] = set()
    for raw in lines:
        password = raw.rstrip("\r\n")
        if not password:
            continue
        digests.add(
            hashlib.sha1(password.encode("utf-8", "surrogatepass"), usedforsecurity=False)
            .hexdigest()
            .upper()
        )
        if len(digests) == count:
            return sorted(digests)
    raise SourceError(f"la fuente tiene {len(digests)} contraseñas distintas; se piden {count}")


def render(digests: Sequence[str]) -> str:
    return "".join(f"{digest}\n" for digest in digests)


def _read_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="surrogateescape", newline="") as source:
        yield from source


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el respaldo local de filtradas.")
    parser.add_argument("--source", type=Path, required=True, help="Archivo de la fuente.")
    parser.add_argument("--format", choices=("hibp", "plaintext"), required=True)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check", action="store_true", help="Compara con el archivo en lugar de escribirlo."
    )
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count debe ser positivo")
    try:
        lines = _read_lines(args.source)
        if args.format == "hibp":
            digests = top_from_hibp(lines, args.count)
        else:
            digests = top_from_plaintext(lines, args.count)
    except (OSError, SourceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    content = render(digests)
    if args.check:
        current = args.output.read_text(encoding="ascii") if args.output.exists() else None
        if current != content:
            print(f"{args.output}: difiere de la fuente", file=sys.stderr)
            return 1
        print(f"{args.output}: al día ({len(digests)} hashes)")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="ascii", newline="\n")
    print(f"{args.output}: {len(digests)} hashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
