"""Los 17 tipos de registro y los 25 eventos de U-04 (TASK-302; DE §5.1, §5.2, §5.3 y §11.3).

- **Tabla de §5.1, por igualdad**: cadena, ``source_key_path``, ``free_text_paths`` narrativas
  (la columna de §5.1, exactamente), ``evidence_paths`` vacías, ``label_rule`` (solo
  ``classification`` y ``review_resolution``, con las rutas de §5.3) y eventos de cada tipo.
- **Texto libre**: ``free_text_paths`` de cada tipo = columna de §5.1 más instantáneas declaradas;
  la exclusión de ``/added[*]/value`` es declarada (``VALIDATOR_EXEMPT_PATHS``) y nunca está en
  la columna narrativa.
- **Metapropiedad de privacidad de U-02** (PR-NUC-16) sobre los 17 tipos registrados.
- **Esquema estricto**, con ``record_by_type`` de U-04: un contenido generado valida; con un campo
  de más (en la raíz o en un objeto anidado) o sin un campo obligatorio, se rechaza.
- **Eventos**: los 25 de §11.3 por igualdad (más el trabajo interno ``export_requested`` de
  TASK-317), de U-04, con la carga al tope por debajo de 64 KB.

Solo datos generados.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.properties.gob.u03_records import PERSON_FIELD_NAMES
from tests.properties.laz.strategies.u04_records import (
    event_payload,
    record_by_type,
    u04_event_registry,
    u04_registry,
)
from vigia_platform.accreditation import record_types as accreditation_types
from vigia_platform.ledger.registry import ChainLevel, CompiledType, privacy_violations
from vigia_platform.loop import record_types as loop_types
from vigia_platform.loop.registration import U04_EVENT_TYPES, U04_RECORD_TYPES
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.publish import MAX_PAYLOAD_BYTES

REGISTRY: Final = u04_registry()
EVENTS: Final = u04_event_registry()
NAMES: Final = sorted(U04_RECORD_TYPES)

DESIGN_TABLE: Final[dict[str, tuple[str, str, frozenset[str], frozenset[str]]]] = {
    # §5.1: tipo → (cadena, source_key_path, rutas de texto libre, eventos que publica)
    "classification": (
        "plant",
        "/classification_id",
        frozenset({"/reason_text"}),
        frozenset(
            {
                "finding_confirmed",
                "finding_discarded",
                "finding_reclassified",
                "loop_suspended",
                "loop_resumed",
            }
        ),
    ),
    "review_resolution": (
        "plant",
        "/resolution_id",
        frozenset({"/reason_text"}),
        frozenset({"review_confirmed", "review_discarded"}),
    ),
    "action_assigned": (
        "plant",
        "/action_id",
        frozenset({"/action_description", "/expected_outcome"}),
        frozenset({"action_assigned"}),
    ),
    "action_evidence_added": (
        "plant",
        "/evidence_entry_id",
        frozenset({"/description", "/verification_note"}),
        frozenset({"action_evidence_added"}),
    ),
    "closure": (
        "plant",
        "/closure_id",
        frozenset({"/closure_description", "/verification_note"}),
        frozenset({"finding_closed"}),
    ),
    "unclosable_state_declared": (
        "plant",
        "/declaration_id",
        frozenset({"/evaluated_alternative", "/rejection_reason", "/compensatory_measure"}),
        frozenset({"finding_unclosable"}),
    ),
    "root_cause_analysis": (
        "plant",
        "/analysis_id",
        frozenset(
            {
                "/why_steps[*]/statement",
                "/cause_effect_branches[*]/factors[*]/text",
                "/root_cause_statement",
                "/conclusion",
                "/missing_information",
            }
        ),
        frozenset({"root_cause_analysis_signed"}),
    ),
    "export_produced": ("plant", "/export_id", frozenset(), frozenset({"export_available"})),
    "monthly_package_produced": (
        "plant",
        "/package_id",
        frozenset(),
        frozenset({"monthly_package_available"}),
    ),
    "sample_audit_result": ("plant", "/sample_item_id", frozenset({"/comment"}), frozenset()),
    "vocabulary_list_changed": (
        "organization",
        "/change_id",
        frozenset({"/reason"}),
        frozenset({"vocabulary_list_changed"}),
    ),
    "action_overdue": (
        "plant",
        "/overdue_id",
        frozenset(),
        frozenset({"finding_overdue", "finding_escalated"}),
    ),
    "unclosable_review": (
        "plant",
        "/review_id",
        frozenset({"/review_note"}),
        frozenset({"unclosable_reviewed"}),
    ),
    "reopening": (
        "plant",
        "/reopening_id",
        frozenset({"/reason_text"}),
        frozenset({"finding_reopened"}),
    ),
    "commitments_recorded": (
        "plant",
        "/commitment_batch_id",
        frozenset({"/commitments[*]/text"}),
        frozenset({"commitments_recorded"}),
    ),
    "loop_configuration_changed": ("plant", "/change_id", frozenset({"/reason"}), frozenset()),
    "late_segment_attached": (
        "plant",
        "/segment_record_id",
        frozenset(),
        frozenset({"late_segment_attached"}),
    ),
}
"""La tabla consolidada de §5.1, copiada a mano del diseño (no derivada del código)."""

DESIGN_EVENTS: Final = frozenset(
    {
        "finding_confirmed",
        "finding_discarded",
        "finding_reclassified",
        "loop_suspended",
        "loop_resumed",
        "review_confirmed",
        "review_discarded",
        "review_overdue",
        "action_assigned",
        "action_evidence_added",
        "finding_closed",
        "finding_unclosable",
        "unclosable_review_due",
        "unclosable_reviewed",
        "finding_reopened",
        "finding_overdue",
        "finding_escalated",
        "root_cause_analysis_signed",
        "export_available",
        "monthly_package_available",
        "commitments_recorded",
        "adoption_low",
        "vocabulary_list_changed",
        "late_segment_attached",
        "loop_projection_divergence",
    }
)
"""Los nombres de §11.3 cuya unidad publicadora es U-04 (nota R14 de §7: veinticinco)."""

INTERNAL_EVENTS: Final = frozenset(
    {"export_requested", "assistant_requested", "email_delivery_events_poll"}
)
"""Eventos internos de U-04 fuera de §11.3: el trabajo de ``export_worker`` (TASK-317), el de
``assistant_worker`` (TASK-316) y el pulso sin carga que dispara ``email_delivery_events``
(TASK-325)."""

NARRATIVE: Final = {
    **loop_types.NARRATIVE_FREE_TEXT_PATHS,
    **accreditation_types.NARRATIVE_FREE_TEXT_PATHS,
}
SNAPSHOT: Final = {**loop_types.SNAPSHOT_TEXT_PATHS, **accreditation_types.SNAPSHOT_TEXT_PATHS}


def _compiled(name: str) -> CompiledType:
    return REGISTRY.get(name)


# --- la tabla de §5.1 -----------------------------------------------------------------------


def test_the_seventeen_types_are_exactly_those_of_the_design() -> None:
    assert len(DESIGN_TABLE) == 17
    assert set(REGISTRY.record_types()) == set(DESIGN_TABLE) == U04_RECORD_TYPES


@pytest.mark.parametrize("name", NAMES)
def test_each_type_matches_its_row_of_the_table(name: str) -> None:
    chain, source_key, free_text, events = DESIGN_TABLE[name]
    definition = _compiled(name).definition
    assert definition.writer_unit is ActorUnit.U04
    assert definition.schema_version == 1
    assert definition.chain_level is ChainLevel(chain)
    assert definition.source_key_path == source_key
    assert definition.evidence_paths == ()
    assert frozenset(definition.outbox_events) == events
    # La columna de texto libre de §5.1, por igualdad de conjuntos (no por inclusión).
    assert frozenset(NARRATIVE[name]) == free_text
    assert frozenset(definition.free_text_paths) == free_text | frozenset(SNAPSHOT[name])
    assert not free_text & frozenset(SNAPSHOT[name])


def test_only_classification_and_review_project_a_label_with_the_paths_of_5_3() -> None:
    rules = {
        name: dict(compiled.definition.label_rule.paths())
        for name in NAMES
        if (compiled := _compiled(name)).definition.label_rule is not None
    }
    common = {
        "subject_record_path": "/anchor_record_id",
        "family_path": "/family",
        "outcome_path": "/outcome",
        "reason_category_path": "/reason_category",
    }
    assert rules == {
        "classification": {**common, "labeled_by_path": "/signer"},
        "review_resolution": {**common, "labeled_by_path": "/resolved_by"},
    }


def test_the_term_value_is_excluded_by_declaration_and_never_narrative() -> None:
    assert loop_types.VALIDATOR_EXEMPT_PATHS == {
        "vocabulary_list_changed": ("/added[*]/value", "/removed[*]/value")
    }
    vocabulary = _compiled("vocabulary_list_changed").definition
    assert NARRATIVE["vocabulary_list_changed"] == ("/reason",)
    for path in loop_types.VALIDATOR_EXEMPT_PATHS["vocabulary_list_changed"]:
        assert path not in NARRATIVE["vocabulary_list_changed"]
        assert path in vocabulary.free_text_paths  # política base de U-02, sí


def test_the_snapshot_paths_are_only_user_snapshots_titles_files_and_terms() -> None:
    leaves = {path.rsplit("/", 1)[-1] for paths in SNAPSHOT.values() for path in paths}
    assert leaves == {
        "display_name",
        "professional_license",
        "assignee_display_name",
        "responsible_display_name",
        "performed_by_role",
        "filename",
        "value",
        "suggestion_es",
    }


def test_the_privacy_metaproperty_of_u02_holds_on_the_seventeen() -> None:
    assert len(REGISTRY.latest()) == 17
    assert privacy_violations(REGISTRY.latest()) == []


# --- esquema estricto -----------------------------------------------------------------------


def _validates(compiled: CompiledType, document: Any) -> bool:
    try:
        compiled.validate_json(json.dumps(document))
    except ValidationError:
        return False
    return True


def _objects(document: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(document, dict):
        found.append(document)
        for value in document.values():
            found.extend(_objects(value))
    elif isinstance(document, list):
        for value in document:
            found.extend(_objects(value))
    return found


@pytest.mark.parametrize("name", NAMES)
@settings(max_examples=20)
@given(data=st.data())
def test_a_generated_content_validates_strictly(name: str, data: st.DataObject) -> None:
    compiled = _compiled(name)
    document = data.draw(record_by_type(compiled))
    assert _validates(compiled, document), document


def _wrong_reason(document: dict[str, Any]) -> None:
    others = {
        "confirmed": "no_person_present",
        "authorized_operation": "real_coexistence",
        "false_positive": "scheduled_maintenance",
    }
    document["reason_category"] = others[document["outcome"]]


def _flip_reclassification(document: dict[str, Any]) -> None:
    document["is_reclassification"] = not document["is_reclassification"]


def _review_without_its_field(document: dict[str, Any]) -> None:
    document.pop("new_review_due_at", None)
    document.pop("new_action_id", None)


def _pending_without_missing_information(document: dict[str, Any]) -> None:
    document["state"] = "pending_information"
    document.pop("missing_information", None)


def _skip_a_why(document: dict[str, Any]) -> None:
    document["why_steps"][0]["step_index"] = 2 if len(document["why_steps"]) == 1 else 3


def _setting_with_both_values(document: dict[str, Any]) -> None:
    document["new_values"][0]["integer_value"] = 1
    document["new_values"][0]["business_calendar"] = {
        "working_weekdays": [1],
        "non_working_dates": [],
    }


def _reversed_period(document: dict[str, Any]) -> None:
    period = document["period"]
    period["starts_at"], period["ends_at"] = period["ends_at"], period["starts_at"]


def _repeated_zone(document: dict[str, Any]) -> None:
    document["zone_ids"] = [document["zone_ids"][0], document["zone_ids"][0]]


def _not_computable_with_zero(document: dict[str, Any]) -> None:
    document["metrics"] = [
        {
            "key": "adoption",
            "status": "not_computable",
            "value": 0.0,
            "unit": "count",
            "excluded_unclosable_count": 0,
        }
    ]


CROSS_FIELD_RULES: Final = (
    ("classification", _wrong_reason),
    ("classification", _flip_reclassification),
    ("unclosable_review", _review_without_its_field),
    ("root_cause_analysis", _pending_without_missing_information),
    ("root_cause_analysis", _skip_a_why),
    ("loop_configuration_changed", _setting_with_both_values),
    ("export_produced", _reversed_period),
    ("export_produced", _repeated_zone),
    ("monthly_package_produced", _not_computable_with_zero),
)
"""Las reglas entre campos que los modelos comprueban y el esquema no expresa (DE §2 y §3)."""


@pytest.mark.parametrize(
    ("name", "break_rule"),
    CROSS_FIELD_RULES,
    ids=[f"{name}:{rule.__name__}" for name, rule in CROSS_FIELD_RULES],
)
@settings(max_examples=20)
@given(data=st.data())
def test_a_content_that_breaks_a_cross_field_rule_is_rejected(
    name: str, break_rule: Any, data: st.DataObject
) -> None:
    compiled = _compiled(name)
    document = data.draw(record_by_type(compiled))
    break_rule(document)
    assert not _validates(compiled, document), document


@pytest.mark.parametrize("name", NAMES)
@settings(max_examples=20)
@given(data=st.data())
def test_an_extra_field_anywhere_is_rejected(name: str, data: st.DataObject) -> None:
    compiled = _compiled(name)
    document = data.draw(record_by_type(compiled))
    mutated = copy.deepcopy(document)
    target = data.draw(st.sampled_from(_objects(mutated)))
    field = data.draw(st.sampled_from([*PERSON_FIELD_NAMES, "note", "extra"]))
    if field in target:
        field = f"{field}_x"
    target[field] = data.draw(st.sampled_from(["Juan Pérez Gómez", "1", "true"]))
    assert not _validates(compiled, mutated)


@pytest.mark.parametrize("name", NAMES)
@settings(max_examples=20)
@given(data=st.data())
def test_a_missing_required_field_is_rejected(name: str, data: st.DataObject) -> None:
    compiled = _compiled(name)
    document = data.draw(record_by_type(compiled))
    required = sorted(compiled.content_schema.get("required", []))
    assert required, f"{name} sin campos obligatorios"
    field = data.draw(st.sampled_from(required))
    mutated = {key: value for key, value in document.items() if key != field}
    assert not _validates(compiled, mutated)


@pytest.mark.parametrize("name", NAMES)
def test_every_type_requires_its_source_key(name: str) -> None:
    compiled = _compiled(name)
    source_key = compiled.definition.source_key_path
    assert source_key is not None
    assert source_key.removeprefix("/") in compiled.content_schema["required"]


# --- eventos ----------------------------------------------------------------------------------


def test_the_twenty_five_events_are_exactly_those_of_11_3() -> None:
    # Más el trabajo interno de la exportación (TASK-317), que ningún consumidor de
    # notificaciones escucha.
    names = set(EVENTS.event_names())
    assert len(DESIGN_EVENTS) == 25
    assert names == DESIGN_EVENTS | INTERNAL_EVENTS == U04_EVENT_TYPES
    assert "user_invited" not in names  # U04-H-13: lo envía U-02
    for compiled in EVENTS.compiled_types():
        assert compiled.definition.publisher_unit is ActorUnit.U04


def test_every_type_publishes_only_registered_u04_events() -> None:
    published = {event for name in NAMES for event in _compiled(name).definition.outbox_events}
    assert published <= set(EVENTS.event_names())


@pytest.mark.parametrize("name", sorted(DESIGN_EVENTS | INTERNAL_EVENTS))
@settings(max_examples=5)
@given(data=st.data())
def test_a_payload_at_its_limit_fits_in_64_kb_and_validates(name: str, data: st.DataObject) -> None:
    compiled = EVENTS.get(name)
    assert compiled is not None
    payload = data.draw(event_payload(compiled, at_limit=True))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(body) <= MAX_PAYLOAD_BYTES
    compiled.payload_model.model_validate_json(body, strict=True)
