"""Verificación de firmas Ed25519 (RFC 8032) en Python puro (LC-NUC-18, PAT-NUC-MAN-08).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10, porque
``tools/build_verifier.py`` lo copia en el verificador de un archivo ``tools/vigia_verify.py``.
Solo verifica: el verificador no firma ni toca claves privadas.

Sigue la verificación sin cofactor de OpenSSL, que es la que usa ``cryptography`` en la
plataforma y contra la que se prueba por oráculo (PR-NUC-57):

1. La clave pública y la firma tienen 32 y 64 bytes.
2. ``S`` (los 32 últimos bytes de la firma, little-endian) es menor que el orden ``L``.
3. La clave pública se decodifica como punto de la curva (§5.1.3); una ``y`` que no está
   reducida se lee módulo ``p`` y el signo de una ``x`` nula se ignora, como en OpenSSL.
4. ``k = SHA-512(R ‖ A ‖ M)`` módulo ``L``; la firma vale si la codificación de
   ``[S]B - [k]A`` es exactamente ``R`` (los 32 primeros bytes de la firma).

Coordenadas extendidas de Edwards (§5.1.4). Tiempo variable: aquí todo es público.
"""

from __future__ import annotations

import hashlib
from typing import Final

__all__ = ["ed25519_verify"]

_P: Final = 2**255 - 19
_L: Final = 2**252 + 27742317777372353535851937790883648493
_D: Final = -121665 * pow(121666, _P - 2, _P) % _P
_D2: Final = 2 * _D % _P
_SQRT_M1: Final = pow(2, (_P - 1) // 4, _P)

_Point = tuple[int, int, int, int]
"""Punto en coordenadas extendidas ``(X, Y, Z, T)`` con ``x = X/Z``, ``y = Y/Z``, ``XY = ZT``."""

_IDENTITY: Final[_Point] = (0, 1, 1, 0)


def _add(left: _Point, right: _Point) -> _Point:
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * _D2 * t2 % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _double(point: _Point) -> _Point:
    x1, y1, z1, _ = point
    a = x1 * x1 % _P
    b = y1 * y1 % _P
    c = 2 * z1 * z1 % _P
    h = a + b
    e = h - (x1 + y1) * (x1 + y1) % _P
    g = a - b
    f = c + g
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _decode(data: bytes) -> _Point | None:
    """Punto de una codificación de 32 bytes, o ``None`` si ``y`` no está en la curva."""
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = (value & ((1 << 255) - 1)) % _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P) % _P
    check = v * x * x % _P
    if check == (-u) % _P and check != u:
        x = x * _SQRT_M1 % _P
    elif check != u:
        return None
    if x & 1 != sign:
        x = (_P - x) % _P
    return (x, y, 1, x * y % _P)


def _encode(point: _Point) -> bytes:
    x, y, z, _ = point
    inverse = pow(z, _P - 2, _P)
    x, y = x * inverse % _P, y * inverse % _P
    return (y | (x & 1) << 255).to_bytes(32, "little")


_BASE: Final = _decode(
    (4 * pow(5, _P - 2, _P) % _P).to_bytes(32, "little")  # y = 4/5, x positiva
)


def _double_scalar_mult(
    first: int, first_point: _Point, second: int, second_point: _Point
) -> _Point:
    """``[first]first_point + [second]second_point`` con un solo recorrido de bits (Straus)."""
    both = _add(first_point, second_point)
    result = _IDENTITY
    for bit in range(max(first.bit_length(), second.bit_length()) - 1, -1, -1):
        result = _double(result)
        pick = (first >> bit & 1, second >> bit & 1)
        if pick == (1, 1):
            result = _add(result, both)
        elif pick == (1, 0):
            result = _add(result, first_point)
        elif pick == (0, 1):
            result = _add(result, second_point)
    return result


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """``True`` si ``signature`` es una firma Ed25519 válida de ``message`` con ``public_key``."""
    if len(public_key) != 32 or len(signature) != 64 or _BASE is None:
        return False
    r_bytes = bytes(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    a = _decode(bytes(public_key))
    if a is None:
        return False
    digest = hashlib.sha512(r_bytes + bytes(public_key) + bytes(message)).digest()
    k = int.from_bytes(digest, "little") % _L
    x, y, z, t = a
    minus_a = ((_P - x) % _P, y, z, (_P - t) % _P)
    return _encode(_double_scalar_mult(s, _BASE, k, minus_a)) == r_bytes
