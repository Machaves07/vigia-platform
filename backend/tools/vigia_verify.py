#!/usr/bin/env python3
# ARCHIVO GENERADO por tools/build_verifier.py: no se edita a mano.
# Fuente: vigia_platform.ledger.chain (pure_rfc8785, pure_ed25519, chain_walk, package_verifier).
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
import sys
import zipfile
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from json.encoder import encode_basestring
from pathlib import Path
from typing import Any, Final, IO


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


if __name__ == "__main__":
    raise SystemExit(main())
