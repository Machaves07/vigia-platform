"""Generadores de contenidos y cargas válidos de U-03 (``record_by_type``, ``mutate_record``).

Los comparten PR-GOB-19, PR-GOB-31 y las pruebas de ejemplo de los tipos y eventos de U-03; las
tareas de dominio los reutilizan para sus propios registros.

- ``schema_strategy``: genera documentos a partir del JSON Schema que el registro persiste (el
  que deriva Pydantic del modelo), con listas cortas por defecto o **al tope** (``at_limit``).
- ``record_by_type(nombre)``: un contenido válido del tipo. Los tres tipos de la ingesta usan los
  generadores del kit de U-01 (``finding``, ``detection_for_review``, ``observability_event``
  con recibo); ``catalog_version_published`` firma un ``zone_catalog`` del kit con una clave de
  prueba. Al resto se le aplican las reglas entre campos que su modelo comprueba (claves
  compuestas, coherencia de la admisión, motivo solo al revocar…).
- ``mutate_record``: cuela en el documento, en cualquier objeto, un campo con nombre de la lista
  prohibida o un nombre cualquiera, con un dato de persona; o sustituye una cadena cerrada por
  un texto libre con nombre de persona.

Solo datos generados: nombres, documentos y correos sintéticos.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from hypothesis import assume
from hypothesis import strategies as st
from vigia_contracts.conformance.generators import (
    detection_for_review,
    finding,
    observability_event,
    sign_envelope,
    test_signing_key,
    zone_catalog,
)

from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import register_catalog_record_types
from vigia_platform.fleet.events import register_fleet_event_types
from vigia_platform.fleet.record_types import enrollment_source_key, register_fleet_record_types
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected, apply_base_policy
from vigia_platform.ledger.registry import CompiledType, RecordTypeRegistry
from vigia_platform.ledger.schema_rules import FORBIDDEN_NAME_TOKENS, field_nodes, is_free_text
from vigia_platform.shared.outbox.registries import CompiledEventType, EventTypeRegistry

__all__ = [
    "IDENTITY_VALUES",
    "PERSON_FIELD_NAMES",
    "Mutation",
    "event_payload",
    "free_text_values",
    "mutate_record",
    "record_by_type",
    "schema_strategy",
    "string_paths",
    "u03_event_registry",
    "u03_registry",
]

JsonObject = dict[str, Any]

_SAFE_TEXT: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZáéíóúñü "
"""Alfabeto de los textos libres generados: pasa la política base (sin control ni marcado)."""

_SHORT_LISTS: Final = 3
_SHORT_TEXT: Final = 40
_UNIQUE_KEYS: Final = ("camera_id", "row_id", "user_id", "step_kind", "standard_id")
"""Campos que identifican un elemento de una lista de objetos: se generan sin repetir."""

IDENTITY_VALUES: Final = (
    "Juan Pérez Gómez",
    "María Fernanda López",
    "CC 1020304050",
    "juan.perez@example.com",
    "+57 300 123 4567",
    "Placa ABC 123",
)
"""Datos sintéticos de una persona observada (nombre, documento, correo, teléfono, placa). Todos
llevan un espacio, una arroba o mayúsculas y minúsculas: una placa sola (``ABC123``) tiene la
forma de un ``Code`` del contrato y ninguna regla de esquema la distingue de un código."""

PERSON_FIELD_NAMES: Final = (
    "person_name",
    "full_name",
    "first_name",
    "last_name",
    "employee_id",
    "worker_name",
    "national_id",
    "document_number",
    "track_id",
    "face_crop",
    "email",
    "phone",
    "plate",
    "nombre_completo",
    "cedula",
)
"""Nombres de campo que identifican a una persona (BR-NUC-51)."""


def u03_registry() -> RecordTypeRegistry:
    """Registro con los 26 tipos de U-03."""
    registry = RecordTypeRegistry()
    register_catalog_record_types(registry)
    register_fleet_record_types(registry)
    return registry


def u03_event_registry() -> EventTypeRegistry:
    """Registro con los 15 eventos de U-03."""
    registry = EventTypeRegistry()
    register_catalog_event_types(registry)
    register_fleet_event_types(registry)
    return registry


# --- generación desde el JSON Schema ----------------------------------------------------------


def _resolve(node: Mapping[str, Any], defs: Mapping[str, Any]) -> Mapping[str, Any]:
    while "$ref" in node:
        name = node["$ref"].removeprefix("#/$defs/")
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        node = {**defs[name], **siblings}
    return node


def _unique_key(item: Mapping[str, Any], defs: Mapping[str, Any]) -> str | None:
    properties = _resolve(item, defs).get("properties", {})
    return next((key for key in _UNIQUE_KEYS if key in properties), None)


def _strategy(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
    path: str,
    *,
    at_limit: bool,
    overrides: Mapping[str, st.SearchStrategy[Any]],
) -> st.SearchStrategy[Any]:
    if path in overrides:
        return overrides[path]
    node = _resolve(node, defs)
    if "anyOf" in node:
        branches = [
            _strategy(branch, defs, path, at_limit=at_limit, overrides=overrides)
            for branch in node["anyOf"]
        ]
        if at_limit:
            # Al tope: la rama que no es ``null`` y, entre las demás, la lista antes que la
            # constante (``affected_row_ids``: la lista llena pesa más que ``all``).
            ordered = sorted(
                zip(node["anyOf"], branches, strict=True),
                key=lambda pair: {"array": 0, "object": 1, "null": 9}.get(
                    _resolve(pair[0], defs).get("type", "string"), 5
                ),
            )
            return ordered[0][1]
        return st.one_of(branches)
    if "const" in node:
        return st.just(node["const"])
    if "enum" in node:
        return st.sampled_from(node["enum"])
    kind = node.get("type")
    if kind == "object":
        properties: Mapping[str, Any] = node.get("properties", {})
        required = set(node.get("required", []))
        children = {
            name: _strategy(child, defs, f"{path}/{name}", at_limit=at_limit, overrides=overrides)
            for name, child in properties.items()
        }
        mandatory = {name: s for name, s in children.items() if name in required or at_limit}
        optional = {name: s for name, s in children.items() if name not in mandatory}
        return st.fixed_dictionaries(mandatory, optional=optional)
    if kind == "array":
        item = node["items"]
        items = _strategy(item, defs, f"{path}[*]", at_limit=at_limit, overrides=overrides)
        low = int(node.get("minItems", 0))
        high = (
            int(node["maxItems"])
            if at_limit
            else min(int(node["maxItems"]), max(low, _SHORT_LISTS))
        )
        if at_limit:
            low = high
        resolved = _resolve(item, defs)
        if resolved.get("type") == "string" or "enum" in resolved:
            return st.lists(items, min_size=low, max_size=high, unique=True)
        key = _unique_key(item, defs)
        if key is not None:
            return st.lists(items, min_size=low, max_size=high, unique_by=lambda x: x[key])
        return st.lists(items, min_size=low, max_size=high)
    if kind == "string":
        low = int(node.get("minLength", 0))
        high = int(node["maxLength"])
        pattern = node.get("pattern")
        if isinstance(pattern, str):
            return st.from_regex(pattern, fullmatch=True).filter(lambda s: low <= len(s) <= high)
        if at_limit:
            return st.text(alphabet="abc", min_size=high, max_size=high)
        return st.text(alphabet=_SAFE_TEXT, min_size=low, max_size=max(low, min(high, _SHORT_TEXT)))
    if kind == "integer":
        low_int = node.get("minimum", node.get("exclusiveMinimum", -(2**31)))
        high_int = node.get("maximum", node.get("exclusiveMaximum", 2**31))
        if at_limit:
            return st.just(high_int)
        return st.integers(min_value=low_int, max_value=high_int)
    if kind == "number":
        return st.floats(
            min_value=node.get("minimum"),
            max_value=node.get("maximum"),
            allow_nan=False,
            allow_infinity=False,
        )
    if kind == "boolean":
        return st.booleans()
    if kind == "null":
        return st.none()
    raise AssertionError(f"{path}: esquema sin generador ({node})")


def schema_strategy(
    schema: Mapping[str, Any],
    *,
    at_limit: bool = False,
    overrides: Mapping[str, st.SearchStrategy[Any]] | None = None,
) -> st.SearchStrategy[Any]:
    """Documentos que cumplen ``schema`` (sin las reglas entre campos de los modelos)."""
    return _strategy(
        schema, schema.get("$defs", {}), "", at_limit=at_limit, overrides=overrides or {}
    )


# --- contenidos válidos por tipo ----------------------------------------------------------------

_TIMESTAMP: Final = st.from_regex(
    r"^20[2-9][0-9]-(0[1-9]|1[0-2])-(0[1-9]|1[0-9]|2[0-8])T([01][0-9]|2[0-3]):[0-5][0-9]:"
    r"[0-5][0-9]\.[0-9]{3}Z$",
    fullmatch=True,
)
_UUID: Final = st.uuids(version=4).map(str)
_REASON: Final = st.text(alphabet=_SAFE_TEXT, min_size=10, max_size=60)


_CATALOG_TEXTS: Final = (
    ("standards", ("title_es",)),
    ("standards", ("declared_text",)),
    ("standards", ("declared_by", "display_name")),
    ("signals", ("description_es",)),
)
_POLICY_PROBE: Final = FreeTextField(
    record_type="probe", path="/text", min_length=0, max_length=8192
)


def _policy_clean(text: str, fallback: str) -> str:
    """El texto si pasa la política base de U-02; si no, ``fallback``.

    El kit de U-01 genera textos declarados con selectores de variación (``⚠️``, U+FE0F) que la
    política base de la plataforma rechaza como invisibles: la plataforma nunca compone un
    catálogo con ellos, porque cada texto pasó la política al declararse.
    """
    try:
        apply_base_policy(text, _POLICY_PROBE)
    except FreeTextRejected:
        return fallback
    return text


@st.composite
def _signed_catalog(draw: st.DrawFn) -> JsonObject:
    catalog = draw(zone_catalog())
    for collection, path in _CATALOG_TEXTS:
        for item in catalog.get(collection, []):
            parent = _get(item, path[:-1])
            if path[-1] in parent:
                parent[path[-1]] = _policy_clean(parent[path[-1]], "Texto declarado de prueba")
    key = draw(test_signing_key("catalog"))
    return sign_envelope(catalog, key, draw(_TIMESTAMP))


def _versioned(document: JsonObject, identifier: str, version: str) -> None:
    document["source_key"] = f"{document[identifier]}:{document[version]}"


def _fix_admission(document: JsonObject, draw: st.DrawFn) -> None:
    answers = document["answers"]
    failed = [name for name in ("standard", "remedy", "subject") if not answers[name]]
    document["result"] = "rejected" if failed else "admitted"
    document.pop("failed_criterion", None)
    if failed:
        document["failed_criterion"] = draw(st.sampled_from(failed))


def _fix_gate(document: JsonObject, draw: st.DrawFn) -> None:
    document.pop("reason_es", None)
    if document["status"] == "revoked":
        document["reason_es"] = draw(_REASON)


def _fix_agreement(document: JsonObject, draw: st.DrawFn) -> None:
    signatories = document["signatories"]
    signatories[0]["role"] = "copasst"
    document["confirmations"] = [
        {
            "user_id": signatory["user_id"],
            "role_in_use": signatory["role"],
            "confirmed_at": draw(_TIMESTAMP),
            "origin": draw(st.sampled_from(["management", "transparency"])),
        }
        for signatory in signatories
    ]
    assume(document.get("replaces_agreement_id") != document["agreement_id"])


def _fix_walk_test(document: JsonObject, draw: st.DrawFn) -> None:
    steps = document["steps_summary"]
    for step in steps:
        step["duration_ms"] = min(step["duration_ms"], 10 * 24 * 3600 * 1000)
    document["total_duration_ms"] = sum(step["duration_ms"] for step in steps)
    rows = document["matrix_results"]
    for row in rows:
        row["missed"] = min(row["missed"], 1_000_000 // len(rows))
    document["false_negatives_total"] = sum(row["missed"] for row in rows)
    latency = document["latency"]
    if latency.get("not_measured"):
        # v2 (TASK-216): not_measured nombra, en orden, los tramos que faltan.
        latency["not_measured"] = [
            name
            for name in ("node_tranche", "platform_tranche", "exposure_tranche", "served_tranche")
            if latency.get(name) is None
        ]


def _fix_occlusion(document: JsonObject, draw: st.DrawFn) -> None:
    document.pop("declared_reason_es", None)
    if document["verification"] == "declared":
        document["declared_reason_es"] = draw(_REASON)
    if document["verification"] == "verified" and not document["correlated_event_ids"]:
        document["correlated_event_ids"] = [draw(_UUID)]


def _fix_regression(document: JsonObject, draw: st.DrawFn) -> None:
    cause = document["cause"]
    if cause == "catalog_change":
        document.setdefault("catalog_version", draw(st.integers(1, 2**31 - 1)))
        if document["catalog_version"] is None:
            document["catalog_version"] = 1
    if cause == "model_version_change" and document.get("model_version") is None:
        document["model_version"] = "detector-v2"
    if cause == "framing_recaptured":
        document["affected_row_ids"] = "all"
    else:
        # La cámara, la captura y el motivo solo valen con framing_recaptured (VIG-148): el
        # esquema aún no expresa esa regla (VIG-176), así que el generador no los produce aquí.
        for key in ("camera_id", "captured_at", "reason_es"):
            document.pop(key, None)


def _fix_window(document: JsonObject, draw: st.DrawFn) -> None:
    window = document["maintenance_window"]
    first, second = sorted((window["starts_at"], window["ends_at"]))
    assume(first != second)
    window["starts_at"], window["ends_at"] = first, second


def _fix_enrolled(document: JsonObject, draw: st.DrawFn) -> None:
    document["source_key"] = enrollment_source_key(document["node_id"], document["credential_id"])


def _fix_rotated(document: JsonObject, draw: st.DrawFn) -> None:
    assume(document["rotated_from"] != document["credential_id"])


_FIXES: Final[Mapping[str, Callable[[JsonObject, st.DrawFn], None]]] = {
    "catalog_version_published": lambda d, _: _versioned(d, "zone_id", "catalog_version"),
    "catalog_standard_retired": lambda d, _: _versioned(d, "standard_id", "version"),
    "single_occupancy_declared": lambda d, _: _versioned(d, "zone_id", "catalog_version"),
    "standard_admission_test": _fix_admission,
    "gate_state_changed": _fix_gate,
    "use_agreement_signed": _fix_agreement,
    "walk_test_result": _fix_walk_test,
    "occlusion_test_result": _fix_occlusion,
    "walk_test_regression_marked": _fix_regression,
    "node_target_version_published": _fix_window,
    "node_enrolled": _fix_enrolled,
    "node_credential_rotated": _fix_rotated,
}
"""Reglas entre campos que los modelos comprueban y el esquema no expresa."""

_CONTRACT_RECORDS: Final[Mapping[str, Callable[[JsonObject], st.SearchStrategy[JsonObject]]]] = {
    "finding_received": lambda catalog: finding(catalog, accepted=True),
    "detection_for_review_received": lambda catalog: detection_for_review(catalog, accepted=True),
    "observability_event_received": lambda catalog: observability_event(catalog, accepted=True),
}
"""Los tres tipos de la ingesta: registros del contrato con recibo, del kit de U-01."""


@st.composite
def record_by_type(draw: st.DrawFn, compiled: CompiledType) -> JsonObject:
    """Un contenido válido del tipo ``compiled`` (``record_by_type`` de PR-GOB-19)."""
    name = compiled.record_type
    if name in _CONTRACT_RECORDS:
        catalog = draw(zone_catalog())
        document: JsonObject = draw(_CONTRACT_RECORDS[name](catalog))
        return document
    overrides: dict[str, st.SearchStrategy[Any]] = {}
    if name == "catalog_version_published":
        overrides["/envelope"] = _signed_catalog()
    document = draw(schema_strategy(compiled.content_schema, overrides=overrides))
    if name == "catalog_version_published":
        document["zone_id"] = document["envelope"]["payload"]["zone_id"]
        document["catalog_version"] = document["envelope"]["payload"]["version"]
    fix = _FIXES.get(name)
    if fix is not None:
        fix(document, draw)
    return document


def event_payload(compiled: CompiledEventType, *, at_limit: bool = False) -> st.SearchStrategy[Any]:
    """Cargas válidas del evento; con ``at_limit``, todas las listas y opcionales al tope."""
    return schema_strategy(compiled.payload_schema, at_limit=at_limit)


# --- rutas y mutaciones ---------------------------------------------------------------------


def string_paths(schema: Mapping[str, Any]) -> list[str]:
    """Rutas de las cadenas del esquema (sin las de valor constante)."""
    nodes, _ = field_nodes(schema)
    return sorted(
        {
            node.path
            for node in nodes
            if node.schema.get("type") == "string" and "const" not in node.schema
        }
    )


def free_text_values() -> st.SearchStrategy[str]:
    """Textos libres que nombran a una persona: nunca caben en una cadena cerrada."""
    return st.sampled_from(IDENTITY_VALUES)


def _object_paths(document: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(document, dict):
        yield path
        for key, value in document.items():
            yield from _object_paths(value, (*path, key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _object_paths(value, (*path, index))


def _string_leaves(document: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(document, str):
        yield path
    elif isinstance(document, dict):
        for key, value in document.items():
            yield from _string_leaves(value, (*path, key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _string_leaves(value, (*path, index))


def content_path(pointer: Sequence[Any]) -> str:
    """Ruta declarada (``/cameras[*]/clips[*]/storage_key``) de un puntero concreto."""
    text = ""
    for part in pointer:
        text += "[*]" if isinstance(part, int) else f"/{part}"
    return text


def _get(document: Any, pointer: Sequence[Any]) -> Any:
    for part in pointer:
        document = document[part]
    return document


@dataclass(frozen=True)
class Mutation:
    """Un documento mutado y la ruta concreta que se tocó."""

    document: JsonObject
    pointer: tuple[Any, ...]
    kind: str


_FIELD_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{2,20}$")


@st.composite
def mutate_record(
    draw: st.DrawFn, document: JsonObject, free_text_paths: Sequence[str]
) -> Mutation:
    """Cuela un dato de persona en el documento (``mutate_record`` de PR-GOB-19).

    - ``extra_field``: un campo nuevo en cualquier objeto, con nombre de persona o cualquiera;
    - ``closed_string``: una cadena que **no** es texto libre declarado pasa a un nombre propio.
    """
    mutated: JsonObject = json.loads(json.dumps(document))
    leaves = [
        pointer
        for pointer in _string_leaves(mutated)
        if content_path(pointer) not in set(free_text_paths)
    ]
    kind = draw(st.sampled_from(["extra_field", "closed_string"] if leaves else ["extra_field"]))
    value = draw(free_text_values())
    if kind == "extra_field":
        target = draw(st.sampled_from(list(_object_paths(mutated))))
        name = draw(
            st.one_of(
                st.sampled_from(PERSON_FIELD_NAMES),
                st.sampled_from(sorted(FORBIDDEN_NAME_TOKENS)),
                st.from_regex(_FIELD_NAME, fullmatch=True),
            )
        )
        container = _get(mutated, target)
        assume(name not in container)
        container[name] = value
        return Mutation(mutated, (*target, name), kind)
    pointer = draw(st.sampled_from(leaves))
    parent = _get(mutated, pointer[:-1])
    assume(parent[pointer[-1]] != value)
    parent[pointer[-1]] = value
    return Mutation(mutated, pointer, kind)


def free_text_nodes(schema: Mapping[str, Any]) -> set[str]:
    """Rutas del esquema que el registro cuenta como texto libre."""
    nodes, _ = field_nodes(schema)
    return {node.path for node in nodes if is_free_text(node.schema)}


def fresh_uuid() -> str:
    return str(uuid.uuid4())
