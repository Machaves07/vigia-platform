"""PR-LAZ-24 (metapropiedad): toda ruta de texto libre de U-02, U-03 y U-04 pasa los dos
validadores.

TASK-340 (BR-LAZ-77 y 78; BLM §9; LC-LAZ-16). Extiende N-14 de U-02
(``tests/abuse/test_n14_identity_in_free_text.py``) del contenido a **todas** las rutas:

- se compone el ``RecordTypeRegistry`` **real** (``record_type_registry(registered_units())``: los
  tipos de U-02, U-03 y U-04) y la ``FreeTextPolicyRegistry`` real (``free_text_registry``, la que
  el escritor aplica a cada ruta de ``free_text_paths`` **antes de calcular el hash**), más los
  campos de ``identity`` que validan con la misma política (nombres de organización, planta, zona
  y usuario, y la licencia profesional);
- en **cada** ruta, con sus límites de esquema, un término de la lista inicial cae con
  ``blocked_vocabulary`` y un correo cae con ``person_attribution``; en las rutas de prosa caen
  también un nombre propio, un teléfono, un documento y un cargo con verbo. Las rutas que existen
  **para** un nombre de usuario o un identificador técnico (``FieldKind``) aceptan lo que les toca
  y siguen rechazando el correo y el cargo con verbo; los dos conjuntos están escritos aquí a mano
  y una ruta nueva sin declarar es de prosa (fallo cerrado);
- la **única exclusión** es la declarada de §2.12 (``VALIDATOR_EXEMPT_PATHS``:
  ``/added[*]/value`` y ``/removed[*]/value`` de ``vocabulary_list_changed``), donde los dos
  validadores dejan pasar el término y la política base sigue aplicando;
- **mutación**: sin ``person_attribution`` (o sin ``blocked_vocabulary``) en la composición, la
  metapropiedad encuentra rutas por las que el texto escapa;
- **identificadores generados por máquina** (claves de almacenamiento de clip y de evidencia con
  UUID aleatorios, UUID, sumas hexadecimales y versiones) pasan la política compuesta real en
  **toda** ruta ``identifier``: un grupo de UUID como ``cc54-4333`` no es una cédula.

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import functools
import uuid
from collections.abc import Iterable
from typing import Final

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from vigia_platform.ledger.evidence import (
    EXTENSION_BY_CONTENT_TYPE,
    StorageKey,
    format_storage_key,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.loop.record_types import VALIDATOR_EXEMPT_PATHS
from vigia_platform.loop.registration import U04_FREE_TEXT_VALIDATORS
from vigia_platform.loop.validators.attribution import (
    PERSON_ATTRIBUTION,
    PERSON_ATTRIBUTION_DETECTED,
    FieldKind,
    field_kind,
)
from vigia_platform.loop.validators.vocabulary import BLOCKED_VOCABULARY, BLOCKED_VOCABULARY_TERM
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    free_text_registry,
    record_type_registry,
    registered_units,
)

IDENTITY_FIELDS: Final = (
    FreeTextField("organization", "/name", 1, 200),
    FreeTextField("plant", "/name", 1, 200),
    FreeTextField("zone", "/name", 1, 200),
    FreeTextField("user_account", "/display_name", 1, 200),
    FreeTextField("user_account", "/professional_license", 1, 64),
)
"""Los campos de ``identity`` que pasan por ``checked_free_text`` con la política compuesta."""

NAME_PATHS: Final = frozenset(
    {
        ("action_assigned", "/assignee_display_name"),
        ("action_assigned", "/assigned_by/display_name"),
        ("action_evidence_added", "/added_by/display_name"),
        ("catalog_version_published", "/envelope/payload/standards[*]/declared_by/display_name"),
        ("classification", "/signer/display_name"),
        ("closure", "/signer/display_name"),
        ("commitments_recorded", "/recorded_by/display_name"),
        ("export_produced", "/requested_by/display_name"),
        ("loop_configuration_changed", "/changed_by/display_name"),
        ("organization_created", "/name"),
        ("plant_created", "/name"),
        ("plant_policy_signed", "/signed_by_display_name"),
        ("reopening", "/reopened_by/display_name"),
        ("review_resolution", "/resolved_by/display_name"),
        ("root_cause_analysis", "/signer/display_name"),
        ("sample_audit_result", "/reviewed_by/display_name"),
        ("unclosable_review", "/reviewer/display_name"),
        ("unclosable_state_declared", "/responsible_display_name"),
        ("unclosable_state_declared", "/signer/display_name"),
        ("vocabulary_list_changed", "/changed_by/display_name"),
        ("zone_created", "/name"),
        ("organization", "/name"),
        ("plant", "/name"),
        ("zone", "/name"),
        ("user_account", "/display_name"),
    }
)
"""Rutas que existen para el nombre visible de un usuario o el de una organización, planta o
zona: aceptan un nombre propio (escritas a mano: una ruta nueva no entra sola)."""

IDENTIFIER_PATHS: Final = frozenset(
    {
        ("action_assigned", "/assigned_by/professional_license"),
        ("action_evidence_added", "/attachments[*]/filename"),
        ("catalog_version_published", "/envelope/key_id"),
        ("closure", "/signer/professional_license"),
        ("detection_for_review_received", "/cameras[*]/clips[*]/storage_key"),
        ("detection_for_review_received", "/contract_version"),
        ("detection_for_review_received", "/node_time/clock/source"),
        ("detection_for_review_received", "/software_version"),
        ("finding_received", "/cameras[*]/clips[*]/storage_key"),
        ("finding_received", "/contract_version"),
        ("finding_received", "/node_time/clock/source"),
        ("finding_received", "/software_version"),
        ("observability_event_received", "/contract_version"),
        ("observability_event_received", "/evidence[*]/storage_key"),
        ("observability_event_received", "/node_time/clock/source"),
        ("observability_event_received", "/software_version"),
        ("plant_created", "/timezone"),
        ("plant_policy_signed", "/legal_opinion_reference"),
        ("root_cause_analysis", "/signer/professional_license"),
        ("sample_audit_result", "/reviewed_by/professional_license"),
        ("unclosable_review", "/reviewer/professional_license"),
        ("unclosable_state_declared", "/signer/professional_license"),
        ("update_result_received", "/target_version"),
        ("user_account", "/professional_license"),
    }
)
"""Rutas de identificadores técnicos o de credenciales: aceptan cifras y códigos."""

EXEMPT: Final = frozenset(
    {
        ("vocabulary_list_changed", "/added[*]/value"),
        ("vocabulary_list_changed", "/removed[*]/value"),
    }
)
"""La exclusión declarada de §2.12 y BR-LAZ-83."""

VOCABULARY: Final = "garantiza"
"""Flexión de «garantizar» (§14.2): solo la rechaza ``blocked_vocabulary``."""
EMAIL: Final = "ana@x.co"
NAME: Final = "Ana Gómez"
PHONE: Final = "3001234567"
DOCUMENT: Final = "cédula 1020304050"
ROLE: Final = "el operario omitió"
PADDING: Final = " en la zona"

type Expected = str | None
"""El motivo con que el probe debe caer (``FreeTextRejected.reason``), o ``None`` si pasa."""


def _expectations(kind: FieldKind) -> tuple[tuple[str, Expected], ...]:
    attribution = PERSON_ATTRIBUTION_DETECTED
    return (
        (VOCABULARY, BLOCKED_VOCABULARY_TERM),
        (EMAIL, attribution),
        (ROLE, attribution),
        (DOCUMENT, None if kind is FieldKind.IDENTIFIER else attribution),
        (NAME, attribution if kind is FieldKind.PROSE else None),
        (PHONE, None if kind is FieldKind.IDENTIFIER else attribution),
    )


def _fit(probe: str, field: FreeTextField) -> str | None:
    text = probe
    while len(text) < field.min_length:
        text += PADDING
    return text if len(text) <= field.max_length else None


@functools.cache
def _real_fields() -> tuple[FreeTextField, ...]:
    registry = record_type_registry(registered_units())
    fields = [
        compiled.free_text_fields[path]
        for compiled in registry.latest()
        for path in compiled.definition.free_text_paths
    ]
    return (*fields, *IDENTITY_FIELDS)


def escapes(policy: FreeTextPolicyRegistry, fields: Iterable[FreeTextField]) -> list[str]:
    """Las rutas por las que un probe no cae como debe (vacío: la metapropiedad se cumple)."""
    problems: list[str] = []
    for field in fields:
        key = (field.record_type, field.path)
        exempt = field.path in VALIDATOR_EXEMPT_PATHS.get(field.record_type, ())
        for probe, expected in _expectations(field_kind(field)):
            text = _fit(probe, field)
            if text is None:
                problems.append(f"{key}: «{probe}» no cabe en {field.max_length}")
                continue
            try:
                policy.apply(text, field)
                got: Expected = None
            except FreeTextRejected as rejected:
                got = rejected.reason
            if got != (None if exempt else expected):
                problems.append(f"{key}: «{probe}» → {got}, se esperaba {expected}")
    return problems


def _without(name: str) -> tuple[PlatformUnit, ...]:
    def strip(unit: PlatformUnit) -> PlatformUnit:
        original = unit.free_text

        def free_text(registry: FreeTextPolicyRegistry) -> None:
            full = FreeTextPolicyRegistry()
            original(full)
            for validator_name, validator in full._validators.items():
                if validator_name != name:
                    registry.register(validator_name, validator)

        return dataclasses.replace(unit, free_text=free_text)

    return tuple(strip(unit) for unit in registered_units())


# --- La metapropiedad sobre la composición real ---------------------------------------------------


def test_the_real_policy_has_both_u04_validators_in_order() -> None:
    names = free_text_registry(registered_units()).validator_names
    assert {BLOCKED_VOCABULARY, PERSON_ATTRIBUTION} == U04_FREE_TEXT_VALIDATORS
    assert names.index(BLOCKED_VOCABULARY) < names.index(PERSON_ATTRIBUTION)


def test_every_free_text_path_of_u02_u03_and_u04_goes_through_both_validators() -> None:
    fields = _real_fields()
    units = {
        compiled.writer_unit.value
        for compiled in record_type_registry(registered_units()).latest()
        if compiled.definition.free_text_paths
    }
    assert units == {"U-02", "U-03", "U-04"}
    assert len(fields) >= 80
    assert escapes(free_text_registry(registered_units()), fields) == []


def test_the_name_and_identifier_classes_are_exactly_the_declared_ones() -> None:
    by_kind: dict[FieldKind, set[tuple[str, str]]] = {kind: set() for kind in FieldKind}
    for field in _real_fields():
        by_kind[field_kind(field)].add((field.record_type, field.path))
    assert by_kind[FieldKind.NAME] == NAME_PATHS
    assert by_kind[FieldKind.IDENTIFIER] == IDENTIFIER_PATHS


def test_the_only_exclusion_is_the_declared_one_of_the_vocabulary_list() -> None:
    real = {(field.record_type, field.path) for field in _real_fields()}
    declared = {(name, path) for name, paths in VALIDATOR_EXEMPT_PATHS.items() for path in paths}
    assert declared == EXEMPT
    assert declared <= real


# --- Mutación: sin uno de los dos validadores, el texto escapa ------------------------------------


@pytest.mark.parametrize("name", [PERSON_ATTRIBUTION, BLOCKED_VOCABULARY])
def test_without_one_validator_the_metaproperty_finds_escapes(name: str) -> None:
    policy = free_text_registry(_without(name))
    assert name not in policy.validator_names
    problems = escapes(policy, _real_fields())
    assert len(problems) >= len(_real_fields()) - len(EXEMPT)


# --- Identificadores generados por máquina: nunca se rechazan (revisión de VIG-218) ---------------


@functools.cache
def _real_policy() -> FreeTextPolicyRegistry:
    return free_text_registry(registered_units())


def _identifier_fields() -> tuple[FreeTextField, ...]:
    return tuple(field for field in _real_fields() if field_kind(field) is FieldKind.IDENTIFIER)


def _group(size: int) -> st.SearchStrategy[str]:
    hex_ = st.text(alphabet="0123456789abcdef", min_size=size, max_size=size)
    digits = st.text(alphabet="0123456789", min_size=size - 2, max_size=size - 2)
    letters = st.sampled_from(("cc", "dd", "ab", "fe", "ca"))
    return st.one_of(hex_, st.tuples(letters, digits).map("".join))


_UUIDS: Final = st.tuples(*(_group(size) for size in (8, 4, 4, 4, 12))).map(
    lambda groups: uuid.UUID("-".join(groups))
)
"""UUID cuyos grupos son a menudo dos letras y cifras («cc54», «dd1234…»), como los que una
expresión de documento («cc» + número) confundiría."""
_HEX: Final = st.text(alphabet="0123456789abcdef", min_size=8, max_size=64)
_MACHINE_VALUES: Final = st.one_of(
    st.builds(
        lambda o, p, z, n, c, ext: format_storage_key(StorageKey(o, p, z, n, c, ext)),
        _UUIDS, _UUIDS, _UUIDS, _UUIDS, _UUIDS,
        st.sampled_from(sorted(EXTENSION_BY_CONTENT_TYPE.values())),
    ),
    _UUIDS.map(str),
    _HEX,
    st.tuples(*(st.integers(0, 99_999),) * 3).map(lambda v: ".".join(map(str, v))),
)  # fmt: skip
"""Claves de almacenamiento de clip y de evidencia (``org/…/node/…/<clip>.<ext>``), UUID, sumas y
versiones: lo que el nodo y la plataforma escriben, nunca una persona."""

STORAGE_KEY_WITH_CC: Final = (
    "org/7b6eb806-cc54-4333-8a2b-0c9a3f1e2d4c/plant/1f0e2d3c-4b5a-4968-8778-695a4b3c2d1e"
    "/zone/2a3b4c5d-6e7f-4801-9213-a4b5c6d7e8f9/node/3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
    "/4d5e6f7a-8b9c-4d0e-9f1a-2b3c4d5e6f7a.mp4"
)
"""Regresión de la revisión de VIG-218: «-cc54-4333» parecía una cédula (``identity_document``)."""


@given(value=_MACHINE_VALUES, data=st.data())
def test_machine_generated_identifiers_are_accepted_on_every_identifier_path(
    value: str, data: st.DataObject
) -> None:
    field = data.draw(st.sampled_from(_identifier_fields()))
    assume(field.min_length <= len(value) <= field.max_length)
    assert _real_policy().apply(value, field) == value


@pytest.mark.parametrize(
    "path",
    ["/cameras[*]/clips[*]/storage_key", "/evidence[*]/storage_key"],
)
def test_a_storage_key_with_a_cc_uuid_group_is_accepted(path: str) -> None:
    for field in _identifier_fields():
        if field.path == path:
            assert _real_policy().apply(STORAGE_KEY_WITH_CC, field) == STORAGE_KEY_WITH_CC
