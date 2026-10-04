"""Validación del predicado de un estándar contra la gramática cerrada del contrato (§3.2.1).

Un ``Predicate`` v1 es ``{all_of: [Condition, ...], min_duration_ms}``: una **conjunción** de
condiciones sostenida al menos ``min_duration_ms``, con una plantilla por familia
(``coexistence``, ``guard_bypass``, ``dwell``, ``startup_transition``). La gramática no puede
expresar ausencia (P2), negación, disyunción, conteos ni magnitudes de productividad (P7).

``validate_predicate`` decide con el **lector estricto del contrato** (``DeclaredStandard`` de
``vigia-contracts``, que empareja cada familia con su plantilla): la plataforma nunca redefine la
gramática. Antes, una guarda explícita rechaza lo que el diseño nombra como imposible aunque el
esquema cambiara (``presence: false``, negación, disyunción y rol ``auxiliary``; business-rules
§11), y cualquier entrada que no sea JSON acotado (anidamiento profundo, números no finitos o
enormes, tipos de Python) también es ``PredicateInvalid``: nunca sale otra excepción.

``canonical_conditions`` da las condiciones en un orden canónico: la matriz del walk-test las usa
para el ``row_id`` determinista (``matrix.py``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Final

from pydantic import ValidationError
from vigia_contracts.models.declared_standard import DeclaredStandard
from vigia_contracts.models.enumerations import PredicateFamily

__all__ = [
    "MAX_PREDICATE_DEPTH",
    "PredicateInvalid",
    "canonical_conditions",
    "validate_predicate",
]

MAX_PREDICATE_DEPTH: Final = 8
"""Anidamiento máximo que se copia: la gramática tiene tres niveles, el resto es hostil."""
_MAX_INT: Final = 2**53 - 1
_MAX_STRING: Final = 256
_MAX_ITEMS: Final = 64
_FORBIDDEN_KEYS: Final = frozenset({"not", "none_of", "any_of", "one_of", "or", "negate"})
"""Operadores que la gramática cerrada no tiene: negación y disyunción (P2, business-rules §11)."""
_PROBE: Final[Mapping[str, Any]] = {
    "standard_id": "00000000-0000-4000-8000-000000000000",
    "version": 1,
    "title_es": "sonda",
    "declared_text": "sonda",
    "declared_by": {
        "user_id": "00000000-0000-4000-8000-000000000000",
        "display_name": "sonda",
        "role": "administrator",
    },
    "effective_from": "2026-01-01T00:00:00.000Z",
    "tier_policy": "tier_1_when_signal_valid",
}
"""Estándar sintético que lleva el predicado al lector del contrato: solo varían familia y
predicado."""


class PredicateInvalid(ValueError):
    """El predicado no cumple la gramática cerrada o la plantilla de su familia.

    El mensaje es genérico: nunca lleva el valor recibido.
    """

    def __init__(self) -> None:
        super().__init__("predicado fuera de la gramática cerrada del contrato")


def _json_copy(value: object, depth: int = 0) -> Any:
    """Copia JSON acotada de ``value``; ``PredicateInvalid`` si no es JSON o se desborda."""
    if depth > MAX_PREDICATE_DEPTH:
        raise PredicateInvalid
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_INT:
            raise PredicateInvalid
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > _MAX_INT:
            raise PredicateInvalid
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            raise PredicateInvalid
        return value
    if isinstance(value, list | tuple):
        if len(value) > _MAX_ITEMS:
            raise PredicateInvalid
        return [_json_copy(item, depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > _MAX_ITEMS:
            raise PredicateInvalid
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > _MAX_STRING:
                raise PredicateInvalid
            copied[key] = _json_copy(item, depth + 1)
        return copied
    raise PredicateInvalid


def _forbidden(node: Any) -> bool:
    """¿Afirma ausencia, niega, disyunta o usa el rol ``auxiliary`` en algún nivel?"""
    if isinstance(node, list):
        return any(_forbidden(item) for item in node)
    if not isinstance(node, dict):
        return False
    if _FORBIDDEN_KEYS & node.keys():
        return True
    if "presence" in node and node["presence"] is not True:
        return True
    if node.get("signal_role") == "auxiliary":
        return True
    return any(_forbidden(item) for item in node.values())


def validate_predicate(family: PredicateFamily | str, predicate: object) -> dict[str, Any]:
    """El predicado como JSON si cumple la plantilla de ``family``; si no, ``PredicateInvalid``."""
    try:
        family = PredicateFamily(family)
    except ValueError:
        raise PredicateInvalid from None
    if not isinstance(predicate, Mapping):
        raise PredicateInvalid
    document = _json_copy(predicate)
    if _forbidden(document):
        raise PredicateInvalid
    probe = {**_PROBE, "family": family.value, "predicate": document}
    try:
        # Forma JSON, como llega por la red: el modo estricto no convierte tipos.
        DeclaredStandard.model_validate_json(json.dumps(probe, allow_nan=False))
    except (ValidationError, ValueError, TypeError, RecursionError, OverflowError):
        raise PredicateInvalid from None
    return dict(document)


def canonical_conditions(predicate: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Las condiciones de la conjunción, sin orden: ``(("presence", "true"), ("energy",
    "asserted"), ...)`` ordenadas. Dos predicados con las mismas condiciones en otro orden dan lo
    mismo (la conjunción es conmutativa)."""
    conditions: list[tuple[str, str]] = []
    for condition in predicate["all_of"]:
        if "presence" in condition:
            conditions.append(("presence", "true"))
        else:
            conditions.append((str(condition["signal_role"]), str(condition["value"])))
    return tuple(sorted(conditions))
