"""Canonicalización JSON RFC 8785 (JCS) en Python puro (LC-NUC-18, PAT-NUC-MAN-08).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10, porque
``tools/build_verifier.py`` lo copia en el verificador de un archivo ``tools/vigia_verify.py``.
Es byte a byte la canonicalización de la biblioteca ``rfc8785`` que usa la plataforma
(PR-NUC-57):

- ``null``, ``true`` y ``false``; enteros en ``±(2**53 - 1)`` sin exponente; dobles con la
  notación de ECMAScript (``Number.prototype.toString``, RFC 8785 §3.2.2.3), ``-0`` como ``0``.
- Cadenas con los escapes de ``JSON.stringify``: comilla, barra inversa, ``\\b \\f \\n \\r \\t``
  y el resto de controles como ``\\u00xx`` en minúsculas; todo lo demás, literal en UTF-8.
- Objetos con las claves ordenadas por sus unidades de código UTF-16.

Todo lo que RFC 8785 no representa termina en ``CanonicalizationError``: ``NaN``, infinitos,
enteros fuera del rango seguro, claves que no son texto, sustitutos sueltos, tipos que no son
JSON y anidamientos que agotan la pila. Sin E/S ni hora del sistema.
"""

from __future__ import annotations

import math
from json.encoder import encode_basestring
from typing import Final

__all__ = ["MAX_SAFE_INTEGER", "CanonicalizationError", "canonicalize", "format_number"]

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""Mayor entero que RFC 8785 escribe sin pérdida (I-JSON, RFC 7493)."""


class CanonicalizationError(ValueError):
    """El valor no tiene forma canónica RFC 8785."""


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def format_number(value: float) -> str:
    """Un doble finito en la notación de ECMAScript (RFC 8785 §3.2.2.3)."""
    if not math.isfinite(value):
        raise CanonicalizationError("NaN e infinito no son números JSON")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + format_number(-value)
    # repr da los dígitos más cortos que vuelven al mismo doble, como exige ECMAScript.
    mantissa, _, exponent_text = repr(value).partition("e")
    integer, _, fraction = mantissa.partition(".")
    if fraction == "0":
        fraction = ""
    if integer != "0":
        point = len(integer) + (int(exponent_text) if exponent_text else 0)
        digits = (integer + fraction).rstrip("0")
    else:
        significant = fraction.lstrip("0")
        point = len(significant) - len(fraction)
        digits = significant.rstrip("0")
    # El valor es 0.<digits> * 10**point.
    size = len(digits)
    if size <= point <= 21:
        return digits + "0" * (point - size)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    exponent = point - 1
    head = digits[0] if size == 1 else digits[0] + "." + digits[1:]
    return f"{head}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"


def _write(value: object, parts: list[str]) -> None:
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, str):
        parts.append(encode_basestring(value))
    elif isinstance(value, int):
        number = int(value)
        if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
            raise CanonicalizationError("entero fuera del rango seguro de un doble")
        parts.append(str(number))
    elif isinstance(value, float):
        parts.append(format_number(float(value)))
    elif isinstance(value, list | tuple):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _write(item, parts)
        parts.append("]")
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("las claves de un objeto deben ser texto")
        parts.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_key)):
            if index:
                parts.append(",")
            parts.append(encode_basestring(key))
            parts.append(":")
            _write(value[key], parts)
        parts.append("}")
    else:
        raise CanonicalizationError(f"tipo sin forma JSON: {type(value).__name__}")


def canonicalize(value: object) -> bytes:
    """Bytes UTF-8 de la forma canónica RFC 8785 de ``value``."""
    parts: list[str] = []
    try:
        _write(value, parts)
        return "".join(parts).encode("utf-8")
    except RecursionError:
        raise CanonicalizationError("anidamiento demasiado profundo") from None
    except UnicodeEncodeError:
        # Un sustituto UTF-16 suelto, en un valor o en una clave (también al ordenar).
        raise CanonicalizationError("el texto contiene un sustituto UTF-16 suelto") from None
