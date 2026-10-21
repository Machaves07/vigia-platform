#!/usr/bin/env python3
# ARCHIVO GENERADO por tools/build_verifier.py: no se edita a mano.
# Fuente: vigia_platform.ledger.chain (pure_rfc8785, pure_ed25519, chain_walk, package_verifier); vigia_platform.accreditation (export_verifier).
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

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import hashlib
import itertools
import json
import math
import re
import shutil
import stat
import struct
import sys
import zipfile
import zlib
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from json.encoder import encode_basestring
from pathlib import Path
from typing import Any, Final, IO, TypeGuard


# -------------------- vigia_platform.ledger.chain.pure_rfc8785 --------------------

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


# -------------------- vigia_platform.ledger.chain.pure_ed25519 --------------------

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


# -------------------- vigia_platform.ledger.chain.chain_walk --------------------

"""Recorrido de una cadena: enlaces, hashes y puntos de control (LC-NUC-18, BR-NUC-46, 56, 57).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10. Lo comparten el
verificador de paquetes (``tools/vigia_verify.py``, generado por ``tools/build_verifier.py``) y el
paso 2 del motor de verificación de la plataforma (``ledger.chain.verify``, TASK-118), de modo que
plataforma y verificador aplican el mismo algoritmo (PAT-NUC-MAN-08).

Las entradas son registros del expediente o entradas de auditoría en la **forma del paquete**
(``docs/package-format.md``): diccionarios JSON con los valores textuales exactos de la base
(UUID en minúsculas, marcas ``AAAA-MM-DDTHH:MM:SS.mmmZ``) y el contenido como documento JSON.

Por cada entrada, en este orden (business-logic-model §5; el primer fallo fija el motivo):

1. forma: claves exactas y tipos (``malformed``);
2. secuencia: la anterior más uno (``sequence_gap``);
3. cadena: organización y planta de la cadena (``wrong_chain``);
4. enlace: ``previous_hash`` igual al hash del registro anterior (``previous_hash_mismatch``);
5. contenido: ``content_hash = SHA-256(RFC 8785(content))`` (``content_not_canonical``,
   ``content_hash_mismatch``); en auditoría, ``filters`` y ``filters_hash`` (nulos a la vez);
6. registro: ``record_hash = SHA-256(RFC 8785(sobre) ‖ previous_hash)`` (``record_hash_mismatch``);
7. ancla: si un punto de control de un paquete anterior cubre esta secuencia, el hash coincide
   (``previous_checkpoint_mismatch``): si difiere, alguien reescribió el prefijo;
8. punto de control (``record_type = checkpoint`` o ``operation = checkpoint``): forma
   (``checkpoint_malformed``), cobertura exacta del registro anterior (``checkpoint_coverage``),
   clave conocida (``unknown_key``) y firma Ed25519 sobre el canónico de
   ``{covered_hash, covered_sequence, kind, organization_id, plant_id, taken_at}``
   (``bad_signature``).

La secuencia rota es siempre la primera esperada que falla (la anterior más uno) y se nombra
además el identificador de la entrada encontrada. Al terminar, la cabeza declarada (última
secuencia y último hash) debe coincidir con la recalculada (``head_mismatch``) y toda ancla debe
haberse comprobado (``previous_checkpoint_missing``: el paquete no llega a esa secuencia).

Sin E/S ni hora del sistema.
"""


CHAIN_KINDS: Final = ("ledger", "audit")
"""``ledger``: expediente (por organización y planta, o de organización); ``audit``: auditoría."""

CHECKPOINT: Final = "checkpoint"

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNATURE: Final = re.compile(r"[A-Za-z0-9+/]{86}==")

_ACTOR_KEYS: Final = frozenset(
    {"concession_id", "display_name_snapshot", "id", "kind", "role_in_use", "unit"}
)
_RECORD_SCOPE_KEYS: Final = frozenset({"node_id", "plant_id", "zone_id"})
_AUDIT_SCOPE_KEYS: Final = frozenset({"plant_id", "zone_id"})
_RESOURCE_KEYS: Final = frozenset({"id", "kind"})
_CHECKPOINT_KEYS: Final = frozenset(
    {"covered_hash", "covered_sequence", "key_id", "signature", "taken_at"}
)
_RECORD_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "content",
        "content_hash",
        "correlation_id",
        "organization_id",
        "plant_id",
        "previous_hash",
        "received_at",
        "record_hash",
        "record_id",
        "record_type",
        "schema_version",
        "scope",
    }
)
_AUDIT_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "correlation_id",
        "entry_hash",
        "entry_id",
        "filters",
        "filters_hash",
        "occurred_at",
        "operation",
        "organization_id",
        "outcome",
        "previous_hash",
        "resource_ref",
        "result_count",
        "scope",
    }
)

MESSAGES: Final = {
    "malformed": "la entrada no tiene la forma de un registro de la cadena",
    "sequence_gap": "la secuencia no sigue a la anterior (falta, sobra o se repite un registro)",
    "wrong_chain": "la entrada es de otra organización o planta",
    "previous_hash_mismatch": "el enlace con el registro anterior no coincide",
    "content_not_canonical": "el contenido no tiene forma canónica RFC 8785",
    "content_hash_mismatch": "el hash del contenido no coincide",
    "record_hash_mismatch": "el hash del registro no coincide",
    "previous_checkpoint_mismatch": (
        "el registro no coincide con el punto de control del paquete anterior: "
        "el prefijo de la cadena cambió"
    ),
    "checkpoint_malformed": "el punto de control no tiene la forma esperada",
    "checkpoint_coverage": "el punto de control no cubre exactamente el registro anterior",
    "unknown_key": "la clave del punto de control no está entre las claves públicas del paquete",
    "bad_signature": "la firma del punto de control no verifica",
    "head_mismatch": "la cabeza declarada de la cadena no coincide con la recalculada",
    "previous_checkpoint_missing": (
        "el paquete no contiene la secuencia del punto de control anterior: "
        "no se puede comprobar el prefijo"
    ),
}
"""Motivo de rotura → explicación en español."""


def _fullmatch(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional(value: object, check: re.Pattern[str]) -> bool:
    return value is None or _fullmatch(check, value)


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def genesis_hash(organization_id: str, plant_id: str | None) -> str:
    """``SHA-256("vigia:genesis:" + organization_id + ":" + (plant_id | "organization"))``."""
    tail = "organization" if plant_id is None else plant_id
    return hashlib.sha256(f"vigia:genesis:{organization_id}:{tail}".encode()).hexdigest()


def checkpoint_message(
    kind: str,
    organization_id: str,
    plant_id: str | None,
    covered_sequence: int,
    covered_hash: str,
    taken_at: str,
) -> bytes:
    """Bytes que firma la clave ``checkpoint`` (domain-entities §3.4, BR-NUC-53)."""
    return canonicalize(
        {
            "covered_hash": covered_hash,
            "covered_sequence": covered_sequence,
            "kind": kind,
            "organization_id": organization_id,
            "plant_id": plant_id,
            "taken_at": taken_at,
        }
    )


@dataclass(frozen=True)
class ChainRef:
    """Una cadena: expediente de planta, expediente de organización o auditoría."""

    kind: str
    organization_id: str
    plant_id: str | None

    def describe(self) -> str:
        """Nombre de la cadena en español."""
        if self.kind == "audit":
            return f"auditoría de la organización {self.organization_id}"
        if self.plant_id is None:
            return f"expediente de la organización {self.organization_id}"
        return f"expediente de la planta {self.plant_id}"


@dataclass(frozen=True)
class Break:
    """Primer fallo de una cadena: secuencia esperada, entrada encontrada y motivo."""

    sequence: int | None
    entry_id: str | None
    reason: str
    detail: str = ""

    def message(self) -> str:
        """Explicación en español."""
        text = MESSAGES.get(self.reason, self.reason)
        return f"{text} ({self.detail})" if self.detail else text


@dataclass(frozen=True)
class CheckpointSeen:
    """Un punto de control verificado dentro de la cadena."""

    sequence: int
    entry_id: str
    entry_hash: str
    covered_sequence: int
    covered_hash: str
    key_id: str
    taken_at: str


@dataclass(frozen=True)
class ChainResult:
    """Resultado del recorrido de una cadena."""

    chain: ChainRef
    status: str
    first_sequence: int | None
    last_sequence: int
    last_hash: str
    entries: int
    checkpoints: tuple[CheckpointSeen, ...]
    anchors_matched: tuple[int, ...]
    broken: Break | None

    @property
    def intact(self) -> bool:
        return self.status == "intact"


def _entry_id(chain: ChainRef, entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get("entry_id" if chain.kind == "audit" else "record_id")
    return value if _fullmatch(_UUID, value) else None


def _actor_ok(actor: object) -> bool:
    return (
        isinstance(actor, dict)
        and actor.keys() == _ACTOR_KEYS
        and _optional(actor["concession_id"], _UUID)
        and isinstance(actor["display_name_snapshot"], str)
        and _fullmatch(_UUID, actor["id"])
        and isinstance(actor["kind"], str)
        and _optional_text(actor["role_in_use"])
        and isinstance(actor["unit"], str)
    )


def _record_ok(entry: dict[str, object]) -> bool:
    if entry.keys() != _RECORD_KEYS:
        return False
    scope = entry["scope"]
    return (
        _fullmatch(_UUID, entry["record_id"])
        and _fullmatch(_UUID, entry["organization_id"])
        and _optional(entry["plant_id"], _UUID)
        and _is_integer(entry["chain_sequence"])
        and isinstance(entry["record_type"], str)
        and _is_integer(entry["schema_version"])
        and _actor_ok(entry["actor"])
        and isinstance(scope, dict)
        and scope.keys() == _RECORD_SCOPE_KEYS
        and all(_optional(value, _UUID) for value in scope.values())
        and _fullmatch(_UUID, entry["correlation_id"])
        and _fullmatch(_TIMESTAMP, entry["received_at"])
        and _fullmatch(_HEX64, entry["content_hash"])
        and _fullmatch(_HEX64, entry["previous_hash"])
        and _fullmatch(_HEX64, entry["record_hash"])
    )


def _audit_ok(entry: dict[str, object]) -> bool:
    if entry.keys() != _AUDIT_KEYS:
        return False
    scope = entry["scope"]
    resource = entry["resource_ref"]
    return (
        _fullmatch(_UUID, entry["entry_id"])
        and _fullmatch(_UUID, entry["organization_id"])
        and _is_integer(entry["chain_sequence"])
        and _actor_ok(entry["actor"])
        and isinstance(entry["operation"], str)
        and isinstance(scope, dict)
        and scope.keys() == _AUDIT_SCOPE_KEYS
        and all(_optional(value, _UUID) for value in scope.values())
        and (
            resource is None
            or (
                isinstance(resource, dict)
                and resource.keys() == _RESOURCE_KEYS
                and _optional(resource["id"], _UUID)
                and _optional_text(resource["kind"])
            )
        )
        and _optional(entry["filters_hash"], _HEX64)
        and (entry["result_count"] is None or _is_integer(entry["result_count"]))
        and isinstance(entry["outcome"], str)
        and _fullmatch(_UUID, entry["correlation_id"])
        and _fullmatch(_TIMESTAMP, entry["occurred_at"])
        and _fullmatch(_HEX64, entry["previous_hash"])
        and _fullmatch(_HEX64, entry["entry_hash"])
    )


def _record_envelope(entry: dict[str, object]) -> dict[str, object]:
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "content_hash": entry["content_hash"],
        "correlation_id": entry["correlation_id"],
        "organization_id": entry["organization_id"],
        "plant_id": entry["plant_id"],
        "received_at": entry["received_at"],
        "record_id": entry["record_id"],
        "record_type": entry["record_type"],
        "schema_version": entry["schema_version"],
        "scope": entry["scope"],
    }


def _audit_envelope(entry: dict[str, object]) -> dict[str, object]:
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "correlation_id": entry["correlation_id"],
        "entry_id": entry["entry_id"],
        "filters_hash": entry["filters_hash"],
        "occurred_at": entry["occurred_at"],
        "operation": entry["operation"],
        "organization_id": entry["organization_id"],
        "outcome": entry["outcome"],
        "resource_ref": entry["resource_ref"],
        "result_count": entry["result_count"],
        "scope": entry["scope"],
    }


def _sha256_canonical(value: object) -> str | None:
    """SHA-256 hexadecimal del canónico de ``value``, o ``None`` si no tiene forma canónica."""
    try:
        return hashlib.sha256(canonicalize(value)).hexdigest()
    except CanonicalizationError:
        return None


@dataclass
class ChainWalker:
    """Recorre una cadena entrada a entrada, en orden de secuencia.

    ``public_keys``: clave pública Ed25519 (32 bytes) por ``key_id`` de propósito ``checkpoint``.
    ``start_sequence`` y ``start_hash``: punto de partida; por omisión, la génesis (secuencia 0 y
    el hash de génesis de la cadena). ``anchors``: ``record_hash`` esperado por secuencia, tomado
    de puntos de control de paquetes anteriores (BR-NUC-57).
    """

    chain: ChainRef
    public_keys: Mapping[str, bytes]
    start_sequence: int = 0
    start_hash: str | None = None
    anchors: Mapping[int, str] = field(default_factory=dict)
    _last_sequence: int = field(init=False)
    _last_hash: str = field(init=False)
    _entries: int = field(init=False, default=0)
    _checkpoints: list[CheckpointSeen] = field(init=False, default_factory=list)
    _anchors_matched: list[int] = field(init=False, default_factory=list)
    _last_entry_id: str | None = field(init=False, default=None)
    _broken: Break | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.chain.kind not in CHAIN_KINDS:
            raise ValueError(f"tipo de cadena desconocido: {self.chain.kind!r}")
        self._last_sequence = self.start_sequence
        self._last_hash = (
            self.start_hash
            if self.start_hash is not None
            else genesis_hash(self.chain.organization_id, self.chain.plant_id)
        )
        expected = self.anchors.get(self.start_sequence)
        if expected is not None:
            if expected != self._last_hash:
                self._broken = Break(self.start_sequence + 1, None, "previous_checkpoint_mismatch")
            else:
                self._anchors_matched.append(self.start_sequence)

    @property
    def broken(self) -> Break | None:
        return self._broken

    def feed(self, entry: object) -> Break | None:
        """Comprueba la entrada siguiente; devuelve el fallo si la cadena se rompe aquí.

        Tras el primer fallo la cadena queda rota y las entradas siguientes se ignoran.
        """
        if self._broken is not None:
            return self._broken
        self._broken = self._check(entry)
        return self._broken

    def _fail(self, entry: object, reason: str, detail: str = "") -> Break:
        return Break(self._last_sequence + 1, _entry_id(self.chain, entry), reason, detail)

    def _check(self, entry: object) -> Break | None:
        audit = self.chain.kind == "audit"
        if not isinstance(entry, dict):
            return self._fail(entry, "malformed")
        hash_key = "entry_hash" if audit else "record_hash"
        sequence, entry_hash = entry.get("chain_sequence"), entry.get(hash_key)
        well_formed = _audit_ok(entry) if audit else _record_ok(entry)
        if not (well_formed and isinstance(sequence, int) and isinstance(entry_hash, str)):
            return self._fail(entry, "malformed")
        if sequence != self._last_sequence + 1:
            return self._fail(entry, "sequence_gap", f"se encontró la secuencia {sequence}")
        if entry["organization_id"] != self.chain.organization_id or (
            not audit and entry["plant_id"] != self.chain.plant_id
        ):
            return self._fail(entry, "wrong_chain")
        if entry["previous_hash"] != self._last_hash:
            return self._fail(entry, "previous_hash_mismatch")

        if audit:
            content, declared = entry["filters"], entry["filters_hash"]
        else:
            content, declared = entry["content"], entry["content_hash"]
        if audit and content is None:
            if declared is not None:
                return self._fail(entry, "content_hash_mismatch")
        else:
            computed = _sha256_canonical(content)
            if computed is None:
                return self._fail(entry, "content_not_canonical")
            if computed != declared:
                return self._fail(entry, "content_hash_mismatch")

        envelope = _audit_envelope(entry) if audit else _record_envelope(entry)
        try:
            envelope_bytes = canonicalize(envelope)
        except CanonicalizationError:
            return self._fail(entry, "malformed")
        recomputed = hashlib.sha256(envelope_bytes + self._last_hash.encode("ascii")).hexdigest()
        if recomputed != entry_hash:
            return self._fail(entry, "record_hash_mismatch")

        expected = self.anchors.get(sequence)
        if expected is not None:
            if expected != entry_hash:
                return self._fail(entry, "previous_checkpoint_mismatch")
            self._anchors_matched.append(sequence)

        kind_field = entry["operation"] if audit else entry["record_type"]
        if kind_field == CHECKPOINT:
            failure = self._check_checkpoint(entry, content, sequence, entry_hash)
            if failure is not None:
                return failure

        self._last_sequence = sequence
        self._last_hash = entry_hash
        self._last_entry_id = _entry_id(self.chain, entry)
        self._entries += 1
        return None

    def _check_checkpoint(
        self, entry: dict[str, object], content: object, sequence: int, entry_hash: str
    ) -> Break | None:
        if not isinstance(content, dict) or content.keys() != _CHECKPOINT_KEYS:
            return self._fail(entry, "checkpoint_malformed")
        covered_sequence = content["covered_sequence"]
        covered_hash = content["covered_hash"]
        taken_at = content["taken_at"]
        key_id = content["key_id"]
        signature_text = content["signature"]
        if not (
            _is_integer(covered_sequence)
            and isinstance(covered_sequence, int)
            and isinstance(covered_hash, str)
            and _fullmatch(_HEX64, covered_hash)
            and isinstance(taken_at, str)
            and _fullmatch(_TIMESTAMP, taken_at)
            and isinstance(key_id, str)
            and isinstance(signature_text, str)
            and _fullmatch(_SIGNATURE, signature_text)
        ):
            return self._fail(entry, "checkpoint_malformed")
        if covered_sequence != sequence - 1 or covered_hash != self._last_hash:
            return self._fail(entry, "checkpoint_coverage")
        public_key = self.public_keys.get(key_id)
        if public_key is None:
            return self._fail(entry, "unknown_key", f"key_id {key_id!r}")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except (binascii.Error, ValueError):
            return self._fail(entry, "checkpoint_malformed")
        if base64.b64encode(signature).decode("ascii") != signature_text:
            # Base64 no canónico: los bits de relleno del último carácter no son cero. Otro texto
            # que decodifica a la misma firma no es la firma escrita (PR-NUC-21).
            return self._fail(entry, "checkpoint_malformed", "firma en base64 no canónico")
        message = checkpoint_message(
            self.chain.kind,
            self.chain.organization_id,
            self.chain.plant_id,
            covered_sequence,
            covered_hash,
            taken_at,
        )
        if not ed25519_verify(public_key, message, signature):
            return self._fail(entry, "bad_signature", f"key_id {key_id!r}")
        self._checkpoints.append(
            CheckpointSeen(
                sequence=sequence,
                entry_id=_entry_id(self.chain, entry) or "",
                entry_hash=entry_hash,
                covered_sequence=covered_sequence,
                covered_hash=covered_hash,
                key_id=key_id,
                taken_at=taken_at,
            )
        )
        return None

    def finish(self, declared_head: tuple[int, str] | None = None) -> ChainResult:
        """Cierra el recorrido; con ``declared_head``, exige que la cabeza coincida."""
        if self._broken is None and declared_head is not None:
            declared_sequence, declared_hash = declared_head
            if declared_sequence > self._last_sequence:
                self._broken = Break(
                    self._last_sequence + 1,
                    None,
                    "head_mismatch",
                    f"la cabeza declarada llega a la secuencia {declared_sequence}",
                )
            elif declared_sequence < self._last_sequence:
                self._broken = Break(
                    declared_sequence + 1,
                    None,
                    "head_mismatch",
                    f"la cabeza declarada termina en la secuencia {declared_sequence}",
                )
            elif declared_hash != self._last_hash:
                self._broken = Break(self._last_sequence, self._last_entry_id, "head_mismatch")
        if self._broken is None:
            missing = sorted(set(self.anchors) - set(self._anchors_matched))
            if missing:
                self._broken = Break(missing[0], None, "previous_checkpoint_missing")
        return ChainResult(
            chain=self.chain,
            status="intact" if self._broken is None else "broken",
            first_sequence=self.start_sequence + 1 if self._entries else None,
            last_sequence=self._last_sequence,
            last_hash=self._last_hash,
            entries=self._entries,
            checkpoints=tuple(self._checkpoints),
            anchors_matched=tuple(self._anchors_matched),
            broken=self._broken,
        )


# -------------------- vigia_platform.ledger.chain.package_verifier --------------------

"""Verificador de paquetes exportados: lectura, resultado y línea de órdenes (LC-NUC-18, BR-NUC-57).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10. ``tools/build_verifier.py``
lo copia, detrás de ``pure_rfc8785``, ``pure_ed25519`` y ``chain_walk``, en el verificador de un
archivo ``tools/vigia_verify.py`` que U-04 incluye en cada exportación y paquete mensual
(NFR-NUC-53): funciona sin red, sin acceso al sistema y sin ningún secreto del proveedor.

Uso: ``python vigia_verify.py <paquete> [--previous-checkpoint <ruta> ...] [--out resultado.json]``.

- ``<paquete>``: un directorio o un ``.zip`` con ``manifest.json`` y un archivo JSON Lines por
  cadena (formato en ``docs/package-format.md``).
- ``--previous-checkpoint``: un punto de control de un paquete anterior (la línea del registro, en
  un archivo ``.json``) o el paquete anterior entero (se toma el último punto de control de cada
  cadena). Se comprueba su firma y que el paquete actual contiene en esa secuencia el mismo
  registro: si difiere, alguien reescribió el prefijo de la cadena.
- ``--out``: archivo de resultado JSON. El resumen legible, en español, va a la salida estándar.

Código de salida: 0 solo si el paquete está íntegro (``intact``); 1 si alguna cadena está rota o
el paquete no se puede leer (``broken``); 2 si la orden está mal escrita.
"""


FORMAT: Final = "vigia-package"
FORMAT_VERSION: Final = 1
"""Versión del formato del paquete que este verificador lee."""

MANIFEST: Final = "manifest.json"
MAX_LINE_BYTES: Final = 4 * 1024 * 1024
"""Tope de una línea del paquete: un registro lleva a lo sumo 256 KB de contenido canónico."""
MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
"""Tope del manifiesto."""

EXIT_INTACT: Final = 0
EXIT_BROKEN: Final = 1
EXIT_USAGE: Final = 2

_MAX_SAFE_INTEGER: Final = 2**53 - 1
_UUID_TEXT: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64_TEXT: Final = re.compile(r"[0-9a-f]{64}")
_PUBLIC_KEY: Final = re.compile(r"[A-Za-z0-9+/]{43}=")
_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_MEMBER: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}){0,3}"
)
"""Ruta relativa de un archivo del paquete: sin ``..``, sin raíz y sin barra inversa."""
_RAW_ID: Final = re.compile(
    rb'"(?:record_id|entry_id)"\s*:\s*"'
    rb'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
"""Identificador de una línea ilegible, para nombrar el registro aunque la línea no se lea."""
_CHAIN_KEYS: Final = frozenset(
    {"file", "first_sequence", "kind", "last_hash", "last_sequence", "plant_id"}
)
_KEY_KEYS: Final = frozenset({"key_id", "public_key"})

PACKAGE_MESSAGES: Final = {
    "package_unreadable": "el paquete no se puede leer",
    "manifest_invalid": "el manifiesto del paquete no es válido",
    "unsupported_format": "el formato del paquete no es el que lee este verificador",
    "previous_checkpoint_invalid": "el punto de control anterior no es válido",
    "previous_checkpoint_chain_missing": (
        "el paquete no contiene la cadena del punto de control anterior"
    ),
}


class PackageError(Exception):
    """El paquete o un punto de control anterior no se pueden leer."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail

    def message(self) -> str:
        text = PACKAGE_MESSAGES.get(self.reason, self.reason)
        return f"{text}: {self.detail}" if self.detail else text


# --- Lectura estricta de JSON ---------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} no es un número JSON")


def _read_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("número fuera del rango de un doble")
    return value


def _read_int(text: str) -> int | float:
    value = int(text)
    if -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
        return value
    return _read_float(text)  # RFC 8785 lee todo número como doble


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("el objeto repite una clave")
    return document


def parse_json(data: bytes) -> object:
    """Documento JSON de ``data`` (UTF-8) con la lectura de RFC 8785.

    Rechaza con ``ValueError`` lo que no se puede volver a canonicalizar: UTF-8 inválido,
    ``NaN`` e infinitos, números fuera del doble y claves repetidas. Un entero de magnitud mayor
    que ``2**53 - 1`` se lee como doble, igual que lo escribió RFC 8785.
    """
    try:
        text = data.decode("utf-8")
        document: object = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_read_float,
            parse_int=_read_int,
            object_pairs_hook=_unique_keys,
        )
    except RecursionError:
        raise ValueError("anidamiento demasiado profundo") from None
    except UnicodeDecodeError:
        raise ValueError("no es UTF-8 válido") from None
    return document


# --- Fuente del paquete: directorio o zip -------------------------------------------------------


class _Source:
    """Archivos de un paquete por nombre relativo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._zip: zipfile.ZipFile | None = None
        if path.is_dir():
            return
        try:
            self._zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as error:
            raise PackageError(
                "package_unreadable", f"no es un directorio ni un zip ({error})"
            ) from None

    def names(self) -> list[str]:
        if self._zip is not None:
            return sorted(name for name in self._zip.namelist() if not name.endswith("/"))
        return sorted(
            child.relative_to(self.path).as_posix()
            for child in self.path.rglob("*")
            if child.is_file()
        )

    def open(self, name: str) -> IO[bytes]:
        if _MEMBER.fullmatch(name) is None:
            raise PackageError("manifest_invalid", f"ruta de archivo no permitida {name!r}")
        try:
            if self._zip is not None:
                return self._zip.open(name)
            root = self.path.resolve()
            target = (root / name).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise PackageError("package_unreadable", f"falta el archivo {name!r}")
            return target.open("rb")
        except KeyError:
            raise PackageError("package_unreadable", f"falta el archivo {name!r}") from None
        except OSError as error:
            raise PackageError("package_unreadable", f"{name!r}: {error}") from None

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _read_all(source: _Source, name: str, limit: int) -> bytes:
    with source.open(name) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PackageError("manifest_invalid", f"{name!r} supera {limit} bytes")
    return data


# --- Manifiesto -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainSpec:
    """Una cadena declarada en el manifiesto."""

    ref: ChainRef
    file: str
    first_sequence: int
    last_sequence: int
    last_hash: str


@dataclass(frozen=True)
class Manifest:
    organization_id: str
    chains: tuple[ChainSpec, ...]
    public_keys: dict[str, bytes]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _chain_spec(organization_id: str, item: object) -> ChainSpec:
    if not isinstance(item, dict) or item.keys() != _CHAIN_KEYS:
        raise PackageError("manifest_invalid", "una cadena no tiene las claves esperadas")
    kind, plant_id, name = item["kind"], item["plant_id"], item["file"]
    first, last, last_hash = item["first_sequence"], item["last_sequence"], item["last_hash"]
    if kind not in CHAIN_KINDS or not isinstance(kind, str):
        raise PackageError("manifest_invalid", f"tipo de cadena desconocido {kind!r}")
    if not (plant_id is None or (isinstance(plant_id, str) and _UUID_TEXT.fullmatch(plant_id))):
        raise PackageError("manifest_invalid", "plant_id no es un UUID")
    if kind == "audit" and plant_id is not None:
        raise PackageError("manifest_invalid", "la cadena de auditoría no lleva planta")
    if not isinstance(name, str) or _MEMBER.fullmatch(name) is None or name == MANIFEST:
        raise PackageError("manifest_invalid", f"ruta de archivo no permitida {name!r}")
    if not (_is_int(first) and _is_int(last) and isinstance(first, int) and isinstance(last, int)):
        raise PackageError("manifest_invalid", "secuencias de la cadena no enteras")
    if first < 1 or last < first - 1:
        raise PackageError("manifest_invalid", "rango de secuencias de la cadena no válido")
    if not isinstance(last_hash, str) or _HEX64_TEXT.fullmatch(last_hash) is None:
        raise PackageError("manifest_invalid", "last_hash no es un SHA-256 hexadecimal")
    return ChainSpec(ChainRef(kind, organization_id, plant_id), name, first, last, last_hash)


def _public_keys(items: object) -> dict[str, bytes]:
    if not isinstance(items, list):
        raise PackageError("manifest_invalid", "checkpoint_keys no es una lista")
    keys: dict[str, bytes] = {}
    for item in items:
        if not isinstance(item, dict) or item.keys() != _KEY_KEYS:
            raise PackageError("manifest_invalid", "una clave no tiene las claves esperadas")
        key_id, text = item["key_id"], item["public_key"]
        if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
            raise PackageError("manifest_invalid", "key_id no válido")
        if not isinstance(text, str) or _PUBLIC_KEY.fullmatch(text) is None:
            raise PackageError("manifest_invalid", f"clave pública no válida para {key_id!r}")
        if key_id in keys:
            raise PackageError("manifest_invalid", f"key_id repetido {key_id!r}")
        try:
            key = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise PackageError(
                "manifest_invalid", f"clave pública no válida para {key_id!r}"
            ) from None
        if base64.b64encode(key).decode("ascii") != text:
            # Base64 no canónico: otro texto para la misma clave no es la clave publicada.
            raise PackageError(
                "manifest_invalid", f"clave pública en base64 no canónico para {key_id!r}"
            )
        keys[key_id] = key
    return keys


def read_manifest(source: _Source) -> Manifest:
    """El manifiesto del paquete, validado; ``PackageError`` si no se puede usar."""
    try:
        document = parse_json(_read_all(source, MANIFEST, MAX_MANIFEST_BYTES))
    except ValueError as error:
        raise PackageError("manifest_invalid", str(error)) from None
    if not isinstance(document, dict):
        raise PackageError("manifest_invalid", "no es un objeto JSON")
    if document.get("format") != FORMAT:
        raise PackageError("unsupported_format", f"se esperaba format={FORMAT!r}")
    version = document.get("format_version")
    if not _is_int(version) or version != FORMAT_VERSION:
        raise PackageError(
            "unsupported_format",
            f"format_version {version!r}; este verificador lee la versión {FORMAT_VERSION}:"
            " use el verificador incluido en el paquete",
        )
    organization_id = document.get("organization_id")
    if not isinstance(organization_id, str) or _UUID_TEXT.fullmatch(organization_id) is None:
        raise PackageError("manifest_invalid", "organization_id no es un UUID")
    chains_document = document.get("chains")
    if not isinstance(chains_document, list) or not chains_document:
        raise PackageError("manifest_invalid", "chains debe ser una lista no vacía")
    chains = tuple(_chain_spec(organization_id, item) for item in chains_document)
    if len({spec.ref for spec in chains}) != len(chains):
        raise PackageError("manifest_invalid", "una cadena aparece dos veces")
    if len({spec.file for spec in chains}) != len(chains):
        raise PackageError("manifest_invalid", "dos cadenas comparten archivo")
    return Manifest(organization_id, chains, _public_keys(document.get("checkpoint_keys")))


# --- Entradas -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unreadable:
    """Línea que no es un documento JSON legible."""

    raw_id: str | None
    detail: str


def _iter_lines(source: _Source, name: str) -> Generator[tuple[int, object], None, None]:
    """``(número de línea, entrada)`` de un archivo JSON Lines; las líneas vacías se saltan."""
    with source.open(name) as stream:
        number = 0
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            number += 1
            if len(line) > MAX_LINE_BYTES:
                yield number, _Unreadable(None, f"la línea {number} supera {MAX_LINE_BYTES} bytes")
                return
            stripped = line.strip(b" \t\r\n")
            if not stripped:
                continue
            try:
                yield number, parse_json(stripped)
            except ValueError as error:
                match = _RAW_ID.search(stripped)
                raw_id = match.group(1).decode("ascii") if match else None
                yield number, _Unreadable(raw_id, f"línea {number}: {error}")


# --- Puntos de control anteriores ---------------------------------------------------------------


@dataclass
class PreviousCheckpoint:
    """Un punto de control de un paquete anterior, que ancla el prefijo de su cadena."""

    source: str
    chain: ChainRef | None
    sequence: int | None
    entry_id: str | None
    entry_hash: str | None
    status: str = "pending"
    reason: str | None = None
    detail: str = ""


def _is_checkpoint(entry: object) -> bool:
    return isinstance(entry, dict) and (
        entry.get("record_type") == "checkpoint" or entry.get("operation") == "checkpoint"
    )


def _checkpoint_from_entry(
    origin: str, entry: object, public_keys: dict[str, bytes]
) -> PreviousCheckpoint:
    """Valida por sí sola la entrada de un punto de control y devuelve su ancla."""
    invalid = PreviousCheckpoint(
        origin, None, None, None, None, "invalid", "previous_checkpoint_invalid"
    )
    if not isinstance(entry, dict) or not _is_checkpoint(entry):
        invalid.detail = "no es un registro de punto de control"
        return invalid
    audit = "entry_id" in entry
    organization_id, plant_id = entry.get("organization_id"), entry.get("plant_id")
    sequence, previous_hash = entry.get("chain_sequence"), entry.get("previous_hash")
    if not (
        isinstance(organization_id, str)
        and (audit or plant_id is None or isinstance(plant_id, str))
        and _is_int(sequence)
        and isinstance(sequence, int)
        and sequence >= 1
        and isinstance(previous_hash, str)
    ):
        invalid.detail = "le faltan la cadena, la secuencia o el enlace"
        return invalid
    chain = ChainRef(
        "audit" if audit else "ledger",
        organization_id,
        None if audit or not isinstance(plant_id, str) else plant_id,
    )
    walker = ChainWalker(chain, public_keys, start_sequence=sequence - 1, start_hash=previous_hash)
    failure = walker.feed(entry)
    invalid.chain, invalid.sequence = chain, sequence
    if failure is not None:
        invalid.entry_id = failure.entry_id
        invalid.detail = failure.message()
        return invalid
    result = walker.finish()
    seen = result.checkpoints[0]
    return PreviousCheckpoint(origin, chain, sequence, seen.entry_id, seen.entry_hash)


def load_previous_checkpoints(
    path: Path, public_keys: dict[str, bytes]
) -> list[PreviousCheckpoint]:
    """Puntos de control de ``path``: un ``.json`` con una entrada (o una lista) o un paquete.

    Falla cerrado: si ``path`` no se puede leer o no contiene ningún punto de control, devuelve
    una sola ancla ``invalid`` (quien pidió comprobar el prefijo no recibe un ``intact`` vacío).
    """
    origin = str(path)
    found: list[PreviousCheckpoint] = []
    try:
        if path.is_file() and not zipfile.is_zipfile(path):
            try:
                document = parse_json(path.read_bytes())
            except (OSError, ValueError) as error:
                raise PackageError("previous_checkpoint_invalid", str(error)) from None
            entries = document if isinstance(document, list) else [document]
            found = [_checkpoint_from_entry(origin, entry, public_keys) for entry in entries]
        else:
            source = _Source(path)
            try:
                manifest = read_manifest(source)
                for spec in manifest.chains:
                    last: object = None
                    for _, entry in _iter_lines(source, spec.file):
                        if _is_checkpoint(entry):
                            last = entry
                    if last is not None:
                        found.append(
                            _checkpoint_from_entry(f"{origin}:{spec.file}", last, public_keys)
                        )
            finally:
                source.close()
    except PackageError as error:
        detail = error.message()
    else:
        if found:
            return found
        detail = "no contiene ningún punto de control"
    return [
        PreviousCheckpoint(
            origin, None, None, None, None, "invalid", "previous_checkpoint_invalid", detail
        )
    ]


# --- Verificación ---------------------------------------------------------------------------------


@dataclass
class ChainReport:
    spec: ChainSpec
    result: ChainResult
    broken_line: int | None = None


@dataclass
class PackageReport:
    """Resultado de verificar un paquete."""

    package: str
    organization_id: str | None = None
    chains: list[ChainReport] = field(default_factory=list)
    previous: list[PreviousCheckpoint] = field(default_factory=list)
    package_error: PackageError | None = None
    unlisted_files: list[str] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return (
            self.package_error is None
            and bool(self.chains)
            and all(chain.result.intact for chain in self.chains)
            and all(previous.status == "matched" for previous in self.previous)
        )


def _walk_chain(
    source: _Source, spec: ChainSpec, public_keys: dict[str, bytes], anchors: dict[int, str]
) -> ChainReport:
    lines = _iter_lines(source, spec.file)
    try:
        first = next(lines, None)
        start_hash: str | None = None
        if spec.first_sequence > 1 and first is not None and isinstance(first[1], dict):
            # Paquete que no empieza en la génesis: el enlace con el prefijo viene del paquete.
            candidate = first[1].get("previous_hash")
            start_hash = candidate if isinstance(candidate, str) else None
        walker = ChainWalker(
            spec.ref,
            public_keys,
            start_sequence=spec.first_sequence - 1,
            start_hash=start_hash,
            anchors=anchors,
        )
        for number, entry in itertools.chain([first] if first is not None else [], lines):
            failure = walker.feed(entry)
            if failure is None:
                continue
            result = walker.finish()
            if isinstance(entry, _Unreadable):
                # La línea no se pudo leer: se nombra el registro si su identificador se lee.
                failure = Break(failure.sequence, entry.raw_id, "malformed", entry.detail)
                result = dataclasses.replace(result, broken=failure)
            return ChainReport(spec, result, number)
        return ChainReport(spec, walker.finish((spec.last_sequence, spec.last_hash)))
    finally:
        lines.close()


def verify_package(path: Path, previous_paths: Sequence[Path] = ()) -> PackageReport:
    """Verifica el paquete de ``path`` y, si se dan, los puntos de control anteriores."""
    report = PackageReport(str(path))
    try:
        source = _Source(path)
    except PackageError as error:
        report.package_error = error
        return report
    try:
        manifest = read_manifest(source)
        report.organization_id = manifest.organization_id
        for previous_path in previous_paths:
            report.previous.extend(load_previous_checkpoints(previous_path, manifest.public_keys))
        by_chain = {spec.ref: spec for spec in manifest.chains}
        anchors: dict[ChainRef, dict[int, str]] = {spec.ref: {} for spec in manifest.chains}
        for previous in report.previous:
            if previous.status != "pending" or previous.chain is None:
                continue
            if previous.chain not in by_chain:
                previous.status, previous.reason = (
                    "chain_missing",
                    "previous_checkpoint_chain_missing",
                )
                continue
            if previous.sequence is None or previous.entry_hash is None:
                continue
            chain_anchors = anchors[previous.chain]
            if chain_anchors.setdefault(previous.sequence, previous.entry_hash) != (
                previous.entry_hash
            ):
                previous.status, previous.reason = "invalid", "previous_checkpoint_invalid"
                previous.detail = "dos puntos de control anteriores discrepan en la misma secuencia"
        for spec in manifest.chains:
            report.chains.append(_walk_chain(source, spec, manifest.public_keys, anchors[spec.ref]))
        results = {chain.spec.ref: chain.result for chain in report.chains}
        for previous in report.previous:
            if previous.status != "pending" or previous.chain is None:
                continue
            if previous.sequence in results[previous.chain].anchors_matched:
                previous.status = "matched"
            else:
                previous.status, previous.reason = "not_matched", "previous_checkpoint_mismatch"
        listed = {MANIFEST, *(spec.file for spec in manifest.chains)}
        report.unlisted_files = [name for name in source.names() if name not in listed]
    except PackageError as error:
        report.package_error = error
    finally:
        source.close()
    return report


# --- Resultado ------------------------------------------------------------------------------------


def _break_document(failure: Break | None, line: int | None) -> dict[str, object] | None:
    if failure is None:
        return None
    return {
        "sequence": failure.sequence,
        "entry_id": failure.entry_id,
        "reason": failure.reason,
        "message": failure.message(),
        "line": line,
    }


def _chain_document(chain: ChainReport) -> dict[str, object]:
    result, spec = chain.result, chain.spec
    last_checkpoint = result.checkpoints[-1].sequence if result.checkpoints else None
    return {
        "kind": spec.ref.kind,
        "organization_id": spec.ref.organization_id,
        "plant_id": spec.ref.plant_id,
        "file": spec.file,
        "status": result.status,
        "from_genesis": spec.first_sequence == 1,
        "first_sequence": result.first_sequence,
        "last_sequence": result.last_sequence,
        "last_hash": result.last_hash,
        "entries": result.entries,
        "checkpoints_verified": len(result.checkpoints),
        "last_checkpoint_sequence": last_checkpoint,
        "entries_after_last_checkpoint": (
            result.last_sequence - last_checkpoint
            if last_checkpoint is not None
            else result.entries
        ),
        "previous_checkpoints_matched": list(result.anchors_matched),
        "broken": _break_document(result.broken, chain.broken_line),
    }


def _previous_document(previous: PreviousCheckpoint) -> dict[str, object]:
    reason = previous.reason
    message = None
    if reason is not None:
        message = PACKAGE_MESSAGES.get(reason) or MESSAGES.get(reason, reason)
        if previous.detail:
            message = f"{message}: {previous.detail}"
    return {
        "source": previous.source,
        "kind": previous.chain.kind if previous.chain else None,
        "organization_id": previous.chain.organization_id if previous.chain else None,
        "plant_id": previous.chain.plant_id if previous.chain else None,
        "sequence": previous.sequence,
        "entry_id": previous.entry_id,
        "status": previous.status,
        "reason": reason,
        "message": message,
    }


def report_document(report: PackageReport) -> dict[str, object]:
    """El resultado como documento JSON (el archivo de ``--out``)."""
    first_broken: dict[str, object] | None = None
    for chain in report.chains:
        if chain.result.broken is not None:
            first_broken = {
                "kind": chain.spec.ref.kind,
                "plant_id": chain.spec.ref.plant_id,
                **(_break_document(chain.result.broken, chain.broken_line) or {}),
            }
            break
    error = report.package_error
    return {
        "verifier": {"name": "vigia_verify", "format_version": FORMAT_VERSION},
        "package": report.package,
        "status": "intact" if report.intact else "broken",
        "organization_id": report.organization_id,
        "package_error": (
            None if error is None else {"reason": error.reason, "message": error.message()}
        ),
        "chains": [_chain_document(chain) for chain in report.chains],
        "previous_checkpoints": [_previous_document(previous) for previous in report.previous],
        "first_broken": first_broken,
        "unlisted_files": report.unlisted_files,
    }


def render_summary(report: PackageReport) -> str:
    """Resumen legible en español."""
    lines = [
        f"Verificador de paquetes de Vigía (formato {FORMAT_VERSION})",
        f"Paquete: {report.package}",
    ]
    if report.package_error is not None:
        lines.append(f"ERROR: {report.package_error.message()}")
    for chain in report.chains:
        result, name = chain.result, chain.spec.ref.describe()
        if result.intact:
            span = (
                f"secuencias {result.first_sequence} a {result.last_sequence}"
                if result.entries
                else "sin registros"
            )
            lines.append(
                f"- {name}: íntegra ({span}; {result.entries} registros; "
                f"{len(result.checkpoints)} puntos de control verificados)"
            )
            if chain.spec.first_sequence > 1:
                lines.append(
                    f"  El paquete empieza en la secuencia {chain.spec.first_sequence}: el enlace "
                    "con el prefijo se toma del propio paquete."
                )
        elif result.broken is not None:
            failure = result.broken
            where = f"en la secuencia {failure.sequence}" if failure.sequence is not None else ""
            who = f", registro {failure.entry_id}" if failure.entry_id else ""
            line = f", línea {chain.broken_line}" if chain.broken_line else ""
            lines.append(f"- {name}: ROTA {where}{who}{line}: {failure.message()}")
    for previous in report.previous:
        document = _previous_document(previous)
        chain_name = previous.chain.describe() if previous.chain else "cadena desconocida"
        if previous.status == "matched":
            lines.append(
                f"- Punto de control anterior ({chain_name}, secuencia {previous.sequence}): "
                "coincide; el prefijo no cambió."
            )
        else:
            lines.append(
                f"- Punto de control anterior ({chain_name}, secuencia {previous.sequence}): "
                f"NO COINCIDE: {document['message']}"
            )
    if report.unlisted_files:
        lines.append(
            "Aviso: archivos del paquete que el manifiesto no declara: "
            + ", ".join(report.unlisted_files)
        )
    lines.append("Resultado: ÍNTEGRO" if report.intact else "Resultado: ROTO")
    return "\n".join(lines) + "\n"


# --- Línea de órdenes -----------------------------------------------------------------------------


class _Formatter(argparse.HelpFormatter):
    """Ayuda con el prefijo de uso en español."""

    def add_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[Any],
        prefix: str | None = None,
    ) -> None:
        super().add_usage(usage, actions, groups, "uso: " if prefix is None else prefix)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigia_verify.py",
        description=(
            "Verifica, sin red y sin acceso a la plataforma, la integridad de un paquete exportado "
            "de Vigía: hashes, enlaces de cada cadena y firmas de los puntos de control."
        ),
        epilog=(
            "Código de salida: 0 si el paquete está íntegro; 1 si está roto o no se puede leer; "
            "2 si la orden es incorrecta."
        ),
        formatter_class=_Formatter,
        add_help=False,
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument(
        "package", metavar="paquete", type=Path, help="directorio o .zip del paquete"
    )
    parser.add_argument(
        "--previous-checkpoint",
        dest="previous",
        metavar="RUTA",
        type=Path,
        action="append",
        default=[],
        help=(
            "punto de control de un paquete anterior (archivo .json con el registro) o el paquete "
            "anterior entero; se puede repetir"
        ),
    )
    parser.add_argument(
        "--out", metavar="resultado.json", type=Path, help="archivo de resultado JSON"
    )
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y termina")
    parser.add_argument(
        "--version",
        action="version",
        version=f"vigia_verify (formato de paquete {FORMAT_VERSION})",
        help="muestra la versión y termina",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: 0 si íntegro, 1 si roto, 2 si la orden es incorrecta."""
    arguments = _parser().parse_args(argv)
    for path in (arguments.package, *arguments.previous):
        if not path.exists():
            print(f"vigia_verify.py: error: no existe {str(path)!r}", file=sys.stderr)
            return EXIT_USAGE
    report = verify_package(arguments.package, arguments.previous)
    sys.stdout.write(render_summary(report))
    if arguments.out is not None:
        text = json.dumps(report_document(report), ensure_ascii=False, indent=2) + "\n"
        try:
            arguments.out.write_text(text, encoding="utf-8")
        except OSError as error:
            print(
                f"vigia_verify.py: error: no se pudo escribir {str(arguments.out)!r}: {error}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    return EXIT_INTACT if report.intact else EXIT_BROKEN


# -------------------- vigia_platform.accreditation.export_verifier --------------------

"""Verificador endurecido del paquete exportado de U-04 (LC-LAZ-25; PAT-LAZ-SEG-02; BR-LAZ-90 y 91;
NFR-LAZ-23 y 53; PR-LAZ-28; escenario L-6).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10, porque
``tools/build_verifier.py`` lo copia, detrás de los cuatro módulos puros del verificador de U-02
(``pure_rfc8785``, ``pure_ed25519``, ``chain_walk`` y ``package_verifier``), en el archivo único
``resources/verifier/vigia_verify.py`` que viaja dentro de cada paquete (``accreditation.
verifier_bundle``). U-04 **no rediseña** el verificador de U-02: reutiliza su lectura estricta de
JSON, su canonicalización RFC 8785, su verificación Ed25519 y el mensaje firmado de los puntos de
control, y le añade la lectura del paquete exportado con estas defensas, en este orden:

1. **Volúmenes**: ``vol-01.zip`` … ``vol-99.zip`` seguidos, ninguno enlace simbólico. Antes de
   que ``zipfile`` lea el directorio central se lee el registro final del ZIP: sin ZIP64 (el
   paquete nunca lo usa), en un solo disco y con **un número de entradas acotado**
   (``max_volume_entries`` por volumen y ``max_entries`` en total).
2. **Cada entrada, sin leer sus datos**: ruta con ``..`` (``path_traversal``), absoluta o con
   letra de unidad (``absolute_path``), o fuera del patrón cerrado de rutas del paquete
   (``path_not_allowed``); **enlace simbólico** (``symlink``) u otro archivo especial
   (``special_file``); cifrada o con un método distinto de ``stored`` y ``deflate``
   (``compression_not_allowed``); tamaño descomprimido de más de **100 veces** el comprimido
   (``expansion_ratio``) o de más de **2 GB** (``entry_too_large``) ``[objetivos propios]``;
   repetida, también sin distinguir mayúsculas (``duplicate_entry``); un archivo que a la vez es
   directorio de otro (``path_not_allowed``).
3. **Manifiesto** (``manifest.json`` de ``vol-01``): JSON estricto y la forma que se usa; con
   ``--manifest-sha256``, su SHA-256 debe ser el que la plataforma registró al producirlo
   (``export_produced``).
4. **Ancla del manifiesto** (decisión del dueño A2 y B1, 2026-10-19): ``manifest.signature.json``
   de ``vol-01`` lleva la firma Ed25519, con la clave ``checkpoint`` de los puntos de control, de
   ``{kind, export_id, organization_id, plant_id, manifest_sha256, generated_at}``. Se comprueba
   con las claves públicas incluidas (leídas y hasheadas antes que el resto), su vigencia en
   ``generated_at`` y que el SHA-256 firmado es el del manifiesto leído
   (``manifest_signature_mismatch``). Como el manifiesto lista el hash de cada artefacto, la firma
   ata al expediente todo lo que el paquete lleva. **Sin firma válida y sin ``--manifest-sha256``
   el resultado nunca es ``intact``**: es ``unanchored`` (código 3), y no se extrae nada.
5. **Hashes del manifiesto antes de extraer nada**: cada artefacto existe en su volumen con su
   tamaño y nada sobra; después se lee cada uno **en flujo**, sin escribir en disco, y su SHA-256
   debe ser el declarado.
6. **Contenido**: claves públicas y puntos de control iguales en el manifiesto y en sus archivos;
   cada punto de control con forma y cobertura exactas, clave conocida, **vigente al firmar**
   (una clave retirada no firma después de su retiro) y firma Ed25519 válida; el
   ``content_hash`` de cada registro con contenido; el ``entry_hash`` de cada entrada de
   auditoría, recalculado entero con el sobre de U-02; los enlaces entre registros y entradas
   consecutivos y con lo que cubre cada punto de control; y los conteos por estado, los clips,
   los adjuntos y las entradas ausentes del manifiesto frente a lo que el paquete lleva. Un
   adjunto que cita una entrada de evidencia viaja con sus bytes o consta **no recuperable**
   (manifiesto 2, DR-LAZ-04) con la declaración ``closure_attachment_unrecoverable`` del hallazgo
   (mismo hash y fecha); uno ausente sin declarar es ``manifest_mismatch``.
7. Solo si todo lo anterior está íntegro **y anclado** y se pide ``--extract``, se escriben los
   archivos en un directorio vacío, volviendo a comprobar cada hash mientras se escriben.

Sin el ancla, las comprobaciones 5 y 6 solo prueban la coherencia interna: el paquete exporta cada
registro sin su actor ni su alcance (no se recalcula su ``record_hash``) y solo una parte de la
cadena, así que quien re-hashea el manifiesto puede cambiar el contenido o el estado de un
hallazgo de forma coherente. Por eso sin ancla el resultado es ``unanchored``. La firma se
comprueba con las claves que viajan en el paquete, y quien añada su propia clave a ese conjunto
puede firmar un manifiesto rehecho: por eso el resumen y ``--out`` dicen **qué clave** ancló el
paquete (``key_id`` y SHA-256 de su clave pública), para compararla con la que publica la
plataforma (``/.well-known/vigia-checkpoint-keys``).

Resultado: ``intact`` (código 0), ``broken`` (código 1) con la **posición** del primer fallo
(volumen, artefacto, desplazamiento de su cabecera en el volumen y puntero JSON dentro del
artefacto) o ``unanchored`` (código 3); 2 si la orden es incorrecta. Sin red, sin hora del sistema
y sin secretos.
"""


EXPORT_VERIFIER_VERSION: Final = "export-verifier.2"
"""Versión de este verificador: la que el manifiesto declara en ``verifier_version``. La 2
(VIG-274) lee el manifiesto 2, con los adjuntos declarados no recuperables (DR-LAZ-04)."""

EXPORT_MANIFEST_VERSION: Final = 2
EXPORT_MANIFEST_VERSIONS: Final = (1, 2)
"""Las versiones del manifiesto que este verificador lee: un paquete es permanente, así que la 2
sigue verificando los de la 1."""
UNRECOVERABLE_RECORD_TYPE: Final = "closure_attachment_unrecoverable"
_UNRECOVERABLE_KEYS: Final = frozenset(
    {"declaration_id", "detected_at", "reason_es", "record_id", "sha256"}
)
EXPORT_MANIFEST_NAME: Final = "manifest.json"
EXPORT_VERIFIER_NAME: Final = "vigia_verify.py"
EXPORT_VERIFIER_ABSENT: Final = "absent"
EXPORT_SIGNATURE_NAME: Final = "manifest.signature.json"
"""La firma del SHA-256 del manifiesto, en ``vol-01`` junto a él (no se lista como artefacto)."""
MANIFEST_SIGNATURE_KIND: Final = "vigia.export.manifest"
"""``kind`` del mensaje firmado: separa esta firma de la de un punto de control."""
MANIFEST_SIGNATURE_PAYLOAD_KEYS: Final = frozenset(
    {"export_id", "generated_at", "kind", "manifest_sha256", "organization_id", "plant_id"}
)
MANIFEST_SIGNATURE_DECLARATION: Final = {
    "path": EXPORT_SIGNATURE_NAME,
    "algorithm": "Ed25519",
    "key_purpose": "checkpoint",
    "payload_kind": MANIFEST_SIGNATURE_KIND,
}
"""Lo que el manifiesto declara en ``manifest_signature`` cuando lleva firma."""
EXIT_UNANCHORED: Final = 3
"""Sin firma válida del manifiesto ni ``--manifest-sha256``: la integridad no está anclada."""
MAX_SIGNATURE_BYTES: Final = 64 * 1024

MAX_EXPANSION_RATIO: Final = 100
"""Una entrada no se descomprime a más de 100 veces su tamaño comprimido ``[objetivo propio]``."""
MAX_ENTRY_BYTES: Final = 2 * 1000**3
"""Ninguna entrada pasa de 2 GB descomprimida ``[objetivo propio]`` (el tamaño de un volumen)."""
MAX_VOLUME_ENTRIES: Final = 65_535
"""Lo más que cabe en un ZIP sin ZIP64."""
MAX_ENTRIES: Final = 1_000_000
"""Entradas de todo el paquete ``[objetivo propio]``."""
MAX_VOLUMES: Final = 99
MAX_EXPORT_MANIFEST_BYTES: Final = 64 * 1024 * 1024
"""Lo que ``vol-01`` reserva para el manifiesto."""
MAX_JSON_ARTIFACT_BYTES: Final = 512 * 1024 * 1024
"""Tope de un artefacto JSON que se interpreta además de hashearse ``[objetivo propio]``."""
READ_CHUNK: Final = 1024 * 1024

_VOLUME_NAME: Final = re.compile(r"vol-([0-9]{2})\.zip")
_ENTRY_PATH: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_MAX_PATH: Final = 255
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_UUID_FORM: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256_FORM: Final = re.compile(r"[0-9a-f]{64}")
_STAMP_FORM: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNATURE_FORM: Final = re.compile(r"[A-Za-z0-9+/]{86}==")
_PUBLIC_KEY_FORM: Final = re.compile(r"[A-Za-z0-9+/]{43}=")
_KEY_ID_FORM: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_END_OF_DIRECTORY: Final = b"PK\x05\x06"
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_END_OF_DIRECTORY_SIZE: Final = 22
_MAX_COMMENT: Final = 65_535
_ALLOWED_METHODS: Final = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

JSON_KINDS: Final = ("findings_json", "audit", "checkpoint", "public_key")
"""Artefactos que, además de hashearse, se interpretan."""
OPEN_LIFECYCLE_STATES: Final = ("received", "in_review", "open", "action_assigned", "overdue")
"""Los estados que el manifiesto cuenta como abiertos (``accreditation.manifest.build``)."""
KEY_STATUSES: Final = ("active", "overlapping", "retired")
CHECKPOINT_CONTENT_KEYS: Final = frozenset(
    {"covered_hash", "covered_sequence", "key_id", "signature", "taken_at"}
)
_ARTIFACT_KEYS: Final = frozenset({"kind", "path", "sha256", "size_bytes", "volume"})
_KEY_KEYS_EXPORT: Final = frozenset(
    {"algorithm", "key_id", "public_key", "status", "valid_from", "valid_until"}
)
_CHECKPOINT_ENTRY_KEYS: Final = frozenset(
    {"content", "entry_hash", "entry_id", "kind", "plant_id", "sequence"}
)
_AUDIT_EXPORT_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "correlation_id",
        "entry_hash",
        "entry_id",
        "filters",
        "occurred_at",
        "operation",
        "outcome",
        "previous_hash",
        "resource_id",
        "resource_kind",
        "result_count",
        "scope_plant_id",
        "scope_zone_id",
    }
)
_AUDIT_ACTOR_KEYS: Final = frozenset(
    {"concession_id", "display_name_snapshot", "id", "kind", "role_in_use", "unit"}
)

EXPORT_MESSAGES: Final = {
    "package_unreadable": "el paquete no se puede leer",
    "too_many_entries": "el paquete tiene más entradas de las permitidas",
    "zip64_not_allowed": "el volumen usa ZIP64 o varios discos: el paquete nunca los usa",
    "path_traversal": "una ruta sube de directorio con «..»",
    "absolute_path": "una ruta es absoluta",
    "path_not_allowed": "una ruta no tiene la forma de las rutas del paquete",
    "symlink": "una entrada es un enlace simbólico",
    "special_file": "una entrada no es un archivo regular",
    "duplicate_entry": "una entrada está repetida",
    "compression_not_allowed": "una entrada está cifrada o usa un método de compresión no admitido",
    "expansion_ratio": "una entrada se expande más de 100 veces su tamaño comprimido",
    "entry_too_large": "una entrada supera los 2 GB descomprimida",
    "manifest_missing": "falta el manifiesto en vol-01.zip",
    "manifest_invalid": "el manifiesto no es válido",
    "manifest_hash_mismatch": "el SHA-256 del manifiesto no es el registrado por la plataforma",
    "artifact_missing": "falta un artefacto que el manifiesto declara",
    "artifact_size_mismatch": "el tamaño de un artefacto no es el que declara el manifiesto",
    "unlisted_entry": "el paquete lleva una entrada que el manifiesto no declara",
    "artifact_unreadable": "un artefacto no se puede descomprimir íntegro",
    "artifact_hash_mismatch": "el SHA-256 de un artefacto no es el que declara el manifiesto",
    "artifact_invalid": "un artefacto no tiene la forma esperada",
    "manifest_mismatch": "el manifiesto no coincide con lo que el paquete lleva",
    "key_invalid": "una clave pública no es válida",
    "checkpoint_invalid": "un punto de control no tiene la forma esperada",
    "unknown_key": "la clave de un punto de control no está entre las claves públicas incluidas",
    "key_not_valid_at_signing": "la clave no estaba vigente cuando se tomó el punto de control",
    "bad_signature": "la firma de un punto de control no verifica",
    "checkpoint_mismatch": "un registro no coincide con lo que cubre un punto de control firmado",
    "content_not_canonical": "un contenido no tiene forma canónica RFC 8785",
    "content_hash_mismatch": "el hash del contenido de un registro no coincide",
    "entry_hash_mismatch": "el hash de una entrada de auditoría no coincide con su sobre",
    "previous_hash_mismatch": "el enlace con el registro anterior no coincide",
    "record_conflict": "dos registros distintos ocupan la misma secuencia de la cadena",
    "extract_failed": "no se pudo extraer un archivo tal como se verificó",
    "manifest_signature_invalid": "la firma del manifiesto no tiene la forma esperada o falta",
    "manifest_signature_mismatch": "la firma del manifiesto no corresponde a este manifiesto",
}
"""Motivo de rotura → explicación en español (lista cerrada)."""


# --- Resultado ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportPosition:
    """Dónde está el primer fallo: volumen, artefacto, desplazamiento y puntero JSON."""

    volume: str | None = None
    artifact: str | None = None
    offset: int | None = None
    """Byte del volumen donde empieza la cabecera local del artefacto."""
    pointer: str | None = None
    """Puntero JSON (RFC 6901) dentro del artefacto."""


@dataclass(frozen=True)
class ExportFailure:
    reason: str
    position: ExportPosition
    detail: str = ""

    def message(self) -> str:
        text = EXPORT_MESSAGES.get(self.reason, self.reason)
        return f"{text} ({self.detail})" if self.detail else text


class ExportBroken(Exception):
    """El paquete está roto: lleva el primer fallo."""

    def __init__(self, failure: ExportFailure) -> None:
        super().__init__(failure.reason)
        self.failure = failure


@dataclass(frozen=True)
class ExportLimits:
    """Topes del verificador; los de producción son los del diseño."""

    max_expansion_ratio: int = MAX_EXPANSION_RATIO
    max_entry_bytes: int = MAX_ENTRY_BYTES
    max_volume_entries: int = MAX_VOLUME_ENTRIES
    max_entries: int = MAX_ENTRIES
    max_manifest_bytes: int = MAX_EXPORT_MANIFEST_BYTES
    max_json_bytes: int = MAX_JSON_ARTIFACT_BYTES


@dataclass
class ExportReport:
    """Resultado de verificar un paquete exportado."""

    package: str
    failure: ExportFailure | None = None
    volumes: list[str] = field(default_factory=list)
    manifest_sha256: str | None = None
    declared_verifier_version: str | None = None
    artifacts_verified: int = 0
    checkpoints_verified: int = 0
    records_checked: int = 0
    audit_entries_checked: int = 0
    extracted_to: str | None = None
    anchored_by: list[str] = field(default_factory=list)
    signing_key: dict[str, str] | None = None
    """``key_id`` y SHA-256 de la clave pública que firmó el manifiesto."""
    """``manifest_signature`` y/o ``manifest_sha256``: lo que ata el manifiesto a la plataforma."""

    @property
    def intact(self) -> bool:
        """Íntegro **y anclado**: sin ancla nunca es ``intact`` (decisión B1)."""
        return self.failure is None and bool(self.anchored_by)

    @property
    def status(self) -> str:
        if self.failure is not None:
            return "broken"
        return "intact" if self.anchored_by else "unanchored"


def _fail(
    reason: str,
    detail: str = "",
    *,
    volume: str | None = None,
    artifact: str | None = None,
    offset: int | None = None,
    pointer: str | None = None,
) -> ExportBroken:
    return ExportBroken(
        ExportFailure(reason, ExportPosition(volume, artifact, offset, pointer), detail)
    )


# --- Volúmenes y entradas -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    volume: str
    info: zipfile.ZipInfo

    @property
    def name(self) -> str:
        return self.info.filename

    @property
    def offset(self) -> int:
        return self.info.header_offset


def find_volumes(path: Path) -> list[Path]:
    """Los volúmenes del paquete: un directorio con ``vol-NN.zip`` o la ruta de ``vol-01.zip``.

    Cualquier otro ``.zip`` se lee como paquete de un solo volumen.
    """
    if path.is_symlink():
        raise _fail("symlink", "el paquete es un enlace simbólico", volume=path.name)
    if path.is_dir():
        directory = path
        names = sorted(child.name for child in path.iterdir() if _VOLUME_NAME.fullmatch(child.name))
    elif path.is_file():
        if _VOLUME_NAME.fullmatch(path.name) is None:
            return [path]
        if path.name != "vol-01.zip":
            raise _fail("package_unreadable", "indique vol-01.zip o el directorio de los volúmenes")
        directory = path.parent
        names = sorted(
            child.name for child in directory.iterdir() if _VOLUME_NAME.fullmatch(child.name)
        )
    else:
        raise _fail("package_unreadable", "no es un directorio ni un archivo")
    if not names:
        raise _fail("package_unreadable", "no hay ningún vol-NN.zip")
    expected = [f"vol-{number:02d}.zip" for number in range(1, len(names) + 1)]
    if names != expected or len(names) > MAX_VOLUMES:
        raise _fail(
            "package_unreadable", "los volúmenes no van de vol-01.zip en adelante sin huecos"
        )
    volumes = [directory / name for name in names]
    for volume in volumes:
        if volume.is_symlink():
            raise _fail("symlink", "el volumen es un enlace simbólico", volume=volume.name)
        if not volume.is_file():
            raise _fail("package_unreadable", "el volumen no es un archivo", volume=volume.name)
    return volumes


def _end_of_directory_entries(handle: IO[bytes], volume: str) -> int:
    """Entradas que declara el registro final del ZIP, leído antes que el directorio central."""
    size = handle.seek(0, 2)
    tail_size = min(size, _END_OF_DIRECTORY_SIZE + _MAX_COMMENT)
    handle.seek(size - tail_size)
    tail = handle.read(tail_size)
    index = tail.rfind(_END_OF_DIRECTORY)
    if index < 0 or len(tail) - index < _END_OF_DIRECTORY_SIZE:
        raise _fail("package_unreadable", "no es un ZIP", volume=volume)
    disk, disk_with_directory, on_disk, total, directory_size, directory_offset = struct.unpack(
        "<4H2L", tail[index + 4 : index + 20]
    )
    zip64 = index >= 20 and tail[index - 20 : index - 16] == _ZIP64_LOCATOR
    if (
        zip64
        or disk != 0
        or disk_with_directory != 0
        or on_disk != total
        or total == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise _fail("zip64_not_allowed", volume=volume)
    return int(total)


def check_entry_name(entry: _Entry) -> None:
    """Rechaza ``..``, rutas absolutas y todo lo que no sea una ruta relativa del paquete."""
    raw = entry.info.orig_filename
    name = entry.name
    where: dict[str, Any] = {"volume": entry.volume, "artifact": raw, "offset": entry.offset}
    parts = raw.replace("\\", "/").split("/")
    if ".." in parts:
        raise _fail("path_traversal", **where)
    if raw.startswith(("/", "\\")) or _DRIVE.match(raw) is not None:
        raise _fail("absolute_path", **where)
    if raw != name or len(raw) > _MAX_PATH or _ENTRY_PATH.fullmatch(raw) is None:
        raise _fail("path_not_allowed", **where)


def check_entry_type(entry: _Entry) -> None:
    """Rechaza enlaces simbólicos y cualquier entrada que no sea un archivo regular."""
    mode = entry.info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK:
        raise _fail("symlink", volume=entry.volume, artifact=entry.name, offset=entry.offset)
    if kind not in (0, stat.S_IFREG) or entry.info.is_dir():
        raise _fail("special_file", volume=entry.volume, artifact=entry.name, offset=entry.offset)


def check_entry_expansion(entry: _Entry, limits: ExportLimits) -> None:
    """Rechaza métodos no admitidos y expansiones de más de 100 veces o de más de 2 GB."""
    info = entry.info
    where: dict[str, Any] = {"volume": entry.volume, "artifact": entry.name, "offset": entry.offset}
    if info.flag_bits & 0x1 or info.compress_type not in _ALLOWED_METHODS:
        raise _fail("compression_not_allowed", **where)
    if info.file_size > limits.max_entry_bytes:
        raise _fail("entry_too_large", f"{info.file_size} bytes", **where)
    if info.compress_type == zipfile.ZIP_STORED and info.file_size != info.compress_size:
        raise _fail("expansion_ratio", "entrada sin comprimir con tamaños distintos", **where)
    if info.file_size > limits.max_expansion_ratio * info.compress_size:
        raise _fail(
            "expansion_ratio",
            f"{info.file_size} bytes de {info.compress_size} comprimidos",
            **where,
        )


class _Package:
    """Los volúmenes abiertos y sus entradas, ya comprobadas sin leer ningún dato."""

    def __init__(self, volumes: Sequence[Path], limits: ExportLimits) -> None:
        self.limits = limits
        self.archives: dict[str, zipfile.ZipFile] = {}
        self.entries: dict[str, _Entry] = {}
        self._files: set[str] = set()
        self._directories: set[str] = set()
        try:
            self._open(volumes)
        except BaseException:
            self.close()
            raise

    def _open(self, volumes: Sequence[Path]) -> None:
        total = 0
        for number, path in enumerate(volumes, start=1):
            # El nombre lógico del volumen (un paquete de un volumen puede llamarse de otro modo).
            volume = f"vol-{number:02d}.zip"
            try:
                handle = path.open("rb")
            except OSError as error:
                raise _fail("package_unreadable", str(error), volume=volume) from None
            try:
                declared = _end_of_directory_entries(handle, volume)
            except BaseException:
                handle.close()
                raise
            total += declared
            if declared > self.limits.max_volume_entries or total > self.limits.max_entries:
                handle.close()
                raise _fail("too_many_entries", f"{total} entradas", volume=volume)
            try:
                archive = zipfile.ZipFile(handle)
            except (zipfile.BadZipFile, OSError, ValueError, EOFError) as error:
                handle.close()
                raise _fail("package_unreadable", str(error), volume=volume) from None
            self.archives[volume] = archive
            infos = archive.infolist()
            if len(infos) != declared:
                raise _fail("package_unreadable", "el directorio central no cuadra", volume=volume)
            for info in infos:
                entry = _Entry(volume, info)
                check_entry_name(entry)
                check_entry_type(entry)
                check_entry_expansion(entry, self.limits)
                self._check_collisions(entry)
                self.entries[entry.name] = entry

    def _check_collisions(self, entry: _Entry) -> None:
        """Dos rutas que al extraer serían el mismo archivo (también en sistemas de archivos que
        no distinguen mayúsculas), o un archivo que a la vez es directorio de otro."""
        where: dict[str, Any] = {
            "volume": entry.volume,
            "artifact": entry.name,
            "offset": entry.offset,
        }
        folded = entry.name.casefold()
        if folded in self._files:
            raise _fail("duplicate_entry", **where)
        parts = folded.split("/")
        prefixes = ["/".join(parts[:index]) for index in range(1, len(parts))]
        if folded in self._directories or any(prefix in self._files for prefix in prefixes):
            raise _fail("path_not_allowed", "un archivo es a la vez directorio de otro", **where)
        self._files.add(folded)
        self._directories.update(prefixes)

    def stream(self, entry: _Entry) -> Iterator[bytes]:
        """Los bytes descomprimidos de ``entry``, en trozos; nunca más de los declarados."""
        where: dict[str, Any] = {
            "volume": entry.volume,
            "artifact": entry.name,
            "offset": entry.offset,
        }
        archive = self.archives[entry.volume]
        read = 0
        try:
            with archive.open(entry.info) as stream:
                while True:
                    chunk = stream.read(READ_CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > entry.info.file_size:
                        raise _fail("artifact_unreadable", "más bytes de los declarados", **where)
                    yield chunk
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError) as error:
            raise _fail("artifact_unreadable", str(error), **where) from None
        if read != entry.info.file_size:
            raise _fail("artifact_unreadable", "menos bytes de los declarados", **where)

    def close(self) -> None:
        for archive in self.archives.values():
            fp = archive.fp
            archive.close()
            if fp is not None:
                fp.close()


# --- Manifiesto ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Artifact:
    path: str
    kind: str
    sha256: str
    size_bytes: int
    volume: int
    pointer: str


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _manifest_error(detail: str, pointer: str) -> ExportBroken:
    return _fail(
        "manifest_invalid",
        detail,
        volume="vol-01.zip",
        artifact=EXPORT_MANIFEST_NAME,
        pointer=pointer,
    )


def _require(document: Mapping[str, Any], key: str, check: Any, pointer: str = "") -> Any:
    value = document.get(key)
    if key not in document or not check(value):
        raise _manifest_error(f"{key} no es válido", f"{pointer}/{key}")
    return value


def _artifacts(items: object) -> list[_Artifact]:
    if not isinstance(items, list):
        raise _manifest_error("artifacts no es una lista", "/artifacts")
    artifacts: list[_Artifact] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        pointer = f"/artifacts/{index}"
        if not isinstance(item, dict) or item.keys() != _ARTIFACT_KEYS:
            raise _manifest_error("un artefacto no tiene las claves esperadas", pointer)
        path, kind, sha256 = item["path"], item["kind"], item["sha256"]
        size, volume = item["size_bytes"], item["volume"]
        if (
            not _matches(_ENTRY_PATH, path)
            or len(path) > _MAX_PATH
            or path in (EXPORT_MANIFEST_NAME, EXPORT_SIGNATURE_NAME)
            or path in seen
        ):
            raise _manifest_error("ruta de artefacto no válida o repetida", pointer + "/path")
        if not isinstance(kind, str) or not kind:
            raise _manifest_error("kind no es válido", pointer + "/kind")
        if not _matches(_SHA256_FORM, sha256):
            raise _manifest_error("sha256 no es válido", pointer + "/sha256")
        if not _is_count(size):
            raise _manifest_error("size_bytes no es válido", pointer + "/size_bytes")
        if not _is_count(volume) or not 1 <= volume <= MAX_VOLUMES:
            raise _manifest_error("volume no es válido", pointer + "/volume")
        seen.add(path)
        artifacts.append(_Artifact(path, kind, sha256, size, volume, pointer))
    return artifacts


@dataclass(frozen=True)
class _Manifest:
    document: dict[str, Any]
    organization_id: str
    plant_id: str
    artifacts: list[_Artifact]
    verifier_version: str


def _read_manifest(package: _Package, expected_sha256: str | None) -> tuple[_Manifest, str]:
    entry = package.entries.get(EXPORT_MANIFEST_NAME)
    if entry is None or entry.volume != "vol-01.zip":
        raise _fail("manifest_missing", volume="vol-01.zip", artifact=EXPORT_MANIFEST_NAME)
    if entry.info.file_size > package.limits.max_manifest_bytes:
        raise _fail(
            "manifest_invalid",
            "el manifiesto supera su tope",
            volume=entry.volume,
            artifact=entry.name,
            offset=entry.offset,
        )
    data = b"".join(package.stream(entry))
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise _fail(
            "manifest_hash_mismatch", volume=entry.volume, artifact=entry.name, offset=entry.offset
        )
    try:
        document = parse_json(data)
    except ValueError as error:
        raise _manifest_error(str(error), "") from None
    if not isinstance(document, dict):
        raise _manifest_error("no es un objeto JSON", "")
    version = document.get("manifest_version")
    if not _is_count(version) or version not in EXPORT_MANIFEST_VERSIONS:
        raise _manifest_error(
            f"manifest_version {version!r}: use el verificador incluido en el paquete",
            "/manifest_version",
        )
    organization = _require(document, "organization", lambda value: isinstance(value, dict))
    organization_id = _require(
        organization, "organization_id", lambda value: _matches(_UUID_FORM, value), "/organization"
    )
    plant = _require(document, "plant", lambda value: isinstance(value, dict))
    plant_id = _require(plant, "plant_id", lambda value: _matches(_UUID_FORM, value), "/plant")
    verifier_version = _require(document, "verifier_version", lambda value: isinstance(value, str))
    for key in ("checkpoints", "checkpoint_public_keys", "zones", "clips", "attachments"):
        _require(document, key, lambda value: isinstance(value, list))
    _require(document, "absent_entries", lambda value: isinstance(value, list))
    manifest = _Manifest(
        document, organization_id, plant_id, _artifacts(document.get("artifacts")), verifier_version
    )
    return manifest, digest


# --- Hashes antes de extraer --------------------------------------------------------------------


def check_artifact_hash(entry: _Entry, artifact: _Artifact, digest: str) -> None:
    """El SHA-256 de los bytes leídos es el que declara el manifiesto."""
    if digest != artifact.sha256:
        raise _fail(
            "artifact_hash_mismatch",
            volume=entry.volume,
            artifact=artifact.path,
            offset=entry.offset,
            pointer=artifact.pointer,
        )


def verify_artifact_hashes(
    package: _Package, manifest: _Manifest, report: ExportReport
) -> dict[str, Any]:
    """Cada artefacto existe con su tamaño y su SHA-256, y nada sobra; devuelve los JSON leídos."""
    listed = {EXPORT_MANIFEST_NAME}
    signature = package.entries.get(EXPORT_SIGNATURE_NAME)
    if signature is not None and signature.volume == "vol-01.zip":
        listed.add(EXPORT_SIGNATURE_NAME)
    for artifact in manifest.artifacts:
        volume = f"vol-{artifact.volume:02d}.zip"
        entry = package.entries.get(artifact.path)
        if entry is None or entry.volume != volume:
            raise _fail(
                "artifact_missing",
                "no está en su volumen",
                volume=volume,
                artifact=artifact.path,
                pointer=artifact.pointer,
            )
        if entry.info.file_size != artifact.size_bytes:
            raise _fail(
                "artifact_size_mismatch",
                volume=volume,
                artifact=artifact.path,
                offset=entry.offset,
                pointer=artifact.pointer,
            )
        listed.add(artifact.path)
    for name, entry in sorted(
        package.entries.items(), key=lambda item: (item[1].volume, item[1].offset)
    ):
        if name not in listed:
            raise _fail("unlisted_entry", volume=entry.volume, artifact=name, offset=entry.offset)
    documents: dict[str, Any] = {}
    for artifact in manifest.artifacts:
        entry = package.entries[artifact.path]
        keep = artifact.kind in JSON_KINDS and artifact.size_bytes <= package.limits.max_json_bytes
        hasher = hashlib.sha256()
        kept: list[bytes] = []
        for chunk in package.stream(entry):
            hasher.update(chunk)
            if keep:
                kept.append(chunk)
        check_artifact_hash(entry, artifact, hasher.hexdigest())
        report.artifacts_verified += 1
        if keep:
            try:
                documents[artifact.path] = parse_json(b"".join(kept))
            except ValueError as error:
                raise _fail(
                    "artifact_invalid",
                    str(error),
                    volume=entry.volume,
                    artifact=artifact.path,
                    offset=entry.offset,
                ) from None
    return documents


# --- Ancla del manifiesto -----------------------------------------------------------------------


def _parse_key_items(
    items: object, where: Callable[[str], dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Las claves públicas ``checkpoint`` incluidas, validadas, por ``key_id``."""
    if not isinstance(items, list):
        raise _fail("artifact_invalid", "keys no es una lista", **where("/keys"))
    keys: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        position = where(f"/keys/{index}")
        if not isinstance(item, dict) or item.keys() != _KEY_KEYS_EXPORT:
            raise _fail("key_invalid", "claves inesperadas", **position)
        key_id, text = item["key_id"], item["public_key"]
        if not _matches(_KEY_ID_FORM, key_id) or key_id in keys:
            raise _fail("key_invalid", "key_id no válido o repetido", **position)
        if item["algorithm"] != "Ed25519" or item["status"] not in KEY_STATUSES:
            raise _fail("key_invalid", "algoritmo o estado no válido", **position)
        valid_from, valid_until = item["valid_from"], item["valid_until"]
        if not (
            _matches(_STAMP_FORM, valid_from)
            and _matches(_STAMP_FORM, valid_until)
            and valid_from < valid_until
        ):
            raise _fail("key_invalid", "vigencia no válida", **position)
        raw = _canonical_base64(text, _PUBLIC_KEY_FORM)
        if raw is None or len(raw) != 32:
            raise _fail("key_invalid", "clave pública no válida", **position)
        keys[key_id] = {"raw": raw, "valid_from": valid_from, "valid_until": valid_until}
    return keys


def _included_keys(package: _Package, manifest: _Manifest) -> dict[str, dict[str, Any]]:
    """Las claves del artefacto ``public_key``, leído y hasheado antes que el resto."""
    found = [artifact for artifact in manifest.artifacts if artifact.kind == "public_key"]
    if len(found) != 1:
        raise _fail(
            "manifest_signature_invalid",
            "sin un artefacto de claves públicas no se puede comprobar la firma",
            volume="vol-01.zip",
            artifact=EXPORT_SIGNATURE_NAME,
        )
    artifact = found[0]
    entry = package.entries.get(artifact.path)
    volume = f"vol-{artifact.volume:02d}.zip"
    if entry is None or entry.volume != volume:
        raise _fail("artifact_missing", volume=volume, artifact=artifact.path)
    if entry.info.file_size != artifact.size_bytes or (
        artifact.size_bytes > package.limits.max_json_bytes
    ):
        raise _fail(
            "artifact_size_mismatch", volume=volume, artifact=artifact.path, offset=entry.offset
        )
    data = b"".join(package.stream(entry))
    check_artifact_hash(entry, artifact, hashlib.sha256(data).hexdigest())

    def where(pointer: str) -> dict[str, Any]:
        return {
            "volume": entry.volume,
            "artifact": artifact.path,
            "offset": entry.offset,
            "pointer": pointer,
        }

    try:
        document = parse_json(data)
    except ValueError as error:
        raise _fail("artifact_invalid", str(error), **where("")) from None
    if not isinstance(document, dict) or document.keys() != {"keys"}:
        raise _fail("artifact_invalid", "se espera {keys}", **where(""))
    return _parse_key_items(document["keys"], where)


def verify_manifest_anchor(
    package: _Package, manifest: _Manifest, digest: str, report: ExportReport
) -> None:
    """La firma del SHA-256 del manifiesto con las claves incluidas (decisión A2).

    Sin ``manifest.signature.json`` el manifiesto queda sin firmar (``unanchored`` salvo
    ``--manifest-sha256``); una firma que no corresponde, de una clave desconocida, no vigente o
    que no verifica, rompe el paquete.
    """
    declared = manifest.document.get("manifest_signature")
    entry = package.entries.get(EXPORT_SIGNATURE_NAME)
    where: dict[str, Any] = {"volume": "vol-01.zip", "artifact": EXPORT_SIGNATURE_NAME}
    if entry is None:
        if declared is not None:
            raise _fail(
                "manifest_signature_invalid", "el manifiesto declara una firma que falta", **where
            )
        return
    where["offset"] = entry.offset
    if entry.volume != "vol-01.zip" or entry.info.file_size > MAX_SIGNATURE_BYTES:
        raise _fail("manifest_signature_invalid", "fuera de vol-01 o demasiado grande", **where)
    if declared != MANIFEST_SIGNATURE_DECLARATION:
        raise _fail(
            "manifest_signature_invalid",
            "el manifiesto no declara la firma que viaja",
            **{**where, "artifact": EXPORT_MANIFEST_NAME, "pointer": "/manifest_signature"},
        )
    keys = _included_keys(package, manifest)
    raw_signature = b"".join(package.stream(entry))
    try:
        document = parse_json(raw_signature)
    except ValueError as error:
        raise _fail("manifest_signature_invalid", str(error), **where) from None
    if not isinstance(document, dict) or document.keys() != {"key_id", "payload", "signature"}:
        raise _fail("manifest_signature_invalid", "se espera {payload, key_id, signature}", **where)
    try:
        canonical = canonicalize(document) == raw_signature
    except CanonicalizationError:
        canonical = False
    if not canonical:
        # Los bytes que se extraen son exactamente los que firmó la plataforma (RFC 8785).
        raise _fail("manifest_signature_invalid", "no está en forma canónica RFC 8785", **where)
    payload, key_id = document["payload"], document["key_id"]
    if (
        not isinstance(payload, dict)
        or payload.keys() != MANIFEST_SIGNATURE_PAYLOAD_KEYS
        or payload["kind"] != MANIFEST_SIGNATURE_KIND
        or not _matches(_STAMP_FORM, payload["generated_at"])
        or not isinstance(key_id, str)
    ):
        raise _fail("manifest_signature_invalid", "mensaje firmado no válido", **where)
    expected = {
        "manifest_sha256": digest,
        "export_id": manifest.document.get("export_id"),
        "organization_id": manifest.organization_id,
        "plant_id": manifest.plant_id,
        "generated_at": manifest.document.get("generated_at"),
    }
    for field_name, value in expected.items():
        if payload[field_name] != value:
            raise _fail("manifest_signature_mismatch", f"{field_name} no coincide", **where)
    key = keys.get(key_id)
    if key is None:
        raise _fail("unknown_key", f"key_id {key_id!r}", **where)
    generated_at = payload["generated_at"]
    if not key["valid_from"] <= generated_at < key["valid_until"]:
        raise _fail(
            "key_not_valid_at_signing",
            f"key_id {key_id!r} vigente de {key['valid_from']} a {key['valid_until']},"
            f" manifiesto de {generated_at}",
            **where,
        )
    signature = _canonical_base64(document["signature"], _SIGNATURE_FORM)
    if signature is None or len(signature) != 64:
        raise _fail("manifest_signature_invalid", "firma en base64 no canónico", **where)
    try:
        message = canonicalize(payload)
    except CanonicalizationError:
        raise _fail("manifest_signature_invalid", "sin forma canónica", **where) from None
    if not ed25519_verify(key["raw"], message, signature):
        raise _fail("bad_signature", f"key_id {key_id!r}", **where)
    report.anchored_by.append("manifest_signature")
    # Qué clave ancló el paquete: el tercero la compara con /.well-known/vigia-checkpoint-keys
    # (una clave añadida al conjunto incluido firmaría igual de bien).
    report.signing_key = {
        "key_id": key_id,
        "public_key_sha256": hashlib.sha256(key["raw"]).hexdigest(),
    }


# --- Contenido ----------------------------------------------------------------------------------


class _Content:
    """Las comprobaciones de contenido sobre los artefactos ya hasheados."""

    def __init__(
        self,
        package: _Package,
        manifest: _Manifest,
        documents: dict[str, Any],
        report: ExportReport,
    ) -> None:
        self.package = package
        self.manifest = manifest
        self.documents = documents
        self.report = report
        self.by_kind: dict[str, list[_Artifact]] = {}
        for artifact in manifest.artifacts:
            self.by_kind.setdefault(artifact.kind, []).append(artifact)
        self.record_ids: dict[str, Mapping[str, Any]] = {}
        """Cada registro de los hallazgos por su ``record_id`` (con o sin contenido)."""
        self.evidence_attachments: dict[str, str] = {}
        """Los adjuntos que citan las entradas de evidencia con contenido: identificador → hash."""
        self.lost: dict[str, Mapping[str, Any]] = {}
        """Las declaraciones ``closure_attachment_unrecoverable`` con contenido, por adjunto."""

    def _where(self, artifact: _Artifact, pointer: str) -> dict[str, Any]:
        entry = self.package.entries[artifact.path]
        return {
            "volume": entry.volume,
            "artifact": artifact.path,
            "offset": entry.offset,
            "pointer": pointer,
        }

    def _mismatch(self, detail: str, pointer: str) -> ExportBroken:
        entry = self.package.entries[EXPORT_MANIFEST_NAME]
        return _fail(
            "manifest_mismatch",
            detail,
            volume=entry.volume,
            artifact=EXPORT_MANIFEST_NAME,
            offset=entry.offset,
            pointer=pointer,
        )

    def _single(self, kind: str) -> _Artifact:
        found = self.by_kind.get(kind, [])
        if len(found) != 1 or found[0].path not in self.documents:
            raise self._mismatch(f"se espera un artefacto «{kind}»", "/artifacts")
        return found[0]

    def _invalid(self, artifact: _Artifact, pointer: str, detail: str = "") -> ExportBroken:
        return _fail("artifact_invalid", detail, **self._where(artifact, pointer))

    def run(self) -> None:
        keys = self.public_keys()
        checkpoints = self.checkpoints(keys)
        self.audit(checkpoints)
        self.findings(checkpoints)
        self.declarations()

    # claves y puntos de control

    def public_keys(self) -> dict[str, dict[str, Any]]:
        artifact = self._single("public_key")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"keys"}:
            raise self._invalid(artifact, "", "se espera {keys}")
        items = document["keys"]
        if items != self.manifest.document["checkpoint_public_keys"]:
            raise self._mismatch(
                "las claves públicas difieren de su archivo", "/checkpoint_public_keys"
            )
        return _parse_key_items(items, lambda pointer: self._where(artifact, pointer))

    def checkpoints(self, keys: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
        artifact = self._single("checkpoint")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"checkpoints"}:
            raise self._invalid(artifact, "", "se espera {checkpoints}")
        items = document["checkpoints"]
        if items != self.manifest.document["checkpoints"]:
            raise self._mismatch("los puntos de control difieren de su archivo", "/checkpoints")
        if not isinstance(items, list):
            raise self._invalid(artifact, "/checkpoints")
        organization_id, plant_id = self.manifest.organization_id, self.manifest.plant_id
        chains: set[tuple[str, object]] = set()
        verified: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            pointer = f"/checkpoints/{index}"
            where = self._where(artifact, pointer)
            if not isinstance(item, dict) or item.keys() != _CHECKPOINT_ENTRY_KEYS:
                raise _fail("checkpoint_invalid", "claves inesperadas", **where)
            kind, chain_plant, content = item["kind"], item["plant_id"], item["content"]
            sequence = item["sequence"]
            if (
                kind not in ("ledger", "audit")
                or (kind == "audit" and chain_plant is not None)
                or (chain_plant is not None and chain_plant != plant_id)
                or (kind, chain_plant) in chains
                or not _matches(_UUID_FORM, item["entry_id"])
                or not _matches(_SHA256_FORM, item["entry_hash"])
                or not _is_count(sequence)
                or sequence < 2
                or not isinstance(content, dict)
                or content.keys() != CHECKPOINT_CONTENT_KEYS
            ):
                raise _fail("checkpoint_invalid", **where)
            chains.add((kind, chain_plant))
            covered_sequence, covered_hash = content["covered_sequence"], content["covered_hash"]
            taken_at, key_id = content["taken_at"], content["key_id"]
            if (
                not _is_count(covered_sequence)
                or covered_sequence != sequence - 1
                or not _matches(_SHA256_FORM, covered_hash)
                or not _matches(_STAMP_FORM, taken_at)
                or not isinstance(key_id, str)
            ):
                raise _fail("checkpoint_invalid", "cobertura o marca no válidas", **where)
            signature = _canonical_base64(content["signature"], _SIGNATURE_FORM)
            if signature is None or len(signature) != 64:
                raise _fail("checkpoint_invalid", "firma en base64 no canónico", **where)
            key = keys.get(key_id)
            if key is None:
                raise _fail("unknown_key", f"key_id {key_id!r}", **where)
            if not key["valid_from"] <= taken_at < key["valid_until"]:
                raise _fail(
                    "key_not_valid_at_signing",
                    f"key_id {key_id!r} vigente de {key['valid_from']} a {key['valid_until']},"
                    f" punto de control de {taken_at}",
                    **where,
                )
            try:
                message = checkpoint_message(
                    kind, organization_id, chain_plant, covered_sequence, covered_hash, taken_at
                )
            except CanonicalizationError:
                raise _fail("checkpoint_invalid", "sin forma canónica", **where) from None
            if not ed25519_verify(key["raw"], message, signature):
                raise _fail("bad_signature", f"key_id {key_id!r}", **where)
            verified.append({**item, "pointer": pointer})
            self.report.checkpoints_verified += 1
        return verified

    # cadenas

    def _anchor(
        self,
        links: dict[int, tuple[str, str, _Artifact, str]],
        checkpoints: list[dict[str, Any]],
        kind: str,
        chain_plant: str | None,
        hash_name: str,
    ) -> None:
        """Enlaces entre consecutivos y con lo que cubre el punto de control firmado."""
        for sequence in sorted(links):
            _, previous_hash, artifact, pointer = links[sequence]
            before = links.get(sequence - 1)
            if before is not None and previous_hash != before[0]:
                raise _fail(
                    "previous_hash_mismatch",
                    f"secuencia {sequence}",
                    **self._where(artifact, pointer + "/previous_hash"),
                )
        for checkpoint in checkpoints:
            if checkpoint["kind"] != kind or checkpoint["plant_id"] != chain_plant:
                continue
            content = checkpoint["content"]
            covered = links.get(content["covered_sequence"])
            if covered is not None and covered[0] != content["covered_hash"]:
                raise _fail(
                    "checkpoint_mismatch",
                    f"secuencia {content['covered_sequence']}",
                    **self._where(covered[2], covered[3] + "/" + hash_name),
                )
            own = links.get(checkpoint["sequence"])
            if own is not None and (
                own[0] != checkpoint["entry_hash"] or own[1] != content["covered_hash"]
            ):
                raise _fail(
                    "checkpoint_mismatch",
                    f"secuencia {checkpoint['sequence']}",
                    **self._where(own[2], own[3] + "/" + hash_name),
                )

    def audit(self, checkpoints: list[dict[str, Any]]) -> None:
        artifact = self._single("audit")
        document = self.documents[artifact.path]
        if not isinstance(document, dict) or document.keys() != {"entries"}:
            raise self._invalid(artifact, "", "se espera {entries}")
        entries = document["entries"]
        if not isinstance(entries, list):
            raise self._invalid(artifact, "/entries")
        links: dict[int, tuple[str, str, _Artifact, str]] = {}
        for index, entry in enumerate(entries):
            pointer = f"/entries/{index}"
            if not _audit_shape(entry):
                raise self._invalid(artifact, pointer, "entrada de auditoría")
            filters = entry["filters"]
            try:
                filters_hash = (
                    None if filters is None else hashlib.sha256(canonicalize(filters)).hexdigest()
                )
                envelope = canonicalize(_audit_envelope_export(entry, self.manifest, filters_hash))
            except CanonicalizationError:
                raise _fail("content_not_canonical", **self._where(artifact, pointer)) from None
            computed = hashlib.sha256(envelope + entry["previous_hash"].encode("ascii")).hexdigest()
            if computed != entry["entry_hash"]:
                raise _fail("entry_hash_mismatch", **self._where(artifact, pointer + "/entry_hash"))
            sequence = entry["chain_sequence"]
            known = links.get(sequence)
            if known is not None and known[0] != entry["entry_hash"]:
                raise _fail("record_conflict", **self._where(artifact, pointer))
            links[sequence] = (entry["entry_hash"], entry["previous_hash"], artifact, pointer)
            self.report.audit_entries_checked += 1
        self._anchor(links, checkpoints, "audit", None, "entry_hash")

    def findings(self, checkpoints: list[dict[str, Any]]) -> None:
        zones = self.manifest.document["zones"]
        zone_ids: list[object] = [zone.get("zone_id") for zone in zones if isinstance(zone, dict)]
        if len(zone_ids) != len(zones) or len(set(map(str, zone_ids))) != len(zone_ids):
            raise self._mismatch("zonas no válidas", "/zones")
        seen_zones: set[str] = set()
        links: dict[int, tuple[str, str, _Artifact, str]] = {}
        records: dict[str, tuple[str, str]] = {}
        states: dict[str, int] = {}
        flagged = 0
        for artifact in self.by_kind.get("findings_json", []):
            document = self.documents.get(artifact.path)
            if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
                raise self._invalid(artifact, "", "se espera {zone_id, findings}")
            zone_id = document.get("zone_id")
            if zone_id not in zone_ids or zone_id in seen_zones:
                raise self._mismatch(f"hallazgos de una zona no declarada: {zone_id!r}", "/zones")
            seen_zones.add(str(zone_id))
            for index, finding in enumerate(document["findings"]):
                pointer = f"/findings/{index}"
                if (
                    not isinstance(finding, dict)
                    or finding.get("zone_id") != zone_id
                    or not isinstance(finding.get("lifecycle_state"), str)
                    or not isinstance(finding.get("evidence_flagged"), bool)
                    or not isinstance(finding.get("records"), list)
                ):
                    raise self._invalid(artifact, pointer, "hallazgo")
                state = finding["lifecycle_state"]
                states[state] = states.get(state, 0) + 1
                flagged += finding["evidence_flagged"]
                for position, record in enumerate(finding["records"]):
                    self._record(artifact, f"{pointer}/records/{position}", record, links, records)
        if seen_zones != set(map(str, zone_ids)):
            raise self._mismatch("falta el archivo de hallazgos de alguna zona", "/zones")
        self._anchor(links, checkpoints, "ledger", self.manifest.plant_id, "record_hash")
        self._counts(states, flagged)

    def _record(
        self,
        artifact: _Artifact,
        pointer: str,
        record: object,
        links: dict[int, tuple[str, str, _Artifact, str]],
        records: dict[str, tuple[str, str]],
    ) -> None:
        if not (
            isinstance(record, dict)
            and _matches(_UUID_FORM, record.get("record_id"))
            and _is_count(record.get("chain_sequence"))
            and _matches(_SHA256_FORM, record.get("content_hash"))
            and _matches(_SHA256_FORM, record.get("previous_hash"))
            and _matches(_SHA256_FORM, record.get("record_hash"))
        ):
            raise self._invalid(artifact, pointer, "registro")
        if "content" in record:
            try:
                computed = hashlib.sha256(canonicalize(record["content"])).hexdigest()
            except CanonicalizationError:
                raise _fail(
                    "content_not_canonical", **self._where(artifact, pointer + "/content")
                ) from None
            if computed != record["content_hash"]:
                raise _fail(
                    "content_hash_mismatch", **self._where(artifact, pointer + "/content_hash")
                )
        record_id, sequence = record["record_id"], record["chain_sequence"]
        identity = (record["record_hash"], str(sequence))
        if records.setdefault(record_id, identity) != identity:
            raise _fail("record_conflict", **self._where(artifact, pointer))
        known = links.get(sequence)
        if known is not None and known[0] != record["record_hash"]:
            raise _fail("record_conflict", **self._where(artifact, pointer))
        links[sequence] = (record["record_hash"], record["previous_hash"], artifact, pointer)
        self.report.records_checked += 1
        self._attachment_facts(artifact, pointer, record)

    def _attachment_facts(self, artifact: _Artifact, pointer: str, record: dict[str, Any]) -> None:
        """Lo que el expediente dice de los adjuntos (DR-LAZ-04): qué citan las entradas de
        evidencia y qué declaró no recuperable. Solo de los registros con contenido."""
        self.record_ids[record["record_id"]] = record
        content = record.get("content")
        if not isinstance(content, dict):
            return
        kind = record.get("record_type")
        if kind == "action_evidence_added":
            items = content.get("attachments", [])
            if not isinstance(items, list):
                raise self._invalid(artifact, pointer + "/content/attachments", "adjuntos")
            for index, item in enumerate(items):
                if not (
                    isinstance(item, dict)
                    and _matches(_UUID_FORM, item.get("attachment_id"))
                    and _matches(_SHA256_FORM, item.get("sha256"))
                ):
                    raise self._invalid(
                        artifact, f"{pointer}/content/attachments/{index}", "adjunto"
                    )
                self.evidence_attachments[item["attachment_id"]] = item["sha256"]
        elif kind == UNRECOVERABLE_RECORD_TYPE:
            if not (
                _matches(_UUID_FORM, content.get("attachment_id"))
                and _matches(_SHA256_FORM, content.get("sha256"))
                and _matches(_STAMP_FORM, content.get("detected_at"))
            ):
                raise self._invalid(artifact, pointer + "/content", "adjunto no recuperable")
            self.lost[content["attachment_id"]] = {**content, "record_id": record["record_id"]}

    def _counts(self, states: Mapping[str, int], flagged: int) -> None:
        document = self.manifest.document
        declared = document.get("state_counts")
        if not isinstance(declared, dict) or any(not _is_count(v) for v in declared.values()):
            raise self._mismatch("state_counts no es válido", "/state_counts")
        for state in set(declared) | set(states):
            if declared.get(state, 0) != states.get(state, 0):
                raise self._mismatch(f"el estado {state!r} no cuadra", f"/state_counts/{state}")
        total = sum(states.values())
        expected = {
            "finding_count": total,
            "open_finding_count": sum(states.get(state, 0) for state in OPEN_LIFECYCLE_STATES),
            "closed_finding_count": states.get("closed", 0),
            "unclosable_count": states.get("unclosable", 0),
            "discarded_count": states.get("discarded", 0),
            "suspended_count": states.get("suspended_by_reclassification", 0),
            "evidence_flagged_count": flagged,
        }
        for key, value in expected.items():
            if document.get(key) != value:
                raise self._mismatch(f"{key} no cuadra con los hallazgos", f"/{key}")

    # declaraciones del manifiesto frente a los artefactos

    def declarations(self) -> None:
        document = self.manifest.document
        artifacts = {artifact.path: artifact for artifact in self.manifest.artifacts}
        form = document.get("package_form")
        if form not in ("with_clips", "without_clips"):
            raise self._mismatch("package_form no válido", "/package_form")
        included: set[str] = set()
        for index, clip in enumerate(document["clips"]):
            pointer = f"/clips/{index}"
            if not isinstance(clip, dict) or clip.get("included") is not (
                clip.get("path") is not None
            ):
                raise self._mismatch("clip mal declarado", pointer)
            if clip["included"] != (form == "with_clips"):
                raise self._mismatch("el clip no corresponde a la forma del paquete", pointer)
            if clip["included"]:
                self._declared(artifacts, clip, "clip", pointer)
                included.add(clip["path"])
        if {a.path for a in self.by_kind.get("clip", [])} != included:
            raise self._mismatch("un clip del paquete no está declarado", "/clips")
        self.attachments(artifacts)
        absent: set[object] = set()
        for index, item in enumerate(document["absent_entries"]):
            if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                raise self._mismatch("entrada ausente mal declarada", f"/absent_entries/{index}")
            absent.add(item["kind"])
        for kind in ("document", "verifier"):
            present = len(self.by_kind.get(kind, []))
            if (kind in absent and present) or (kind not in absent and present != 1):
                raise self._mismatch(f"«{kind}» presente y ausente a la vez", "/absent_entries")
        verifiers = self.by_kind.get("verifier", [])
        absent_version = self.manifest.verifier_version == EXPORT_VERIFIER_ABSENT
        if absent_version != ("verifier" in absent) or (
            verifiers and verifiers[0].path != EXPORT_VERIFIER_NAME
        ):
            raise self._mismatch(
                "verifier_version no cuadra con el verificador", "/verifier_version"
            )

    def attachments(self, artifacts: Mapping[str, _Artifact]) -> None:
        """Cada adjunto del manifiesto viaja con sus bytes o consta **no recuperable** con la
        declaración del expediente (DR-LAZ-04, manifiesto 2); y cada adjunto que cita una entrada
        de evidencia está en el manifiesto con su hash: uno ausente sin declarar es un paquete
        roto, nunca silencio."""
        document = self.manifest.document
        version = document["manifest_version"]
        attached: set[str] = set()
        listed: dict[object, Mapping[str, Any]] = {}
        for index, attachment in enumerate(document["attachments"]):
            pointer = f"/attachments/{index}"
            if not isinstance(attachment, dict) or attachment.get("attachment_id") in listed:
                raise self._mismatch("adjunto mal declarado", pointer)
            listed[attachment.get("attachment_id")] = attachment
            # La versión 1 no declaraba disponibilidad: todos sus adjuntos viajaban.
            available = attachment.get("available", True if version == 1 else None)
            if not isinstance(available, bool):
                raise self._mismatch("adjunto sin available", pointer + "/available")
            if available:
                if version != 1 and attachment.get("unrecoverable") is not None:
                    raise self._mismatch("adjunto disponible y no recuperable", pointer)
                self._declared(artifacts, attachment, "attachment", pointer)
                attached.add(attachment["path"])
            else:
                self._unrecoverable(attachment, pointer)
        if {a.path for a in self.by_kind.get("attachment", [])} != attached:
            raise self._mismatch("un adjunto del paquete no está declarado", "/attachments")
        for attachment_id, sha256 in sorted(self.evidence_attachments.items()):
            entry = listed.get(attachment_id)
            if entry is None or entry.get("sha256") != sha256:
                raise self._mismatch(
                    f"el adjunto {attachment_id} no viaja ni consta como no recuperable",
                    "/attachments",
                )
        for attachment_id, declaration in sorted(self.lost.items()):
            entry = listed.get(attachment_id)
            if (
                entry is not None
                and entry.get("sha256") == declaration["sha256"]
                and entry.get("available") is not False
            ):
                raise self._mismatch(
                    f"el adjunto {attachment_id} es no recuperable en el expediente",
                    "/attachments",
                )

    def _unrecoverable(self, attachment: Mapping[str, Any], pointer: str) -> None:
        """Un adjunto sin bytes: su declaración es la del registro del expediente (mismo
        identificador, hash y fecha) y el paquete no lleva nada en su lugar."""
        declaration = attachment.get("unrecoverable")
        if (
            attachment.get("path") is not None
            or not isinstance(declaration, dict)
            or declaration.keys() != _UNRECOVERABLE_KEYS
            or declaration["sha256"] != attachment.get("sha256")
            or not _matches(_SHA256_FORM, declaration["sha256"])
            or not _matches(_UUID_FORM, declaration["record_id"])
            or not _matches(_UUID_FORM, declaration["declaration_id"])
            or not _matches(_STAMP_FORM, declaration["detected_at"])
            or not isinstance(declaration["reason_es"], str)
        ):
            raise self._mismatch("adjunto no recuperable mal declarado", pointer)
        record = self.record_ids.get(declaration["record_id"])
        if record is None or record.get("record_type") != UNRECOVERABLE_RECORD_TYPE:
            raise self._mismatch(
                "adjunto no recuperable sin su registro en el hallazgo", pointer + "/unrecoverable"
            )
        content = self.lost.get(str(attachment.get("attachment_id")))
        if "content" in record and (
            content is None
            or content["record_id"] != declaration["record_id"]
            or content.get("declaration_id") != declaration["declaration_id"]
            or content["sha256"] != declaration["sha256"]
            or content["detected_at"] != declaration["detected_at"]
        ):
            raise self._mismatch(
                "la declaración no es la del registro del hallazgo", pointer + "/unrecoverable"
            )

    def _declared(
        self, artifacts: Mapping[str, _Artifact], item: Mapping[str, Any], kind: str, pointer: str
    ) -> None:
        path = item.get("path")
        artifact = artifacts.get(path) if isinstance(path, str) else None
        if (
            artifact is None
            or artifact.kind != kind
            or artifact.sha256 != item.get("sha256")
            or artifact.size_bytes != item.get("size_bytes")
        ):
            raise self._mismatch(f"{kind} declarado sin su artefacto", pointer)


def _canonical_base64(text: object, pattern: re.Pattern[str]) -> bytes | None:
    if not isinstance(text, str) or pattern.fullmatch(text) is None:
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if base64.b64encode(raw).decode("ascii") == text else None


def _optional_uuid(value: object) -> bool:
    return value is None or _matches(_UUID_FORM, value)


def _audit_shape(entry: object) -> TypeGuard[dict[str, Any]]:
    if not isinstance(entry, dict) or entry.keys() != _AUDIT_EXPORT_KEYS:
        return False
    actor = entry["actor"]
    return (
        isinstance(actor, dict)
        and actor.keys() == _AUDIT_ACTOR_KEYS
        and _matches(_UUID_FORM, entry["entry_id"])
        and _is_count(entry["chain_sequence"])
        and _matches(_SHA256_FORM, entry["previous_hash"])
        and _matches(_SHA256_FORM, entry["entry_hash"])
        and _optional_uuid(entry["scope_plant_id"])
        and _optional_uuid(entry["scope_zone_id"])
        and _optional_uuid(entry["resource_id"])
        and _matches(_STAMP_FORM, entry["occurred_at"])
    )


def _audit_envelope_export(
    entry: Mapping[str, Any], manifest: _Manifest, filters_hash: str | None
) -> dict[str, Any]:
    """El sobre de U-02 de una entrada de auditoría (BR-NUC-60) a partir de su forma exportada."""
    resource_kind, resource_id = entry["resource_kind"], entry["resource_id"]
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "correlation_id": entry["correlation_id"],
        "entry_id": entry["entry_id"],
        "filters_hash": filters_hash,
        "occurred_at": entry["occurred_at"],
        "operation": entry["operation"],
        "organization_id": manifest.organization_id,
        "outcome": entry["outcome"],
        "resource_ref": (
            None
            if resource_kind is None and resource_id is None
            else {"id": resource_id, "kind": resource_kind}
        ),
        "result_count": entry["result_count"],
        "scope": {"plant_id": entry["scope_plant_id"], "zone_id": entry["scope_zone_id"]},
    }


# --- Extracción ---------------------------------------------------------------------------------


def _extract(package: _Package, manifest: _Manifest, destination: Path) -> None:
    """Escribe el paquete ya verificado en ``destination`` (vacío o inexistente)."""
    created = not destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    expected = {artifact.path: artifact.sha256 for artifact in manifest.artifacts}
    names = [EXPORT_MANIFEST_NAME, *expected]
    if EXPORT_SIGNATURE_NAME in package.entries:
        names.insert(1, EXPORT_SIGNATURE_NAME)
    try:
        for name in names:
            entry = package.entries[name]
            where: dict[str, Any] = {
                "volume": entry.volume,
                "artifact": name,
                "offset": entry.offset,
            }
            target = root.joinpath(*name.split("/"))
            hasher = hashlib.sha256()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    for chunk in package.stream(entry):
                        hasher.update(chunk)
                        output.write(chunk)
            except OSError as error:
                # Nunca un traceback: una ruta que choca con otra o un disco lleno es un fallo
                # declarado, y lo escrito se borra.
                raise _fail("extract_failed", str(error), **where) from None
            if name in expected and hasher.hexdigest() != expected[name]:
                raise _fail("extract_failed", "el hash cambió al extraer", **where)
    except BaseException:
        if created:
            shutil.rmtree(root, ignore_errors=True)
        else:
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        raise


# --- Verificación -------------------------------------------------------------------------------


def verify_export(
    path: Path,
    *,
    manifest_sha256: str | None = None,
    extract_to: Path | None = None,
    limits: ExportLimits | None = None,
) -> ExportReport:
    """Verifica el paquete de ``path``; con ``extract_to``, lo extrae solo si está íntegro."""
    report = ExportReport(str(path))
    limits = limits if limits is not None else ExportLimits()
    package: _Package | None = None
    try:
        volumes = find_volumes(path)
        report.volumes = [volume.name for volume in volumes]
        package = _Package(volumes, limits)
        manifest, digest = _read_manifest(package, manifest_sha256)
        report.manifest_sha256 = digest
        report.declared_verifier_version = manifest.verifier_version
        if manifest_sha256 is not None:
            report.anchored_by.append("manifest_sha256")
        verify_manifest_anchor(package, manifest, digest, report)
        documents = verify_artifact_hashes(package, manifest, report)
        _Content(package, manifest, documents, report).run()
        # Sin ancla no hay «íntegro» y no se extrae nada (decisión B1).
        if extract_to is not None and report.anchored_by:
            _extract(package, manifest, extract_to)
            report.extracted_to = str(extract_to)
    except ExportBroken as broken:
        report.failure = broken.failure
    except RecursionError:
        report.failure = ExportFailure(
            "artifact_invalid", ExportPosition(), "anidamiento demasiado profundo"
        )
    finally:
        if package is not None:
            package.close()
    return report


def export_report_document(report: ExportReport) -> dict[str, Any]:
    """El resultado como documento JSON (el archivo de ``--out``)."""
    failure = report.failure
    broken: dict[str, Any] | None = None
    if failure is not None:
        broken = {
            "reason": failure.reason,
            "message": failure.message(),
            "volume": failure.position.volume,
            "artifact": failure.position.artifact,
            "offset": failure.position.offset,
            "pointer": failure.position.pointer,
        }
    return {
        "verifier": {"name": "vigia_verify", "version": EXPORT_VERIFIER_VERSION},
        "package": report.package,
        "status": report.status,
        "volumes": report.volumes,
        "manifest_sha256": report.manifest_sha256,
        "declared_verifier_version": report.declared_verifier_version,
        "artifacts_verified": report.artifacts_verified,
        "checkpoints_verified": report.checkpoints_verified,
        "records_checked": report.records_checked,
        "audit_entries_checked": report.audit_entries_checked,
        "anchored_by": report.anchored_by,
        "signing_key": report.signing_key,
        "broken": broken,
        "extracted_to": report.extracted_to,
    }


def render_export_summary(report: ExportReport) -> str:
    """Resumen legible en español."""
    lines = [
        f"Verificador de paquetes exportados de Vigía ({EXPORT_VERIFIER_VERSION})",
        f"Paquete: {report.package}",
        f"Volúmenes: {', '.join(report.volumes) if report.volumes else 'ninguno'}",
    ]
    if report.manifest_sha256 is not None:
        lines.append(f"SHA-256 del manifiesto: {report.manifest_sha256}")
    if report.declared_verifier_version not in (
        None,
        EXPORT_VERIFIER_ABSENT,
        EXPORT_VERIFIER_VERSION,
    ):
        lines.append(
            f"Aviso: el paquete declara el verificador {report.declared_verifier_version}; "
            f"este es {EXPORT_VERIFIER_VERSION}."
        )
    lines.append(
        f"Artefactos con su hash: {report.artifacts_verified}; puntos de control firmados: "
        f"{report.checkpoints_verified}; registros: {report.records_checked}; entradas de "
        f"auditoría: {report.audit_entries_checked}"
    )
    failure = report.failure
    if failure is not None:
        position = failure.position
        where = ", ".join(
            part
            for part in (
                f"volumen {position.volume}" if position.volume else "",
                f"artefacto {position.artifact}" if position.artifact else "",
                f"desplazamiento {position.offset}" if position.offset is not None else "",
                f"puntero {position.pointer}" if position.pointer else "",
            )
            if part
        )
        lines.append(f"ROTO: {failure.message()}" + (f" [{where}]" if where else ""))
        lines.append("No se extrajo nada.")
    elif not report.anchored_by:
        lines.append(
            "SIN ANCLA: el paquete no trae una firma válida del manifiesto y no se dio "
            "--manifest-sha256. Las comprobaciones solo prueban que el paquete es coherente "
            "consigo mismo, no que sea el que produjo la plataforma: quien rehaga el manifiesto "
            "puede cambiar registros y estados sin que se note. No se extrajo nada."
        )
    else:
        anchors = {
            "manifest_signature": "firma del manifiesto con la clave de los puntos de control",
            "manifest_sha256": "SHA-256 del manifiesto registrado por la plataforma",
        }
        lines.append("Anclado por: " + "; ".join(anchors[item] for item in report.anchored_by))
        if report.signing_key is not None:
            lines.append(
                f"Clave que firmó el manifiesto: {report.signing_key['key_id']} (SHA-256 de la "
                f"clave pública {report.signing_key['public_key_sha256']}): compárela con "
                "la que publica la plataforma en /.well-known/vigia-checkpoint-keys."
            )
        if report.extracted_to is not None:
            lines.append(f"Extraído en: {report.extracted_to}")
    results = {"intact": "ÍNTEGRO", "broken": "ROTO", "unanchored": "SIN ANCLA"}
    lines.append(f"Resultado: {results[report.status]}")
    return "\n".join(lines) + "\n"


def _export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigia_verify.py",
        description=(
            "Verifica, sin red y sin acceso a la plataforma, un paquete exportado de Vigía: "
            "rechaza rutas y entradas peligrosas, comprueba el hash de cada artefacto antes de "
            "extraer nada, las firmas de los puntos de control y las cadenas."
        ),
        epilog=(
            "Código de salida: 0 si el paquete está íntegro y anclado; 1 si está roto o no se "
            "puede leer; 2 si la orden es incorrecta; 3 si es coherente pero sin ancla (ni firma "
            "del manifiesto ni --manifest-sha256)."
        ),
        formatter_class=_Formatter,
        add_help=False,
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument(
        "package",
        metavar="paquete",
        type=Path,
        help="directorio con vol-01.zip, vol-02.zip… o la ruta de vol-01.zip",
    )
    parser.add_argument(
        "--manifest-sha256",
        dest="manifest_sha256",
        metavar="HEX",
        help="SHA-256 del manifiesto registrado por la plataforma (export_produced)",
    )
    parser.add_argument(
        "--extract",
        metavar="DIRECTORIO",
        type=Path,
        help="extrae el paquete en este directorio vacío, solo si está íntegro",
    )
    parser.add_argument(
        "--out", metavar="resultado.json", type=Path, help="archivo de resultado JSON"
    )
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y termina")
    parser.add_argument(
        "--version",
        action="version",
        version=f"vigia_verify {EXPORT_VERIFIER_VERSION}",
        help="muestra la versión y termina",
    )
    return parser


def _usage(message: str) -> int:
    sys.stderr.write(f"vigia_verify.py: error: {message}\n")
    return EXIT_USAGE


def export_main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: 0 íntegro y anclado, 1 roto, 2 orden incorrecta, 3 sin ancla."""
    arguments = _export_parser().parse_args(argv)
    package: Path = arguments.package
    if not package.exists() and not package.is_symlink():
        return _usage(f"no existe {str(package)!r}")
    expected = arguments.manifest_sha256
    if expected is not None and _SHA256_FORM.fullmatch(expected) is None:
        return _usage("--manifest-sha256 debe ser hexadecimal en minúsculas de 64 caracteres")
    destination: Path | None = arguments.extract
    if destination is not None and (
        destination.is_symlink()
        or (destination.exists() and (not destination.is_dir() or any(destination.iterdir())))
    ):
        return _usage("--extract debe ser un directorio vacío o inexistente")
    report = verify_export(package, manifest_sha256=expected, extract_to=destination)
    sys.stdout.write(render_export_summary(report))
    if arguments.out is not None:
        text = json.dumps(export_report_document(report), ensure_ascii=False, indent=2) + "\n"
        try:
            arguments.out.write_text(text, encoding="utf-8")
        except OSError as error:
            return _usage(f"no se pudo escribir {str(arguments.out)!r}: {error}")
    codes = {"intact": EXIT_INTACT, "broken": EXIT_BROKEN, "unanchored": EXIT_UNANCHORED}
    return codes[report.status]


if __name__ == "__main__":
    raise SystemExit(export_main())
