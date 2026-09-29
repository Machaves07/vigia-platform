"""PR-NUC-16, parte de esquemas (BR-NUC-51; NFR-NUC-35): ningún tipo registrado admite
identidad de persona observada ni texto libre fuera de ``free_text_paths``.

La metapropiedad recorre todos los esquemas registrados (hoy, los de U-02; U-03 y U-04 añaden
los suyos al mismo registro) y el registro rechaza en el arranque, nombrando el tipo y la ruta,
cualquier esquema generado que declare un campo de la lista prohibida (nombre, documento,
empleado, ``track_id``, rostro, apariencia) o un texto libre no declarado, esté donde esté: en
la raíz, anidado o dentro de una lista.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import Field, StrictBool, StrictInt, StrictStr, create_model
from vigia_contracts.models.common import UUID, Timestamp

from vigia_platform.ledger.record_types import register_u02_record_types
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    RecordType,
    RecordTypeRegistry,
    RecordTypeRejected,
    privacy_violations,
)
from vigia_platform.ledger.schema_rules import (
    FORBIDDEN_NAME_PAIRS,
    FORBIDDEN_NAME_TOKENS,
    forbidden_name_reason,
    is_free_text,
    structure_problems,
)
from vigia_platform.shared.context import ActorUnit

FreeText = Annotated[StrictStr, Field(min_length=1, max_length=200)]

SAFE_WORDS = ("zone", "plant", "node", "reason", "code", "record", "batch", "status", "level")
"""Palabras que no identifican a nadie, para componer nombres de campo legítimos."""

FORBIDDEN_NAMES = (
    "person_name",
    "person_id",
    "full_name",
    "first_name",
    "last_name",
    "surname",
    "document_number",
    "identity_document",
    "national_id",
    "cedula",
    "passport_number",
    "employee_id",
    "employee_code",
    "worker_person",
    "track_id",
    "tracking_ref",
    "trackid",
    "face_crop",
    "face_embedding",
    "facial_features",
    "appearance",
    "appearance_vector",
    "clothing_color",
    "biometric_template",
    "reid_vector",
    "personName",
    "nombre_completo",
)


def _definition(model: type[ContentModel], **overrides: Any) -> RecordType:
    values: dict[str, Any] = {
        "record_type": "probe_recorded",
        "writer_unit": ActorUnit.U04,
        "chain_level": ChainLevel.PLANT,
        "schema_version": 1,
        "content_model": model,
    }
    values.update(overrides)
    return RecordType(**values)


def _model(**fields: Any) -> type[ContentModel]:
    model: type[ContentModel] = create_model("Probe", __base__=ContentModel, **fields)
    return model


# --- la metapropiedad sobre todo lo registrado -------------------------------------------------


def test_every_registered_u02_schema_satisfies_the_metaproperty() -> None:
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    assert privacy_violations(registry.all_versions()) == []
    for compiled in registry.all_versions():
        assert structure_problems(compiled.content_schema) == []


def test_u02_free_text_is_only_the_declared_names_and_the_concession_reason() -> None:
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    declared = {
        (compiled.record_type, path)
        for compiled in registry.latest()
        for path in compiled.definition.free_text_paths
    }
    assert declared == {
        ("organization_created", "/name"),
        ("plant_created", "/name"),
        ("zone_created", "/name"),
        ("provider_concession_granted", "/reason"),
    }


# --- criterio de aceptación 1 ------------------------------------------------------------------


def test_person_name_text_field_fails_naming_the_type_and_the_path() -> None:
    model = _model(zone_id=(UUID, ...), person_name=(FreeText, ...))
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model, free_text_paths=("/person_name",)))
    assert raised.value.record_type == "probe_recorded"
    message = str(raised.value)
    assert "«probe_recorded»" in message
    assert "/person_name: campo prohibido por BR-NUC-51" in message
    assert "la plataforma no arranca" in message


def test_person_name_is_also_rejected_when_undeclared() -> None:
    model = _model(zone_id=(UUID, ...), person_name=(FreeText, ...))
    with pytest.raises(RecordTypeRejected, match="/person_name: campo prohibido"):
        RecordTypeRegistry().register(_definition(model))


# --- propiedades generadas ---------------------------------------------------------------------

_safe_word = st.sampled_from(SAFE_WORDS)
_affix = st.lists(_safe_word, max_size=2).map(lambda words: "_".join(words))


def _compose(prefix: str, core: str, suffix: str) -> str:
    return "_".join(part for part in (prefix, core, suffix) if part)


_forbidden_field = st.one_of(
    st.sampled_from(FORBIDDEN_NAMES),
    st.builds(_compose, _affix, st.sampled_from(sorted(FORBIDDEN_NAME_TOKENS)), _affix),
    st.builds(
        _compose,
        _affix,
        st.sampled_from(sorted("_".join(pair) for pair in FORBIDDEN_NAME_PAIRS)),
        _affix,
    ),
)

_any_annotation = st.sampled_from(
    [
        FreeText,
        UUID,
        Timestamp,
        Annotated[StrictInt, Field(ge=0, le=10)],
        Literal["a", "b"],
        Annotated[StrictStr, Field(max_length=32, pattern=r"^[A-Z0-9-]{2,32}$")],
    ]
)


@st.composite
def _placements(draw: st.DrawFn, name: str, annotation: Any) -> tuple[type[ContentModel], str]:
    """El campo en la raíz, dentro de un objeto anidado o dentro de una lista de objetos."""
    where = draw(st.sampled_from(["root", "nested", "list"]))
    field: dict[str, Any] = {name: (annotation, ...)}
    if where == "root":
        return _model(zone_id=(UUID, ...), **field), f"/{name}"
    inner: Any = create_model("Inner", __base__=ContentModel, **field)
    if where == "nested":
        return _model(zone_id=(UUID, ...), detail=(inner, ...)), f"/detail/{name}"
    listed: Any = Annotated[tuple[inner, ...], Field(max_length=5)]
    return _model(zone_id=(UUID, ...), entries=(listed, ...)), f"/entries[*]/{name}"


@given(data=st.data(), name=_forbidden_field, annotation=_any_annotation, declare=st.booleans())
def test_any_forbidden_field_is_rejected_naming_type_and_path(
    data: st.DataObject, name: str, annotation: Any, declare: bool
) -> None:
    """Un nombre prohibido falla con cualquier tipo, en cualquier lugar, declarado o no."""
    model, path = data.draw(_placements(name, annotation))
    free_text = (path,) if declare and annotation is FreeText else ()
    record_type = data.draw(st.sampled_from(["probe_recorded", "classification", "closure"]))
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(
            _definition(model, record_type=record_type, free_text_paths=free_text)
        )
    assert raised.value.record_type == record_type
    assert f"«{record_type}»" in str(raised.value)
    assert any(
        problem.startswith(f"{path}: campo prohibido por BR-NUC-51")
        for problem in raised.value.problems
    ), raised.value.problems


@given(data=st.data(), words=st.lists(_safe_word, min_size=1, max_size=3, unique=True))
def test_undeclared_free_text_is_rejected_and_declared_free_text_is_accepted(
    data: st.DataObject, words: list[str]
) -> None:
    name = "_".join(words)
    model, path = data.draw(_placements(name, FreeText))
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model))
    assert f"{path}: texto libre fuera de las rutas de free_text_paths" in str(raised.value)

    compiled = RecordTypeRegistry().register(_definition(model, free_text_paths=(path,)))
    assert compiled.free_text_fields[path].max_length == 200
    assert privacy_violations([compiled]) == []


@given(data=st.data(), words=st.lists(_safe_word, min_size=1, max_size=3, unique=True))
def test_closed_fields_with_safe_names_are_never_flagged(
    data: st.DataObject, words: list[str]
) -> None:
    annotation = data.draw(_any_annotation.filter(lambda a: a is not FreeText))
    model, _ = data.draw(_placements("_".join(words), annotation))
    compiled = RecordTypeRegistry().register(_definition(model))
    assert privacy_violations([compiled]) == []


@given(
    pattern=st.sampled_from(
        [
            r"^.{1,50}$",
            r"^[^<>]{1,50}$",
            r"^[a-z ]{1,50}$",
            r"^\S+$",
            r"^[\s\S]{0,50}$",
            r"^\w+( \w+)*$",
            r"[a-z]{1,50}",
            r"^[a-z]{1,50}",
        ]
    )
)
def test_open_patterns_still_count_as_free_text(pattern: str) -> None:
    node = {"type": "string", "maxLength": 50, "pattern": pattern}
    assert is_free_text(node)


def test_constant_fields_are_not_examined_by_name() -> None:
    """``no_identifiable_person_declared: const true`` (U-04 §5.2) no identifica a nadie."""
    model = _model(
        zone_id=(UUID, ...),
        no_identifiable_person_declared=(Literal[True], ...),
    )
    compiled = RecordTypeRegistry().register(_definition(model))
    assert privacy_violations([compiled]) == []


@pytest.mark.parametrize(
    "name",
    [
        "document_ref",
        "document_kind",
        "interfaces",
        "display_name",
        "model_name",
        "name",
        "filename",
    ],
)
def test_legitimate_names_used_by_other_units_are_not_forbidden(name: str) -> None:
    assert forbidden_name_reason(name) is None


def test_forbidden_booleans_are_rejected_too() -> None:
    """Un campo booleano variable con nombre prohibido también se rechaza (solo ``const`` no)."""
    model = _model(zone_id=(UUID, ...), face_visible=(StrictBool, ...))
    with pytest.raises(RecordTypeRejected, match="/face_visible: campo prohibido"):
        RecordTypeRegistry().register(_definition(model))
