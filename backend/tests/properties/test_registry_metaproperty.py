"""PR-NUC-16, parte de esquemas (BR-NUC-51; NFR-NUC-35): ningún tipo registrado admite
identidad de persona observada ni texto libre fuera de ``free_text_paths``.

La metapropiedad recorre todos los esquemas registrados (hoy, los de U-02; U-03 y U-04 añaden
los suyos al mismo registro) y el registro rechaza en el arranque, nombrando el tipo y la ruta,
cualquier esquema generado que declare un campo de la lista prohibida (nombre, documento,
empleado, ``track_id``, rostro, apariencia) o un texto libre no declarado, esté donde esté: en
la raíz, anidado o dentro de una lista. Tampoco un alias (VIG-129): el validador aceptaría una
clave que el esquema no declara y el texto libre saltaría ``FreeTextPolicy``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import (
    AfterValidator,
    AliasChoices,
    AliasPath,
    Base64Bytes,
    Base64Str,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    WithJsonSchema,
    WrapValidator,
    create_model,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic_core import core_schema
from vigia_contracts.models.common import UUID, Timestamp
from vigia_contracts.models.finding import Finding

from vigia_platform.ledger.record_types import register_u02_record_types
from vigia_platform.ledger.registry import (
    ChainLevel,
    CompiledType,
    ContentModel,
    RecordType,
    RecordTypeRegistry,
    RecordTypeRejected,
    alias_problems,
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
_ZONE = "0190a8a0-0000-7000-8000-000000000000"

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


def _definition(model: type[BaseModel], **overrides: Any) -> RecordType:
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
        ("plant_created", "/timezone"),
        ("zone_created", "/name"),
        ("provider_concession_granted", "/reason"),
        # TASK-218: el motivo de la retirada de una zona (``node_zone_unassigned`` v2).
        ("node_zone_unassigned", "/reason_es"),
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


def test_forbidden_field_is_named_even_when_its_declared_path_is_malformed() -> None:
    """Contraejemplo de Hypothesis (semilla 1802709680): ``personName`` declarado en
    ``free_text_paths`` daba solo «ruta mal formada»; el motivo de privacidad se perdía."""
    model = _model(zone_id=(UUID, ...), personName=(FreeText, ...))
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model, free_text_paths=("/personName",)))
    assert "ruta declarada mal formada: '/personName'" in raised.value.problems
    assert any(
        problem.startswith("/personName: campo prohibido por BR-NUC-51")
        for problem in raised.value.problems
    )


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
            # Revisión, ronda 1: sintaxis que el motor de Rust de Pydantic entiende como abierta.
            r"^[\x20-\x7e]{1,200}$",
            r"^[ -~]{1,200}$",
            r"^[a-z\ ]{1,200}$",
            r"^[a-z\t]{1,200}$",
            r"^(\w|\x20){1,200}$",
            r"^[[:print:]]{1,200}$",
            r"^[[:space:]a-z]{1,200}$",
            r"^[\x00-\x{10FFFF}]{1,200}$",
            r"^[A-Za-z]{1,64}$",  # admite un nombre pegado
            r"^[A-Z][a-z]{1,30}$",
            r"^(?i)[a-z]{1,50}$",
            r"^(?x)[a-z ]{1,50}$",
            r"^[a-z\p{L}]{1,50}$",
            r"^[a-zé]{1,50}$",
            r"^[!-~]{1,50}$",  # ASCII visible completo: marcado y mayúsculas y minúsculas
            r"^[a-z<>]{1,50}$",
            r"^[a-z&;]{1,50}$",
            r"^[a-z]{1,50}\$",
            r"a[a-z]{1,9}b",  # sin anclar: la cadena puede llevar cualquier cosa alrededor
            r"^[a-z]{1,9}b",
            r"[a-z]{1,9}b$",
            # Revisión, ronda 2: alternancia fuera de grupo, que deja cada rama medio anclada.
            r"^[a-z]{1,8}|[0-9]{1,8}$",
            r"^([a-z]{1,8})|([0-9]{1,8})$",
            r"^(a|b$",
            r"^a)|(b$",
        ]
    )
)
def test_open_patterns_still_count_as_free_text(pattern: str) -> None:
    node = {"type": "string", "maxLength": 50, "pattern": pattern}
    assert is_free_text(node)


@pytest.mark.parametrize(
    ("pattern", "length"),
    [
        (r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", (36, 36)),
        (
            r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]"
            r":[0-5][0-9]\.[0-9]{3}Z$",
            (24, 24),
        ),
        (r"^[A-Z0-9-]{2,32}$", (2, 32)),
        (r"^[0-9a-f]{64}$", (64, 64)),
        (r"^[a-z][a-z0-9_.-]{0,63}$", (1, 64)),
        (r"^[a-z]{2}(-[a-z]+)+-[0-9]$", (9, 32)),
        (r"^[A-Za-z0-9+/]{86}==$", (88, 88)),  # firma en base64: longitud fija
        (r"^\d{4}\-\d{2}$", (7, 7)),
        (r"^(a|b)$", (1, 1)),  # alternancia dentro de un grupo anclado
        (r"^(read|write)-[0-9]{1,4}$", (6, 10)),
    ],
)
def test_closed_patterns_of_identifiers_are_not_free_text(
    pattern: str, length: tuple[int, int]
) -> None:
    node = {"type": "string", "minLength": length[0], "maxLength": length[1], "pattern": pattern}
    assert not is_free_text(node)


# --- revisión, ronda 1: el esquema debe ser el que se valida ----------------------------------


def test_format_does_not_close_a_string() -> None:
    """``format`` desconocido no lo impone Pydantic: la cadena sigue siendo texto libre."""
    assert is_free_text({"type": "string", "maxLength": 200, "format": "x"})
    assert is_free_text({"type": "string", "maxLength": 200, "format": "email"})


@pytest.mark.parametrize("annotation", [bytes, Base64Str, Base64Bytes])
@pytest.mark.parametrize("name", ["attachment", "image", "snapshot_jpeg", "foto"])
def test_binary_fields_are_rejected(annotation: Any, name: str) -> None:
    model = _model(zone_id=(UUID, ...), **{name: (annotation, ...)})
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model))
    assert f"/{name}" in str(raised.value)


@pytest.mark.parametrize(
    ("annotation", "document"),
    [
        (
            Annotated[StrictStr, Field(max_length=200), WithJsonSchema({"const": "x"})],
            "Juan Pérez Gómez, cédula 1020304050",
        ),
        (
            Annotated[StrictStr, Field(max_length=200, json_schema_extra={"format": "x"})],
            "Juan Pérez, tel 3001234567",
        ),
        (
            Annotated[
                StrictStr,
                Field(max_length=200, json_schema_extra={"pattern": "^[a-z]{1,9}$"}),
            ],
            "Juan Pérez",
        ),
    ],
)
def test_custom_json_schemas_are_rejected(annotation: Any, document: str) -> None:
    """El validador aceptaría ``document`` aunque el esquema declarado lo prohíba."""
    model = _model(zone_id=(UUID, ...), person_name=(annotation, ...), note=(annotation, ...))
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model))
    assert "JSON Schema personalizado" in str(raised.value)
    assert "/note" in str(raised.value)
    assert model.model_validate({"zone_id": _ZONE, "person_name": document, "note": document})


class _OwnSchema(ContentModel):
    zone_id: UUID

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        return {"type": "object", "additionalProperties": False, "properties": {}}


class _ExtraInConfig(ContentModel):
    model_config = ContentModel.model_config | {"json_schema_extra": {"title": "x", "x": 1}}
    zone_id: UUID


@pytest.mark.parametrize("model", [_OwnSchema, _ExtraInConfig])
def test_models_that_rewrite_their_own_schema_are_rejected(model: type[ContentModel]) -> None:
    with pytest.raises(RecordTypeRejected, match="JSON Schema personalizado"):
        RecordTypeRegistry().register(_definition(model))


def test_contract_models_keep_the_schema_pydantic_derives() -> None:
    """Los modelos generados del contrato (U-03 guarda ``Finding``) no personalizan su esquema.

    Sus cadenas de versión, origen del reloj y clave de almacenamiento mezclan mayúsculas y
    minúsculas con longitud variable: cuentan como texto libre y U-03 las declara.
    """
    declared = (
        "/contract_version",
        "/software_version",
        "/node_time/clock/source",
        "/cameras[*]/clips[*]/storage_key",
    )
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(
            _definition(Finding, record_type="finding_received", writer_unit=ActorUnit.U03)
        )
    assert "JSON Schema personalizado" not in str(raised.value)
    assert sorted(p.split(":")[0] for p in raised.value.problems) == sorted(declared)
    compiled = RecordTypeRegistry().register(
        _definition(
            Finding,
            record_type="finding_received",
            writer_unit=ActorUnit.U03,
            free_text_paths=declared,
        )
    )
    assert compiled.definition.content_model is Finding


@pytest.mark.parametrize(
    "name",
    [
        "nombre",
        "documento",
        "worker_name",
        "operator_badge",
        "id_number",
        "fingerprint_hash",
        "contact_email",
        "phone",
        "license_plate",
        "photo_ref",
        "face_embedding",
        "embedding",
        "iris_code",
        "voice_sample",
        "thumbnail_key",
    ],
)
def test_review_round_one_names_are_forbidden(name: str) -> None:
    assert forbidden_name_reason(name) is not None
    model = _model(zone_id=(UUID, ...), **{name: (Annotated[StrictInt, Field(ge=0, le=9)], ...)})
    with pytest.raises(RecordTypeRejected, match=f"/{name}: campo prohibido"):
        RecordTypeRegistry().register(_definition(model))


def test_embedding_of_bounded_floats_is_rejected() -> None:
    vector = Annotated[
        tuple[Annotated[StrictFloat, Field(ge=-1.0, le=1.0)], ...], Field(max_length=512)
    ]
    model = _model(zone_id=(UUID, ...), appearance_embedding=(vector, ...))
    with pytest.raises(RecordTypeRejected, match="/appearance_embedding: campo prohibido"):
        RecordTypeRegistry().register(_definition(model))


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
        "hardware_fingerprint",
        "certificate_fingerprint",
        "public_key_fingerprint",
        "image_digest",
    ],
)
def test_legitimate_names_used_by_other_units_are_not_forbidden(name: str) -> None:
    assert forbidden_name_reason(name) is None


def test_forbidden_booleans_are_rejected_too() -> None:
    """Un campo booleano variable con nombre prohibido también se rechaza (solo ``const`` no)."""
    model = _model(zone_id=(UUID, ...), face_visible=(StrictBool, ...))
    with pytest.raises(RecordTypeRejected, match="/face_visible: campo prohibido"):
        RecordTypeRegistry().register(_definition(model))


# --- revisión, ronda 2 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "length"),
    [
        (r"^[A-Za-z_]{64}$", 64),  # longitud fija con «_»: cabe «Juan_Perez_Gomez_CC_…»
        (r"^[A-Za-z]{40}$", 40),  # longitud fija sin dígitos: no es una codificación
        (r"^[A-Za-z0-9-]{43}$", 43),  # alfabeto con separador «-»
    ],
)
def test_fixed_length_exemption_is_only_for_base64(pattern: str, length: int) -> None:
    node = {"type": "string", "minLength": length, "maxLength": length, "pattern": pattern}
    assert is_free_text(node)


_HOSTILE = "Juan Perez Gomez cedula 1020304050 <b>x</b>"
_CLOSED = Annotated[StrictStr, Field(max_length=8, pattern=r"^[a-z]{1,8}$")]


def _keep(value: Any, handler: Any) -> Any:
    return value


class _WrapOnLiteral(ContentModel):
    zone_id: UUID
    person_name: Annotated[Literal["x"], WrapValidator(_keep)]


class _WrapOnClosedPattern(ContentModel):
    zone_id: UUID
    note: Annotated[_CLOSED, WrapValidator(_keep)]


class _FieldValidatorWrap(ContentModel):
    zone_id: UUID
    note: _CLOSED

    @field_validator("note", mode="wrap")
    @classmethod
    def _skip(cls, value: Any, handler: Any) -> Any:
        return value


class _ModelValidatorBefore(ContentModel):
    zone_id: UUID

    @model_validator(mode="before")
    @classmethod
    def _drop_extra(cls, data: Any) -> Any:
        return {key: value for key, value in dict(data).items() if key == "zone_id"}


class _PlainCode:
    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        return core_schema.no_info_plain_validator_function(
            lambda value: value,
            json_schema_input_schema=core_schema.str_schema(pattern=r"^[a-z]{1,8}$", max_length=8),
        )


class _PlainCoreSchema(ContentModel):
    zone_id: UUID
    note: _PlainCode


@pytest.mark.parametrize(
    ("model", "document"),
    [
        (_WrapOnLiteral, {"person_name": _HOSTILE}),
        (_WrapOnClosedPattern, {"note": _HOSTILE}),
        (_FieldValidatorWrap, {"note": _HOSTILE}),
        (_ModelValidatorBefore, {"person_name": _HOSTILE}),
        (_PlainCoreSchema, {"note": _HOSTILE}),
    ],
)
def test_function_validators_that_bypass_the_schema_are_rejected(
    model: type[ContentModel], document: dict[str, str]
) -> None:
    """El validador aceptaría ``document`` aunque el esquema comprobado no lo admita."""
    assert model.model_validate({"zone_id": _ZONE, **document})
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model))
    assert "validador de función before, wrap o plain" in str(raised.value)


_CLOSED_CORE = core_schema.str_schema(pattern=r"^[a-z]{1,8}$", max_length=8, strict=True)
_OPEN_CORE = core_schema.str_schema(strict=True)


class _LaxOrStrict:
    """El JSON Schema describe la rama laxa (cerrada); el modelo estricto valida con la otra."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        return core_schema.lax_or_strict_schema(lax_schema=_CLOSED_CORE, strict_schema=_OPEN_CORE)


class _JsonOrPython:
    """El JSON Schema describe la rama JSON (cerrada); ``model_validate`` usa la de Python."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        return core_schema.json_or_python_schema(json_schema=_CLOSED_CORE, python_schema=_OPEN_CORE)


class _LaxOrStrictModel(ContentModel):
    zone_id: UUID
    note: _LaxOrStrict


class _JsonOrPythonModel(ContentModel):
    zone_id: UUID
    note: _JsonOrPython


@pytest.mark.parametrize("model", [_LaxOrStrictModel, _JsonOrPythonModel])
def test_split_core_schemas_are_rejected(model: type[ContentModel]) -> None:
    assert model.model_validate({"zone_id": _ZONE, "note": _HOSTILE})
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model))
    assert "/note: esquema con dos ramas" in str(raised.value)


class _AfterValidator(ContentModel):
    zone_id: UUID
    code: Annotated[_CLOSED, AfterValidator(lambda value: value)]


def test_after_validators_are_accepted() -> None:
    """``after`` recibe el valor ya validado por el esquema: no puede ampliar lo admitido."""
    RecordTypeRegistry().register(_definition(_AfterValidator))


class _NamedLikeContractRules(ContentModel):
    """Un envolvente que se llama como el de ``ContractModel`` pero no es esa función."""

    zone_id: UUID
    note: _CLOSED

    @model_validator(mode="wrap")
    @classmethod
    def check_contract_rules(cls, data: Any, handler: Any) -> Any:
        return cls.model_construct(**dict(data))


def test_contract_rules_exemption_is_by_function_not_by_name() -> None:
    """Seguimiento nº 2 de VIG-40: la excepción del envolvente del contrato se reconoce por
    ``__func__``; otro validador con el mismo nombre se rechaza."""
    assert _NamedLikeContractRules.model_validate({"zone_id": _ZONE, "note": _HOSTILE})
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(_NamedLikeContractRules))
    assert "/: validador de función before, wrap o plain" in str(raised.value)


# --- VIG-129: alias de validación (P3; seguimiento nº 3 de VIG-40) ----------------------------

_ALIAS_FIELDS: dict[str, Any] = {
    "alias": Field(alias="nota"),
    "validation_alias": Field(validation_alias="nota"),
    "alias_choices": Field(validation_alias=AliasChoices("note", "nota")),
    "alias_path": Field(validation_alias=AliasPath("nota", 0)),
    "alias_choices_of_paths": Field(validation_alias=AliasChoices("note", AliasPath("n", "t"))),
    "serialization_alias": Field(serialization_alias="nota"),
}


def _aliased(how: str) -> Any:
    return Annotated[StrictStr, Field(min_length=1, max_length=200), _ALIAS_FIELDS[how]]


class _Generated(ContentModel):
    model_config = ContentModel.model_config | {"alias_generator": to_camel}
    zone_id: UUID
    free_note: FreeText


@pydantic_dataclass(config=ConfigDict(extra="forbid", strict=True))
class _DataclassPart:
    note: Annotated[FreeText, Field(alias="nota")]


class _DataclassHolder(ContentModel):
    zone_id: UUID
    part: _DataclassPart


def test_alias_choices_bypass_is_rejected_at_registration() -> None:
    """El caso del revisor de VIG-53: ``{"nota": "<b>…</b>"}`` validaba sin pasar por ``/note``."""
    model = _model(zone_id=(UUID, ...), note=(_aliased("alias_choices"), ...))
    assert model.model_validate_json(f'{{"zone_id": "{_ZONE}", "nota": "<b>Juan</b>"}}')
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model, free_text_paths=("/note",)))
    assert raised.value.record_type == "probe_recorded"
    message = str(raised.value)
    assert "«probe_recorded»" in message
    assert "/note: el campo declara un alias" in message
    assert "la plataforma no arranca" in message


@pytest.mark.parametrize("model", [_Generated, _DataclassHolder], ids=["generator", "dataclass"])
def test_generated_and_dataclass_aliases_are_rejected(model: type[ContentModel]) -> None:
    path = "/free_note" if model is _Generated else "/part/note"
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model, free_text_paths=(path,)))
    assert f"{path}: el campo declara un alias" in str(raised.value)


@given(
    data=st.data(),
    words=st.lists(_safe_word, min_size=1, max_size=3, unique=True),
    how=st.sampled_from(sorted(_ALIAS_FIELDS)),
    declare=st.booleans(),
)
def test_any_alias_is_rejected_and_the_metaproperty_detects_it(
    data: st.DataObject, words: list[str], how: str, declare: bool
) -> None:
    """Un alias, de cualquier forma y en cualquier lugar, impide registrar el tipo; y si un tipo
    compilado llegara con él (sin pasar por ``register``), la metapropiedad lo señala."""
    name = "_".join(words)
    model, path = data.draw(_placements(name, _aliased(how)))
    free_text = (path,) if declare else ()
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_definition(model, free_text_paths=free_text))
    assert "«probe_recorded»" in str(raised.value)
    assert any(
        problem.startswith(f"{path}: el campo declara un alias")
        for problem in raised.value.problems
    ), raised.value.problems

    definition = _definition(model, free_text_paths=(path,))
    forged = CompiledType(definition, model.model_json_schema(mode="validation"), {})
    violations = privacy_violations([forged])
    assert any(f"{path}: el campo declara un alias" in v for v in violations), violations
    assert all(v.startswith("probe_recorded v1 ") for v in violations)


def test_models_without_aliases_have_no_alias_problems() -> None:
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    for compiled in registry.all_versions():
        assert alias_problems(compiled.definition.content_model) == []
    assert alias_problems(Finding) == []
