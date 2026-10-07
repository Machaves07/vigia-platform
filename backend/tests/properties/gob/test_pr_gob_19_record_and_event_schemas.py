"""PR-GOB-19: ningún esquema de U-03 admite texto libre sin declarar ni identidad de persona.

Metapropiedad sobre los **26 tipos de registro y los 15 eventos** de U-03 (BLM §6, transversal;
BR-NUC-51, NFR-GOB-37, NFR-GOB-41). Extiende PR-NUC-16 de U-02 (``privacy_violations``) con los
generadores ``record_by_type`` y ``mutate_record``:

- **Esquemas**: los 26 pasan la metapropiedad y las reglas de estructura del expediente; el texto
  libre de cada tipo es **exactamente** el de su ``free_text_paths``; ningún evento tiene texto
  libre ni decimales.
- **Contenidos**: todo contenido generado de cada tipo valida, y su texto libre declarado pasa la
  política (base de U-02 más el validador mínimo de U-03); cualquier mutación que cuele un campo
  de persona en un objeto, o un nombre propio en una cadena cerrada, se rechaza al validar.
- **Cargas**: lo mismo para las 15 cargas.
- **La metapropiedad detecta lo que promete**: cualquiera de los 26 modelos ampliado con un campo
  de persona o con un texto libre sin declarar no se registra, nombrando el tipo y la ruta.

Perfil ``ci`` de Hypothesis; la semilla se registra en la cabecera de la sesión (``conftest``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Annotated, Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, Field, StrictStr, ValidationError, create_model

from tests.properties.gob.u03_records import (
    PERSON_FIELD_NAMES,
    content_path,
    event_payload,
    free_text_nodes,
    mutate_record,
    record_by_type,
    u03_event_registry,
    u03_registry,
)
from vigia_platform.catalog.application.free_text_validator import (
    affirms_intent,
    register_u03_free_text_validator,
)
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextPolicyRegistry,
    canonical_form,
)
from vigia_platform.ledger.registry import (
    ChainLevel,
    CompiledType,
    RecordTypeRegistry,
    RecordTypeRejected,
    privacy_violations,
)
from vigia_platform.ledger.schema_rules import privacy_problems, structure_problems
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import (
    CompiledEventType,
    EventType,
    EventTypeRegistry,
    OutboxRegistrationRejected,
)

REGISTRY = u03_registry()
EVENTS = u03_event_registry()
TYPES = REGISTRY.latest()
EVENT_TYPES = EVENTS.compiled_types()

FREE_TEXT = FreeTextPolicyRegistry()
register_u03_free_text_validator(FREE_TEXT)
FREE_TEXT.seal()

U03_RECORD_TYPES = frozenset(
    {
        "finding_received",
        "detection_for_review_received",
        "observability_event_received",
        "node_communication_state_changed",
        "node_enrolled",
        "node_credential_rotated",
        "node_revoked",
        "update_result_received",
        "node_target_version_published",
        "catalog_version_published",
        "standard_admission_test",
        "gate_state_changed",
        "mounting_gate_record",
        "use_agreement_signed",
        "commissioning_step",
        "walk_test_result",
        "plant_policy_signed",
        "occlusion_test_result",
        "walk_test_regression_marked",
        "walk_test_regression_cleared",
        "catalog_standard_retired",
        "single_occupancy_declared",
        "node_decommissioned",
        "enrollment_code_issued",
        "enrollment_attempt_rejected",
        "ingest_rejected",
    }
)
"""Los 26 tipos de ``domain-entities.md`` §5."""

U03_EVENTS = frozenset(
    {
        "finding_received",
        "detection_for_review_received",
        "observability_event_received",
        "gate_state_changed",
        "zone_activated",
        "catalog_updated",
        "regression_marked",
        "regression_cleared",
        "node_enrolled",
        "node_revoked",
        "node_decommissioned",
        "target_version_published",
        "update_result_received",
        "fleet_alarm_raised",
        "fleet_alarm_cleared",
    }
)
"""Los 15 eventos de ``domain-entities.md`` §6."""

_SETTINGS = settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])


# --- esquemas ------------------------------------------------------------------------------------


def test_exactly_the_26_types_and_the_15_events_are_declared() -> None:
    assert set(REGISTRY.record_types()) == U03_RECORD_TYPES
    assert len(U03_RECORD_TYPES) == 26
    assert set(EVENTS.event_names()) == U03_EVENTS
    assert len(U03_EVENTS) == 15


LATEST_VERSIONS: Final = {"walk_test_result": 2}
"""Tipos con una versión posterior a la 1 (TASK-216 amplía el acta); la 1 sigue registrada."""


def test_every_type_is_written_by_u03_in_the_plant_chain_at_version_1() -> None:
    versions: dict[str, set[int]] = {}
    for compiled in REGISTRY.all_versions():
        versions.setdefault(compiled.record_type, set()).add(compiled.schema_version)
    for compiled in TYPES:
        definition = compiled.definition
        latest = LATEST_VERSIONS.get(compiled.record_type, 1)
        assert definition.writer_unit is ActorUnit.U03, compiled.record_type
        assert definition.chain_level is ChainLevel.PLANT, compiled.record_type
        assert definition.schema_version == latest, compiled.record_type
        assert versions[compiled.record_type] == set(range(1, latest + 1)), compiled.record_type
        assert not definition.chain_follows_scope, compiled.record_type
        config = definition.content_model.model_config
        assert config.get("extra") == "forbid" and config.get("strict") is True


def test_every_u03_schema_satisfies_pr_nuc_16_and_the_structure_rules() -> None:
    assert privacy_violations(REGISTRY.all_versions()) == []
    for compiled in TYPES:
        assert structure_problems(compiled.content_schema) == [], compiled.record_type


def test_free_text_is_exactly_the_declared_paths() -> None:
    """Ni una ruta de texto libre sin declarar, ni una declarada que no lo sea."""
    for compiled in TYPES:
        declared = set(compiled.definition.free_text_paths)
        assert free_text_nodes(compiled.content_schema) == declared, compiled.record_type


def test_events_carry_no_free_text_and_no_decimals() -> None:
    for compiled in EVENT_TYPES:
        schema = compiled.payload_schema
        assert privacy_problems(schema, ()) == [], compiled.event_name
        assert free_text_nodes(schema) == set(), compiled.event_name
        assert structure_problems(schema) == [], compiled.event_name
        assert '"number"' not in json.dumps(schema), compiled.event_name
        assert compiled.definition.publisher_unit is ActorUnit.U03


def test_evidence_paths_are_only_clips() -> None:
    """Los ``document_ref`` de U-03 no son ``Evidence`` de U-02 (nota de §3.14)."""
    evidence = {
        c.record_type: c.definition.evidence_paths for c in TYPES if c.definition.evidence_paths
    }
    assert evidence == {
        "finding_received": ("/cameras[*]/clips[*]",),
        "detection_for_review_received": ("/cameras[*]/clips[*]",),
        "observability_event_received": ("/evidence[*]",),
    }


# --- contenidos: record_by_type y mutate_record ------------------------------------------------

_types = st.sampled_from(TYPES)


def _validate(compiled: CompiledType, document: Any) -> BaseModel:
    return compiled.validate_json(json.dumps(document))


def _declared_texts(compiled: CompiledType, document: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def visit(value: Any, pointer: tuple[Any, ...]) -> None:
        if isinstance(value, str) and content_path(pointer) in compiled.free_text_fields:
            found.append((content_path(pointer), value))
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*pointer, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, (*pointer, index))

    visit(document, ())
    return found


@_SETTINGS
@given(data=st.data(), compiled=_types)
def test_generated_contents_of_every_type_validate_and_pass_the_policy(
    data: st.DataObject, compiled: CompiledType
) -> None:
    document = data.draw(record_by_type(compiled))
    _validate(compiled, document)
    for path, text in _declared_texts(compiled, document):
        candidate = FreeTextCandidate(text=text, canonical=canonical_form(text))
        if not affirms_intent(candidate):
            FREE_TEXT.apply(text, compiled.free_text_fields[path])


@_SETTINGS
@given(data=st.data(), compiled=_types)
def test_any_person_data_slipped_into_a_content_is_rejected(
    data: st.DataObject, compiled: CompiledType
) -> None:
    document = data.draw(record_by_type(compiled))
    mutation = data.draw(mutate_record(document, compiled.definition.free_text_paths))
    with pytest.raises(ValidationError):
        _validate(compiled, mutation.document)


@_SETTINGS
@given(data=st.data(), compiled=st.sampled_from(EVENT_TYPES))
def test_any_person_data_slipped_into_an_event_payload_is_rejected(
    data: st.DataObject, compiled: CompiledEventType
) -> None:
    payload = data.draw(event_payload(compiled))
    compiled.payload_model.model_validate_json(json.dumps(payload), strict=True)
    mutation = data.draw(mutate_record(payload, ()))
    with pytest.raises(ValidationError):
        compiled.payload_model.model_validate_json(json.dumps(mutation.document), strict=True)


# --- la metapropiedad detecta un campo prohibido o un texto libre sin declarar ---------------

FreeText = Annotated[StrictStr, Field(min_length=1, max_length=200)]


def _widened(base: type[BaseModel], name: str) -> type[BaseModel]:
    """``base`` con un campo obligatorio de texto libre llamado ``name``."""
    fields: dict[str, Any] = {name: (FreeText, ...)}
    return create_model("Widened", __base__=base, **fields)


@_SETTINGS
@given(compiled=_types, name=st.sampled_from(PERSON_FIELD_NAMES), declare=st.booleans())
def test_a_u03_type_widened_with_a_person_field_is_not_registered(
    compiled: CompiledType, name: str, declare: bool
) -> None:
    widened = _widened(compiled.definition.content_model, name)
    free_text = (*compiled.definition.free_text_paths, *((f"/{name}",) if declare else ()))
    definition = replace(compiled.definition, content_model=widened, free_text_paths=free_text)
    with pytest.raises(RecordTypeRejected) as rejected:
        RecordTypeRegistry().register(definition)
    assert rejected.value.record_type == compiled.record_type
    assert any(problem.startswith(f"/{name}:") for problem in rejected.value.problems)


@_SETTINGS
@given(compiled=_types, name=st.sampled_from(["note", "comment", "detail", "observation"]))
def test_a_u03_type_widened_with_undeclared_free_text_is_not_registered(
    compiled: CompiledType, name: str
) -> None:
    widened = _widened(compiled.definition.content_model, name)
    definition = replace(compiled.definition, content_model=widened)
    with pytest.raises(RecordTypeRejected) as rejected:
        RecordTypeRegistry().register(definition)
    assert f"/{name}: texto libre fuera de las rutas de free_text_paths" in rejected.value.problems


@_SETTINGS
@given(
    compiled=st.sampled_from(EVENT_TYPES),
    name=st.sampled_from([*PERSON_FIELD_NAMES, "note", "reason_es", "comment"]),
)
def test_a_u03_event_widened_with_text_or_a_person_field_is_not_registered(
    compiled: CompiledEventType, name: str
) -> None:
    widened = _widened(compiled.payload_model, name)
    event = EventType(
        event_name=compiled.event_name,
        publisher_unit=ActorUnit.U03,
        payload_model=widened,
        description_es=compiled.definition.description_es,
    )
    with pytest.raises(OutboxRegistrationRejected) as rejected:
        EventTypeRegistry().register(event)
    assert any(problem.startswith(f"/{name}:") for problem in rejected.value.problems)
