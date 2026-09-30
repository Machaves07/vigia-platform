"""Generadores del oráculo del sobre canónico, compartidos por LC-NUC-09 y LC-NUC-12.

Los usan ``test_canonical_envelope_oracle.py`` (PR-NUC-47, TASK-108),
``test_ledger_canonical.py`` (PR-NUC-48, TASK-109) y
``tests/integration/test_envelope_python_vs_sql.py`` (TASK-109).

Cubren lo que pide PAT-NUC-REN-01: nombres con acentos, caracteres fuera del plano básico y de
control (incluidos ``\\u007f``, ``\\u2028`` y ``\\u2029``, que RFC 8785 no escapa), comillas y
barras inversas; marcas límite (año 1 y 9999, época, 29 de febrero, cambio de mes y de siglo);
``plant_id`` nulo, y las claves opcionales de ``actor`` y ``scope`` ausentes. Dos caracteres no
pueden llegar nunca a la base y no se generan: ``\\u0000`` (PostgreSQL no lo guarda en ``text``
ni en ``jsonb``) y los sustitutos sueltos (no son UTF-8 válido); los rechaza la base al insertar,
antes de cualquier hash, así que no hay divergencia posible por ellos.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from hypothesis import strategies as st

MAX_SAFE_INTEGER = 2**53 - 1

_TRICKY = (
    '"\\/\b\f\n\r\t\x01\x08\x0b\x0e\x1f\x7f\u0080\u009f\u00a0\u00ad\u2028\u2029\ufeff\uffff'
    "áéíóúÁÉÍÓÚñÑüÜçÇ"
    "\U0001f600\U0001f9ba\U00010000\U0010ffff\U0001d11e"
)
_any_character = st.characters(exclude_categories=["Cs"], exclude_characters="\x00")
_characters = st.one_of(st.sampled_from(_TRICKY), _any_character)


def texts(min_size: int = 0, max_size: int = 64) -> st.SearchStrategy[str]:
    """Texto con los caracteres difíciles del sobre (sin ``\\u0000`` ni sustitutos sueltos)."""
    return st.text(alphabet=_characters, min_size=min_size, max_size=max_size)


display_names = texts(min_size=1, max_size=120)
"""``display_name_snapshot`` (≤ 120 caracteres), el campo de texto libre del sobre."""

BOUNDARY_TIMESTAMPS = (
    datetime(1, 1, 1, tzinfo=UTC),
    datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(1970, 1, 1, tzinfo=UTC),
    datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(1999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(2000, 1, 1, tzinfo=UTC),
    datetime(2024, 2, 29, 12, 0, 0, 1000, tzinfo=UTC),
    datetime(2026, 9, 30, 23, 59, 59, 999000, tzinfo=UTC),
    datetime(2026, 10, 1, tzinfo=UTC),
    datetime(2038, 1, 19, 3, 14, 8, tzinfo=UTC),
)


def _to_milliseconds(value: datetime) -> datetime:
    return value.replace(microsecond=value.microsecond - value.microsecond % 1000)


timestamps = st.one_of(
    st.sampled_from(BOUNDARY_TIMESTAMPS),
    st.datetimes(timezones=st.just(UTC)).map(_to_milliseconds),
)
"""Marcas con milisegundos, que es la precisión con la que el disparador las guarda."""

hex64 = st.binary(min_size=1, max_size=16).map(lambda data: hashlib.sha256(data).hexdigest())
optional_uuid = st.one_of(st.none(), st.uuids())


@st.composite
def record_envelope_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Columnas del sobre de un registro, con texto arbitrario incluso donde la tabla lo acota."""
    return {
        "record_id": draw(st.uuids()),
        "organization_id": draw(st.uuids()),
        "plant_id": draw(optional_uuid),
        "chain_sequence": draw(st.integers(1, MAX_SAFE_INTEGER)),
        "record_type": draw(texts()),
        "schema_version": draw(st.integers(1, 2**31 - 1)),
        "actor_kind": draw(texts()),
        "actor_id": draw(st.uuids()),
        "actor_display_name_snapshot": draw(display_names),
        "actor_role_in_use": draw(st.one_of(st.none(), texts())),
        "actor_concession_id": draw(optional_uuid),
        "actor_unit": draw(texts()),
        "scope_plant_id": draw(optional_uuid),
        "scope_zone_id": draw(optional_uuid),
        "scope_node_id": draw(optional_uuid),
        "correlation_id": draw(st.uuids()),
        "received_at": draw(timestamps),
        "content_hash": draw(hex64),
    }


@st.composite
def audit_envelope_rows(draw: st.DrawFn) -> dict[str, Any]:
    """Columnas del sobre de una entrada de auditoría."""
    resource = draw(st.one_of(st.none(), st.tuples(texts(), st.uuids())))
    return {
        "entry_id": draw(st.uuids()),
        "organization_id": draw(st.uuids()),
        "chain_sequence": draw(st.integers(1, MAX_SAFE_INTEGER)),
        "actor_kind": draw(texts()),
        "actor_id": draw(st.uuids()),
        "actor_display_name_snapshot": draw(display_names),
        "actor_role_in_use": draw(st.one_of(st.none(), texts())),
        "actor_concession_id": draw(optional_uuid),
        "actor_unit": draw(texts()),
        "operation": draw(texts()),
        "scope_plant_id": draw(optional_uuid),
        "scope_zone_id": draw(optional_uuid),
        "resource_kind": None if resource is None else resource[0],
        "resource_id": None if resource is None else resource[1],
        "filters_hash": draw(st.one_of(st.none(), hex64)),
        "result_count": draw(st.one_of(st.none(), st.integers(0, 2**31 - 1))),
        "outcome": draw(texts()),
        "correlation_id": draw(st.uuids()),
        "occurred_at": draw(timestamps),
    }


_json_leaf = st.one_of(
    st.none(), st.booleans(), st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER), texts()
)
contents = st.dictionaries(
    texts(max_size=16),
    st.recursive(
        _json_leaf,
        lambda children: st.one_of(
            st.lists(children, max_size=4), st.dictionaries(texts(max_size=8), children, max_size=4)
        ),
        max_leaves=12,
    ),
    max_size=6,
)
"""Contenido JSON: se persiste como bytes canónicos y el disparador los hashea tal cual."""
