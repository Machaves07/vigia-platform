"""Plegado de validación de U-04 (A-45 y su nota del 2026-09-30; LC-LAZ-16; PR-LAZ-23).

``validation_form(candidate)`` es la forma sobre la que buscan **los dos** validadores de U-04
(``blocked_vocabulary`` y ``person_attribution``). Parte de ``FreeTextCandidate.canonical`` de
U-02 (NFKC, todo espacio Unicode como un espacio ASCII, sin diacríticos y en minúsculas) y añade
lo que la nota de A-45 pide:

- ``casefold()`` en lugar de ``lower()``: «Straße» y «STRASSE» pliegan igual (``strasse``);
- el **plegado de confusables** de UTS #39 (``backend/resources/unicode/confusables.txt``, versión
  ``UNICODE_SECURITY_VERSION``, licencia Unicode v3 en ``LICENSE-Unicode.txt``): cada carácter no
  ASCII cuyo prototipo, ya en minúsculas, son letras o dígitos ASCII se sustituye por ese
  prototipo. Así la a, la e o la o cirílicas (U+0430, U+0435, U+043E) y la ómicron griega
  (U+03BF) se leen como las latinas. Los
  caracteres ASCII **no** se pliegan (la tabla lleva «m» → «rn» o «0» → «O», que romperían las
  palabras y las cifras de los patrones).

Es **idempotente** (PR-LAZ-23): se repite la forma canónica, ``casefold`` y los confusables hasta
que el resultado no cambia, de modo que plegar un texto ya plegado lo deja igual. Es una función
pura: sin red, sin base y sin reloj; la tabla se lee una vez por proceso.

**Módulo crítico** (LC-LAZ-16): se importa sin FastAPI ni SQLAlchemy.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from vigia_platform.ledger.free_text import FreeTextCandidate, canonical_form

__all__ = [
    "CONFUSABLES_PATH",
    "UNICODE_SECURITY_VERSION",
    "confusable_map",
    "fold_text",
    "validation_form",
]

CONFUSABLES_PATH: Final = (
    Path(__file__).resolve().parents[4] / "resources" / "unicode" / "confusables.txt"
)
"""``confusables.txt`` de UTS #39 empaquetado en la imagen con ``backend/resources``."""

_VERSION_LINE: Final = re.compile(r"^# Version: (\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_PROTOTYPE: Final = re.compile(r"^[a-z0-9]+$")
_MAX_PASSES: Final = 8
"""Pasadas como máximo hasta el punto fijo; en la práctica basta con una o dos."""


def _security_version(header: str) -> str:
    found = _VERSION_LINE.search(header)
    if found is None:
        raise ValueError("confusables.txt no declara su versión")
    return found.group(1)


@functools.cache
def _table() -> tuple[str, Mapping[int, str]]:
    content = CONFUSABLES_PATH.read_text(encoding="utf-8")
    mapping: dict[int, str] = {}
    for line in content.splitlines():
        data = line.split("#", 1)[0].strip()
        if not data:
            continue
        source, target, *_ = (field.strip() for field in data.split(";"))
        codes = source.split()
        if len(codes) != 1:
            continue
        code = int(codes[0], 16)
        if code < 0x80:
            continue  # lo ASCII no se pliega: «m» → «rn», «0» → «O»
        prototype = canonical_form("".join(chr(int(part, 16)) for part in target.split()))
        prototype = prototype.casefold()
        if _PROTOTYPE.fullmatch(prototype):
            mapping[code] = prototype
    return _security_version(content[:2048]), mapping


def confusable_map() -> Mapping[int, str]:
    """Punto de código no ASCII → su prototipo ASCII en minúsculas (tabla de ``str.translate``)."""
    return _table()[1]


UNICODE_SECURITY_VERSION: Final = _table()[0]
"""Versión de los datos de UTS #39 empaquetados (cabecera ``# Version:`` de la tabla)."""


def _pass(text: str) -> str:
    return text.casefold().translate(confusable_map())


def fold_text(canonical: str) -> str:
    """El plegado de validación de una forma canónica de U-02, hasta el punto fijo."""
    folded = _pass(canonical)
    for _ in range(_MAX_PASSES):
        again = _pass(canonical_form(folded))
        if again == folded:
            return folded
        folded = again
    return folded  # pragma: no cover - ningún texto conocido necesita más de dos pasadas


def validation_form(candidate: FreeTextCandidate) -> str:
    """La forma de validación de ``candidate``: su forma canónica plegada (A-45)."""
    return fold_text(candidate.canonical)
