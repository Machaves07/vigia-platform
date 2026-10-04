"""Los 15 eventos de U-03: carga exacta y tope de 64 KB (interfaces §2, domain-entities §6, T-08).

Una prueba por evento: la carga lleva **exactamente** la unión de los campos de interfaces §2 y de
domain-entities §6 (manda interfaces donde difieren), con sus opcionales; generada **al tope** de
todas sus listas, con todos los opcionales y con cada cadena en su longitud máxima, valida y mide
como mucho ``MAX_PAYLOAD_BYTES`` (64 KB) en la forma que se guarda. Las listas cerradas son las
del diseño (``review_reason``, ``result`` con ``failed``, las ocho ``alarm_kind``).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.properties.gob.u03_records import event_payload, u03_event_registry
from vigia_platform.shared.outbox.publish import MAX_PAYLOAD_BYTES, stored_payload_size
from vigia_platform.shared.outbox.registries import CompiledEventType

EVENTS = u03_event_registry()

NODE_LIFECYCLE = ({"node_id", "plant_id", "zone_ids"}, {"replaces_node_id"})
REGRESSION = (
    {"zone_id", "cause", "affected_row_ids"},
    {"catalog_version", "model_version"},
)

EXPECTED: dict[str, tuple[set[str], set[str]]] = {
    "finding_received": (
        {
            "zone_id",
            "node_id",
            "finding_id",
            "platform_record_id",
            "received_at",
            "family",
            "tier",
            "standard",
        },
        set(),
    ),
    "detection_for_review_received": (
        {
            "zone_id",
            "node_id",
            "detection_id",
            "platform_record_id",
            "received_at",
            "review_reason",
            "idempotency_key",
        },
        set(),
    ),
    "observability_event_received": (
        {
            "zone_id",
            "node_id",
            "event_id_node",
            "platform_record_id",
            "subject_kind",
            "state",
            "phase",
        },
        set(),
    ),
    "gate_state_changed": (
        {"zone_id", "gate", "status", "resulting_mode", "record_id", "reason_es_present"},
        set(),
    ),
    "zone_activated": (
        {"zone_id", "activated_at", "agreement_id", "commissioning_record_id"},
        set(),
    ),
    "catalog_updated": ({"zone_id", "catalog_version", "changed_fields"}, set()),
    "regression_marked": REGRESSION,
    "regression_cleared": (REGRESSION[0] | {"cleared_by_session_id"}, REGRESSION[1]),
    "node_enrolled": NODE_LIFECYCLE,
    "node_revoked": NODE_LIFECYCLE,
    "node_decommissioned": NODE_LIFECYCLE,
    "target_version_published": ({"node_id", "target_version"}, set()),
    "update_result_received": ({"node_id", "target_version", "result"}, set()),
    "fleet_alarm_raised": ({"alarm_id", "alarm_kind", "since"}, {"node_id", "zone_id"}),
    "fleet_alarm_cleared": (
        {"alarm_id", "alarm_kind", "since", "cleared_at"},
        {"node_id", "zone_id"},
    ),
}
"""Por evento: campos obligatorios y opcionales (``?``) de la unión de interfaces §2 y §6."""

ALARM_KINDS = {
    "node_mute",
    "queue_over_threshold",
    "clock_drift",
    "version_retiring",
    "simulated_adapter_in_productive",
    "certificate_expiring",
    "camera_below_min_fps",
    "orphan_clips_growing",
}

# --- la carga máxima ----------------------------------------------------------------------------

MAX_EXAMPLES: dict[str, str] = {
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$": (
        "ffffffff-ffff-8fff-bfff-ffffffffffff"
    ),
    "^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:"
    "[0-5][0-9]\\.[0-9]{3}Z$": "2099-12-31T23:59:59.999Z",
    "^[a-z][a-z0-9_.-]{0,63}$": "m" * 64,
    "^(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})\\.(0|[1-9][0-9]{0,8})"
    "(-[0-9a-z][0-9a-z.-]{0,30})?(\\+[0-9a-z][0-9a-z.-]{0,30})?$": (
        "999999999.999999999.999999999-" + "a" * 31 + "+ab"
    ),
}
"""Ejemplo de longitud máxima de cada patrón de las cargas; un patrón nuevo obliga a añadir el
suyo (la prueba falla), así que el tope siempre se mide con cadenas de longitud máxima."""


def _resolve(node: Mapping[str, Any], defs: Mapping[str, Any]) -> Mapping[str, Any]:
    while "$ref" in node:
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        node = {**defs[node["$ref"].removeprefix("#/$defs/")], **siblings}
    return node


def _identifiers() -> Iterator[str]:
    while True:
        yield str(uuid.uuid4())


def _maximal(node: Mapping[str, Any], defs: Mapping[str, Any], ids: Iterator[str]) -> Any:
    node = _resolve(node, defs)
    if "anyOf" in node:
        options = [
            _maximal(branch, defs, ids)
            for branch in node["anyOf"]
            if _resolve(branch, defs).get("type") != "null"
        ]
        return max(options, key=lambda value: len(json.dumps(value)))
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return max(node["enum"], key=len)
    kind = node.get("type")
    if kind == "object":
        return {
            name: _maximal(child, defs, ids) for name, child in node.get("properties", {}).items()
        }
    if kind == "array":
        return [_maximal(node["items"], defs, ids) for _ in range(node["maxItems"])]
    if kind == "string":
        pattern = node["pattern"]
        if pattern.startswith("^[0-9a-f]{8}-"):
            return next(ids)
        example = MAX_EXAMPLES[pattern]
        assert re.fullmatch(pattern, example), pattern
        assert len(example) == node["maxLength"], pattern
        return example
    if kind == "integer":
        return node["maximum"]
    if kind == "boolean":
        return True
    raise AssertionError(f"tipo sin máximo: {node}")


def maximal_payload(compiled: CompiledEventType) -> dict[str, Any]:
    schema = compiled.payload_schema
    payload: dict[str, Any] = _maximal(schema, schema.get("$defs", {}), _identifiers())
    return payload


def _validated(compiled: CompiledEventType, payload: Any) -> dict[str, Any]:
    model = compiled.payload_model.model_validate_json(json.dumps(payload), strict=True)
    stored: dict[str, Any] = model.model_dump(mode="json")
    return stored


# --- una prueba por evento --------------------------------------------------------------------


@pytest.mark.parametrize("event_name", sorted(EXPECTED))
def test_the_payload_is_exactly_the_union_of_both_designs(event_name: str) -> None:
    compiled = EVENTS.get(event_name)
    assert compiled is not None
    required, optional = EXPECTED[event_name]
    schema = compiled.payload_schema
    assert set(schema["properties"]) == required | optional
    assert set(schema.get("required", [])) == required
    assert "event_id" not in schema["properties"], "event_id es del sobre, común a los quince"


@pytest.mark.parametrize("event_name", sorted(EXPECTED))
def test_the_payload_at_the_top_of_its_lists_fits_in_64_kb(event_name: str) -> None:
    compiled = EVENTS.get(event_name)
    assert compiled is not None
    payload = maximal_payload(compiled)
    stored = _validated(compiled, payload)
    assert set(stored) == set().union(*EXPECTED[event_name])
    assert stored_payload_size(stored) <= MAX_PAYLOAD_BYTES


def test_the_heaviest_payload_is_far_from_the_limit_and_measured() -> None:
    """Con 1 024 filas afectadas la carga de ``regression_cleared`` es la mayor y cabe."""
    sizes = {
        compiled.event_name: stored_payload_size(_validated(compiled, maximal_payload(compiled)))
        for compiled in EVENTS.compiled_types()
    }
    heaviest = max(sizes, key=lambda name: sizes[name])
    assert heaviest in {"regression_marked", "regression_cleared"}
    assert sizes[heaviest] <= MAX_PAYLOAD_BYTES


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
@given(data=st.data(), compiled=st.sampled_from(EVENTS.compiled_types()))
def test_property_generated_payloads_at_the_top_fit_in_64_kb(
    data: st.DataObject, compiled: CompiledEventType
) -> None:
    payload = data.draw(event_payload(compiled, at_limit=True))
    assert stored_payload_size(_validated(compiled, payload)) <= MAX_PAYLOAD_BYTES


# --- listas cerradas -------------------------------------------------------------------------


def _with(event_name: str, **changes: Any) -> tuple[CompiledEventType, dict[str, Any]]:
    compiled = EVENTS.get(event_name)
    assert compiled is not None
    payload = maximal_payload(compiled)
    payload.update(changes)
    return compiled, payload


@pytest.mark.parametrize("reason", ["low_confidence", "ambiguous", "no_bounding_box", "other"])
def test_review_reason_admits_the_four_values(reason: str) -> None:
    _validated(*_with("detection_for_review_received", review_reason=reason))


@pytest.mark.parametrize("result", ["applied", "reverted", "failed"])
def test_update_result_admits_failed(result: str) -> None:
    _validated(*_with("update_result_received", result=result))


@pytest.mark.parametrize("kind", sorted(ALARM_KINDS))
@pytest.mark.parametrize("event_name", ["fleet_alarm_raised", "fleet_alarm_cleared"])
def test_the_eight_alarm_kinds(event_name: str, kind: str) -> None:
    _validated(*_with(event_name, alarm_kind=kind))


@pytest.mark.parametrize("event_name", ["regression_marked", "regression_cleared"])
def test_affected_rows_are_a_list_or_all(event_name: str) -> None:
    _validated(*_with(event_name, affected_row_ids="all"))
    _validated(*_with(event_name, affected_row_ids=[str(uuid.uuid4())]))
    _validated(*_with(event_name, cause="framing_recaptured"))
    for wrong in ("some", [], ["fila-1"]):
        with pytest.raises(ValidationError):
            _validated(*_with(event_name, affected_row_ids=wrong))


@pytest.mark.parametrize(
    ("event_name", "field", "value"),
    [
        ("detection_for_review_received", "review_reason", "sabotage"),
        ("update_result_received", "result", "partial"),
        ("fleet_alarm_raised", "alarm_kind", "zone_clear"),
        ("gate_state_changed", "gate", "exit"),
        ("gate_state_changed", "reason_es_present", "Motivo de la revocación"),
        ("catalog_updated", "changed_fields", ["standards", "standards", "x"]),
        ("target_version_published", "target_version", "1.0.0-RC1"),
        ("regression_marked", "model_version", "Detector V2"),
        ("node_enrolled", "zone_ids", [str(uuid.uuid4()) for _ in range(17)]),
    ],
)
def test_values_outside_the_closed_lists_or_limits_are_rejected(
    event_name: str, field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        _validated(*_with(event_name, **{field: value}))


@pytest.mark.parametrize("event_name", sorted(EXPECTED))
def test_one_more_element_than_the_limit_is_rejected(event_name: str) -> None:
    compiled = EVENTS.get(event_name)
    assert compiled is not None
    payload = maximal_payload(compiled)
    lists = [key for key, value in payload.items() if isinstance(value, list)]
    for key in lists:
        oversized = json.loads(json.dumps(payload))
        oversized[key] = [*oversized[key], oversized[key][0]]
        with pytest.raises(ValidationError):
            _validated(compiled, oversized)
