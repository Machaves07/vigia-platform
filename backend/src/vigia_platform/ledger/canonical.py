"""Canonicalización del expediente: una sola vez por registro (LC-NUC-09, PAT-NUC-REN-01).

El escritor canonicaliza el contenido validado **una vez** con la biblioteca heredada de U-01
(``vigia_contracts.canonical``, RFC 8785) y persiste esos bytes en ``content``; ``content_json``
se deriva de ellos en la base. El verificador y el paso 2 del motor usan el sobre canónico en
Python para recalcular ``record_hash`` sin la base.

- ``canonical_bytes(document)``: los bytes RFC 8785 de un documento JSON o de un modelo Pydantic
  (en su forma de transporte, como U-01, PR-NUC-17). Si el documento supera
  ``LARGE_DOCUMENT_BYTES`` (16 KB ``[objetivo propio]``) la serialización corre en el pool de
  CPU (``shared.cpu_pool``, PAT-NUC-REN-05) y no bloquea el bucle de eventos. El tamaño se acota
  **por arriba** recorriendo el documento sin serializarlo, y el recorrido se detiene en cuanto
  supera el umbral: ningún documento de más de 16 KB se serializa en el bucle.
- ``canonical_bytes_sync(document)``: lo mismo, síncrono, para quien ya corre en un hilo del pool
  (paso 2 del motor, verificación por lotes).
- ``parse(data)``: el documento de unos bytes JSON, leído como lo hace RFC 8785: rechaza lo que
  la canonicalización no puede volver a escribir (``NaN``, infinitos, números fuera del doble,
  claves repetidas, sustitutos sueltos, UTF-8 inválido). Un entero de magnitud mayor que
  ``2**53 - 1`` se lee como doble, igual que lo escribió RFC 8785, para que
  ``canonical(parse(canonical(x))) = canonical(x)`` (PR-NUC-48).
- ``envelope_canonical(record)`` y ``audit_envelope_canonical(entry)``: los bytes del sobre de
  forma fija de un registro (BR-NUC-46) o de una entrada de auditoría (BR-NUC-60), a partir de las
  columnas persistidas; son byte a byte los de ``ledger.vigia_canonical_envelope`` y
  ``shared.vigia_canonical_audit_envelope`` (TASK-108): todas las claves presentes, ``null`` en
  las ausentes, marcas en UTC truncadas a milisegundos con ``Z``.

Todo lo que no se puede canonicalizar o leer termina en ``CanonicalFormError``; nunca en
``RecursionError``, ``OverflowError`` ni otra excepción sin controlar. Módulo sin E/S ni hora del
sistema; no importa FastAPI ni SQLAlchemy.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, JsonValue
from vigia_contracts.canonical import CanonicalizationError, canonicalize

from vigia_platform.shared.cpu_pool import CpuPool, get_cpu_pool

__all__ = [
    "LARGE_DOCUMENT_BYTES",
    "MAX_SAFE_INTEGER",
    "CanonicalFormError",
    "audit_envelope_canonical",
    "canonical_bytes",
    "canonical_bytes_sync",
    "envelope_canonical",
    "parse",
]

LARGE_DOCUMENT_BYTES: Final = 16 * 1024
"""Por encima de este tamaño canónico, la serialización va al pool de CPU ``[objetivo propio]``."""

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""Mayor entero que RFC 8785 escribe sin pérdida (I-JSON, RFC 7493)."""

_FLOAT_BOUND: Final = 25
"""Cota de la longitud de un doble en la notación de ECMAScript (``-1.2345678901234567e-308``)."""

_SURROGATE_ESCAPE: Final = re.compile(r"\\u[dD][89a-fA-F]")
"""Escape de un sustituto UTF-16 en el texto JSON; solo entonces puede haber uno suelto."""


class CanonicalFormError(ValueError):
    """El documento no se puede canonicalizar con RFC 8785 o los bytes no son JSON legible."""


# --- Contenido ------------------------------------------------------------------------------------


def _dump(document: BaseModel | JsonValue) -> JsonValue:
    """Forma de transporte de un modelo (la de U-01); un valor JSON queda igual."""
    if not isinstance(document, BaseModel):
        return document
    try:
        dumped: JsonValue = document.model_dump(mode="json", by_alias=True, exclude_none=True)
    except (RecursionError, ValueError) as error:
        # PydanticSerializationError es un ValueError.
        raise CanonicalFormError(
            f"el modelo no tiene forma de transporte JSON ({type(error).__name__})"
        ) from None
    return dumped


def _string_bound(value: str) -> int:
    """Cota superior de los bytes de ``value`` como cadena JSON canónica, comillas incluidas."""
    if value.isprintable():
        # Sin controles: UTF-8 exacto más un escape por comilla y barra inversa.
        size = len(value.encode("utf-8", "surrogatepass"))
        return size + 2 + value.count('"') + value.count("\\")
    # Con controles u otros no imprimibles: a lo sumo seis bytes por carácter (\u00xx).
    return 6 * len(value) + 2


def _exceeds(document: JsonValue, limit: int) -> bool:
    """``True`` si el tamaño canónico de ``document`` puede superar ``limit`` bytes.

    Recorrido iterativo que suma una cota superior (exacta para texto imprimible, enteros,
    booleanos y ``null``) y se detiene en cuanto la suma pasa de ``limit``: cada nodo aporta al
    menos un byte, así que el trabajo en el bucle está acotado por ``limit``. Lo que no es JSON
    no suma; la serialización lo rechaza después.
    """
    total = 0
    pending: list[object] = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            total += _string_bound(value)
        elif value is None or value is True:
            total += 4
        elif value is False:
            total += 5
        elif isinstance(value, int):
            total += len(str(value)) if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else 1
        elif isinstance(value, float):
            total += _FLOAT_BOUND
        elif isinstance(value, Mapping):
            total += 1 + max(len(value), 1)  # llaves y separadores
            for key, item in value.items():
                total += 1  # dos puntos
                if isinstance(key, str):
                    total += _string_bound(key)
                pending.append(item)
                if total > limit:
                    return True
        elif isinstance(value, list | tuple):
            total += 1 + max(len(value), 1)  # corchetes y comas
            for item in value:
                pending.append(item)
                if total > limit:
                    return True
        if total > limit:
            return True
    return False


def _canonicalize(document: JsonValue) -> bytes:
    try:
        return canonicalize(document)
    except (CanonicalizationError, RecursionError, TypeError, ValueError) as error:
        # Sin el texto del error: puede citar valores del contenido (PR-NUC-54).
        raise CanonicalFormError(
            f"el documento no admite forma canónica RFC 8785 ({type(error).__name__})"
        ) from None


def canonical_bytes_sync(document: BaseModel | JsonValue) -> bytes:
    """Bytes RFC 8785 de ``document`` en el hilo actual (para quien ya está en el pool)."""
    return _canonicalize(_dump(document))


async def canonical_bytes(document: BaseModel | JsonValue, *, pool: CpuPool | None = None) -> bytes:
    """Bytes RFC 8785 de ``document``; en el pool de CPU si supera ``LARGE_DOCUMENT_BYTES``."""
    dumped = _dump(document)
    if not _exceeds(dumped, LARGE_DOCUMENT_BYTES):
        return _canonicalize(dumped)
    return await (pool if pool is not None else get_cpu_pool()).run(_canonicalize, dumped)


# --- Lectura --------------------------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise CanonicalFormError(f"{name} no es un número JSON representable")


def _read_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise CanonicalFormError("número fuera del rango de un doble IEEE 754")
    return value


def _read_int(text: str) -> int | float:
    value = int(text)
    if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        return value
    return _read_float(text)  # RFC 8785 lee todo número como doble


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise CanonicalFormError("el objeto repite una clave")
    return document


def _has_lone_surrogate(document: object) -> bool:
    pending: list[object] = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def parse(data: bytes) -> JsonValue:
    """Documento JSON de ``data`` (UTF-8), con la lectura estricta de RFC 8785."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise CanonicalFormError("se esperaban bytes JSON en UTF-8")
    try:
        text = bytes(data).decode("utf-8")
        document: JsonValue = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_read_float,
            parse_int=_read_int,
            object_pairs_hook=_unique_keys,
        )
    except CanonicalFormError:
        raise
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        # json.JSONDecodeError y el tope de dígitos de int() son ValueError.
        raise CanonicalFormError(
            f"los bytes no son un documento JSON legible ({type(error).__name__})"
        ) from None
    if _SURROGATE_ESCAPE.search(text) and _has_lone_surrogate(document):
        raise CanonicalFormError("el documento contiene un sustituto UTF-16 suelto")
    return document


# --- Sobres ---------------------------------------------------------------------------------------


def _uuid(row: Mapping[str, Any], column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, uuid.UUID):
        raise CanonicalFormError(f"{column} debe ser un UUID")
    return str(value)


def _text(row: Mapping[str, Any], column: str) -> str | None:
    value = row[column]
    if value is None or isinstance(value, str):
        return value
    raise CanonicalFormError(f"{column} debe ser texto")


def _integer(row: Mapping[str, Any], column: str) -> int | None:
    value = row[column]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalFormError(f"{column} debe ser un entero")
    return value


def _timestamp(row: Mapping[str, Any], column: str) -> str | None:
    """Marca en UTC con milisegundos truncados y ``Z``, como ``to_char(..., 'MS')`` en SQL."""
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CanonicalFormError(f"{column} debe ser una marca con zona horaria")
    try:
        moment = value.astimezone(UTC).replace(tzinfo=None)
    except OverflowError:
        raise CanonicalFormError(f"{column} queda fuera del rango de fechas en UTC") from None
    # isoformat rellena el año a cuatro cifras (strftime("%Y") no lo hace en glibc).
    return moment.isoformat(timespec="milliseconds") + "Z"


def _actor(row: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {
        "concession_id": _uuid(row, "actor_concession_id"),
        "display_name_snapshot": _text(row, "actor_display_name_snapshot"),
        "id": _uuid(row, "actor_id"),
        "kind": _text(row, "actor_kind"),
        "role_in_use": _text(row, "actor_role_in_use"),
        "unit": _text(row, "actor_unit"),
    }


def _envelope(
    row: Mapping[str, Any], builder: Callable[[Mapping[str, Any]], dict[str, JsonValue]]
) -> bytes:
    try:
        envelope = builder(row)
    except KeyError as error:
        raise CanonicalFormError(f"falta la columna {error.args[0]!r} del sobre") from None
    return _canonicalize(envelope)


def _record_envelope(record: Mapping[str, Any]) -> dict[str, JsonValue]:
    return {
        "actor": _actor(record),
        "chain_sequence": _integer(record, "chain_sequence"),
        "content_hash": _text(record, "content_hash"),
        "correlation_id": _uuid(record, "correlation_id"),
        "organization_id": _uuid(record, "organization_id"),
        "plant_id": _uuid(record, "plant_id"),
        "received_at": _timestamp(record, "received_at"),
        "record_id": _uuid(record, "record_id"),
        "record_type": _text(record, "record_type"),
        "schema_version": _integer(record, "schema_version"),
        "scope": {
            "node_id": _uuid(record, "scope_node_id"),
            "plant_id": _uuid(record, "scope_plant_id"),
            "zone_id": _uuid(record, "scope_zone_id"),
        },
    }


def _audit_envelope(entry: Mapping[str, Any]) -> dict[str, JsonValue]:
    resource_kind = _text(entry, "resource_kind")
    resource_id = _uuid(entry, "resource_id")
    return {
        "actor": _actor(entry),
        "chain_sequence": _integer(entry, "chain_sequence"),
        "correlation_id": _uuid(entry, "correlation_id"),
        "entry_id": _uuid(entry, "entry_id"),
        "filters_hash": _text(entry, "filters_hash"),
        "occurred_at": _timestamp(entry, "occurred_at"),
        "operation": _text(entry, "operation"),
        "organization_id": _uuid(entry, "organization_id"),
        "outcome": _text(entry, "outcome"),
        "resource_ref": (
            None
            if resource_kind is None and resource_id is None
            else {"id": resource_id, "kind": resource_kind}
        ),
        "result_count": _integer(entry, "result_count"),
        "scope": {
            "plant_id": _uuid(entry, "scope_plant_id"),
            "zone_id": _uuid(entry, "scope_zone_id"),
        },
    }


def envelope_canonical(record: Mapping[str, Any]) -> bytes:
    """Bytes del sobre de un registro (BR-NUC-46), iguales a ``ledger.vigia_canonical_envelope``.

    ``record`` son las columnas de ``ledger.ledger_record`` (una fila de la base o un
    ``Mapping`` con los mismos nombres): UUID como ``uuid.UUID``, marcas con zona horaria.
    """
    return _envelope(record, _record_envelope)


def audit_envelope_canonical(entry: Mapping[str, Any]) -> bytes:
    """Bytes del sobre de una entrada (BR-NUC-60), iguales a ``vigia_canonical_audit_envelope``."""
    return _envelope(entry, _audit_envelope)
