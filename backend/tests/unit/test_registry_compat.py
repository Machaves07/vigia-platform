"""Registro de tipos: versiones, compatibilidad y persistencia (LC-NUC-08; BR-NUC-52).

Un tipo se registra con todas sus versiones en orden; cada una debe ampliar a la anterior. Lo
registrado se contrasta con la tabla ``ledger.record_type`` (aquí, el adaptador en memoria):
retirar un campo, estrechar un rango, retirar un tipo o una versión o cambiar un esquema sin
subir la versión impiden arrancar con un mensaje en español que nombra el tipo y la ruta.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from typing import Annotated, Any, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    create_model,
)
from vigia_contracts.models.common import UUID

from vigia_platform.ledger.record_types import U02_RECORD_TYPES, register_u02_record_types
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    InMemoryRecordTypeStore,
    LabelRule,
    PersistedRecordType,
    RecordType,
    RecordTypeRegistry,
    RecordTypeRejected,
    RecordTypeUnknown,
    RegistryStartupError,
)
from vigia_platform.ledger.schema_rules import compatibility_problems
from vigia_platform.shared.context import ActorUnit

Short = Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=r"^[a-z]{1,40}$")]


def _type(model: type[BaseModel], version: int = 1, **overrides: Any) -> RecordType:
    values: dict[str, Any] = {
        "record_type": "sample_recorded",
        "writer_unit": ActorUnit.U03,
        "chain_level": ChainLevel.PLANT,
        "schema_version": version,
        "content_model": model,
    }
    values.update(overrides)
    return RecordType(**values)


class SampleV1(ContentModel):
    sample_id: UUID
    code: Short
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]


class SampleV2Widened(ContentModel):
    """Amplía: campo opcional nuevo, rango mayor, longitud mayor y un valor más en la lista."""

    sample_id: UUID
    code: Annotated[StrictStr, Field(min_length=1, max_length=80, pattern=r"^[a-z]{1,40}$")]
    level: Annotated[StrictInt, Field(ge=0, le=100)]
    kind: Literal["a", "b", "c"]
    extra_note_code: Short | None = None


class SampleV2WithoutCode(ContentModel):
    """Retira ``code``."""

    sample_id: UUID
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# --- criterio de aceptación 2: retirar un campo impide arrancar -------------------------------


def test_new_version_removing_a_field_is_rejected_in_spanish() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    with pytest.raises(RecordTypeRejected) as raised:
        registry.register(_type(SampleV2WithoutCode, version=2))
    message = str(raised.value)
    assert "No se puede registrar el tipo de registro «sample_recorded»" in message
    assert "la plataforma no arranca" in message
    assert "/code: campo retirado (v1 → v2)" in message
    assert registry.get("sample_recorded").schema_version == 1


def test_persisted_version_with_a_field_the_code_removed_blocks_startup() -> None:
    old = RecordTypeRegistry()
    old.register(_type(SampleV1))
    store = InMemoryRecordTypeStore()
    _run(old.synchronize(store))

    new = RecordTypeRegistry()
    new.register(_type(SampleV2WithoutCode, version=2))
    with pytest.raises(RegistryStartupError) as raised:
        _run(new.synchronize(store))
    message = str(raised.value)
    assert "incompatible con el ya persistido; la plataforma no arranca" in message
    assert "sample_recorded v1 → v2 /code: campo retirado" in message
    assert not new.sealed
    assert _run(store.load())["sample_recorded"].schema_version == 1


# --- estrechamientos y ampliaciones ----------------------------------------------------------


class NarrowedRange(ContentModel):
    sample_id: UUID
    code: Short
    level: Annotated[StrictInt, Field(ge=1, le=10)]
    kind: Literal["a", "b"]


class NarrowedMaximum(ContentModel):
    sample_id: UUID
    code: Short
    level: Annotated[StrictInt, Field(ge=0, le=9)]
    kind: Literal["a", "b"]


class NarrowedLength(ContentModel):
    sample_id: UUID
    code: Annotated[StrictStr, Field(min_length=1, max_length=39, pattern=r"^[a-z]{1,40}$")]
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]


class RaisedMinLength(ContentModel):
    sample_id: UUID
    code: Annotated[StrictStr, Field(min_length=2, max_length=40, pattern=r"^[a-z]{1,40}$")]
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]


class RemovedEnumValue(ContentModel):
    sample_id: UUID
    code: Short
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a"]


class NewRequiredField(ContentModel):
    sample_id: UUID
    code: Short
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]
    batch_id: UUID


class ChangedType(ContentModel):
    sample_id: UUID
    code: Short
    level: Short
    kind: Literal["a", "b"]


class ChangedPattern(ContentModel):
    sample_id: UUID
    code: Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=r"^[a-z0-9]{1,40}$")]
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    kind: Literal["a", "b"]


class OptionalV1(ContentModel):
    sample_id: UUID
    note_code: Short | None = None


class OptionalBecameRequired(ContentModel):
    sample_id: UUID
    note_code: Short


class OptionalBecameNonNullable(ContentModel):
    sample_id: UUID
    note_code: Short = "x"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (SampleV1, NarrowedRange, "/level: mínimo elevado (rango estrechado)"),
        (SampleV1, NarrowedMaximum, "/level: máximo reducido (rango estrechado)"),
        (SampleV1, NarrowedLength, "/code: longitud máxima reducida de 40 a 39"),
        (SampleV1, RaisedMinLength, "/code: longitud mínima elevada de 1 a 2"),
        (SampleV1, RemovedEnumValue, "/kind: valores retirados de la lista: ['b']"),
        (SampleV1, NewRequiredField, "/batch_id: campo obligatorio nuevo"),
        (SampleV1, ChangedType, "/level: ya no admite valores de tipo integer"),
        (SampleV1, ChangedPattern, "/code: «pattern» cambiado o añadido"),
        (OptionalV1, OptionalBecameRequired, "/note_code: campo opcional que pasó a obligatorio"),
        (OptionalV1, OptionalBecameNonNullable, "/note_code: ya no admite valores de tipo null"),
    ],
)
def test_narrowing_is_rejected_naming_the_path(
    old: type[BaseModel], new: type[BaseModel], expected: str
) -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(old))
    with pytest.raises(RecordTypeRejected) as raised:
        registry.register(_type(new, version=2))
    assert expected in str(raised.value)


def test_widening_requires_a_higher_version_and_keeps_every_version() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    with pytest.raises(RecordTypeRejected, match="no es mayor que la ya registrada"):
        registry.register(_type(SampleV2Widened, version=1))
    registry.register(_type(SampleV2Widened, version=2))
    assert registry.get("sample_recorded").schema_version == 2
    assert registry.get("sample_recorded", schema_version=1).definition.content_model is SampleV1
    with pytest.raises(RecordTypeUnknown, match="no está registrado en su versión 3"):
        registry.get("sample_recorded", schema_version=3)


def test_versions_are_registered_in_ascending_order() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV2Widened, version=2))
    with pytest.raises(RecordTypeRejected, match="la versión 1 no es mayor"):
        registry.register(_type(SampleV1, version=1))


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("writer_unit", ActorUnit.U04),
        ("chain_level", ChainLevel.ORGANIZATION),
        ("chain_follows_scope", True),
        ("source_key_path", "/sample_id"),
    ],
)
def test_immutable_attributes_cannot_change_between_versions(attribute: str, value: Any) -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    with pytest.raises(RecordTypeRejected, match=f"«{attribute}» cambió"):
        registry.register(_type(SampleV2Widened, version=2, **{attribute: value}))


def test_widened_version_is_persisted_over_the_previous_row() -> None:
    old = RecordTypeRegistry()
    old.register(_type(SampleV1))
    store = InMemoryRecordTypeStore()
    _run(old.synchronize(store))

    new = RecordTypeRegistry()
    new.register(_type(SampleV1))
    new.register(_type(SampleV2Widened, version=2))
    _run(new.synchronize(store))
    row = _run(store.load())["sample_recorded"]
    assert row.schema_version == 2
    assert "extra_note_code" in row.content_schema["properties"]
    assert new.sealed


def test_widened_version_is_accepted_against_the_row_without_the_old_code() -> None:
    old = RecordTypeRegistry()
    old.register(_type(SampleV1))
    store = InMemoryRecordTypeStore()
    _run(old.synchronize(store))

    new = RecordTypeRegistry()
    new.register(_type(SampleV2Widened, version=2))
    _run(new.synchronize(store))
    assert _run(store.load())["sample_recorded"].schema_version == 2


def _persisted(registry: RecordTypeRegistry, **changes: Any) -> dict[str, PersistedRecordType]:
    row = registry.get("sample_recorded").to_persisted()
    return {"sample_recorded": dataclasses.replace(row, **changes)}


def test_schema_changed_without_a_new_version_blocks_startup() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    schema = dict(registry.get("sample_recorded").content_schema)
    properties = dict(schema["properties"])
    properties.pop("kind")
    schema["properties"] = properties
    store = InMemoryRecordTypeStore(_persisted(registry, content_schema=schema))
    with pytest.raises(RegistryStartupError, match="cambió sin subir schema_version"):
        _run(registry.synchronize(store))


def test_declared_paths_changed_without_a_new_version_block_startup() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1, outbox_events=("sample_recorded",)))
    store = InMemoryRecordTypeStore(_persisted(registry, outbox_events=()))
    with pytest.raises(RegistryStartupError, match="los eventos de la versión 1 cambiaron"):
        _run(registry.synchronize(store))


def test_persisted_version_newer_than_the_code_blocks_startup() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    store = InMemoryRecordTypeStore(_persisted(registry, schema_version=2))
    with pytest.raises(RegistryStartupError) as raised:
        _run(registry.synchronize(store))
    assert "la base tiene la versión 2 y el código solo llega a la 1" in str(raised.value)
    assert "retirar una versión está prohibido (BR-NUC-52)" in str(raised.value)


def test_persisted_type_that_no_unit_registers_blocks_startup() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    rows = _persisted(registry)
    rows["retired_type"] = dataclasses.replace(rows["sample_recorded"], record_type="retired_type")
    with pytest.raises(RegistryStartupError, match="retired_type: el tipo está en la base"):
        _run(registry.synchronize(InMemoryRecordTypeStore(rows)))


def test_persisted_writer_unit_mismatch_blocks_startup() -> None:
    registry = RecordTypeRegistry()
    registry.register(_type(SampleV1))
    store = InMemoryRecordTypeStore(_persisted(registry, writer_unit="U-04"))
    with pytest.raises(RegistryStartupError, match="«writer_unit» cambió de 'U-04' a 'U-03'"):
        _run(registry.synchronize(store))


def test_persisted_schema_round_trips_through_json_and_resynchronizes() -> None:
    """La fila vuelve de ``jsonb`` con otro orden de claves: sigue siendo la misma versión."""
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    store = InMemoryRecordTypeStore()
    _run(registry.synchronize(store))

    reordered = {
        name: dataclasses.replace(row, content_schema=_reversed(row.content_schema))
        for name, row in _run(store.load()).items()
    }
    again = RecordTypeRegistry()
    register_u02_record_types(again)
    _run(again.synchronize(InMemoryRecordTypeStore(reordered)))
    assert again.sealed


def _reversed(document: Any) -> Any:
    if isinstance(document, Mapping):
        return {key: _reversed(document[key]) for key in reversed(list(document))}
    if isinstance(document, list):
        return [_reversed(item) for item in document]
    return document


# --- registro cerrado y declaración --------------------------------------------------------


def test_sealed_registry_rejects_new_types_and_unknown_types_are_reported() -> None:
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    _run(registry.synchronize(InMemoryRecordTypeStore()))
    with pytest.raises(RecordTypeRejected, match="está sellado"):
        registry.register(_type(SampleV1))
    with pytest.raises(RecordTypeUnknown, match="«finding_received» no está registrado"):
        registry.get("finding_received")


def test_u02_registers_its_fourteen_types_once() -> None:
    registry = RecordTypeRegistry()
    register_u02_record_types(registry)
    assert len(registry.record_types()) == len(U02_RECORD_TYPES) == 14
    assert {c.writer_unit for c in registry.latest()} == {ActorUnit.U02}
    with pytest.raises(RecordTypeRejected, match="no es mayor que la ya registrada"):
        register_u02_record_types(registry)


class LaxModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sample_id: UUID


class FreeTextModel(ContentModel):
    sample_id: UUID
    optional_id: UUID | None = None
    note: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    items: Annotated[tuple[Short, ...], Field(max_length=4)]


_RULE = LabelRule(
    subject_record_path="/sample_id",
    family_path="/sample_id",
    outcome_path="/sample_id",
    reason_category_path="/sample_id",
    labeled_by_path="/sample_id",
)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (_type(SampleV1, record_type="Sample"), "el nombre del tipo debe ser snake_case"),
        (_type(SampleV1, record_type="x" * 65), "el nombre del tipo debe ser snake_case"),
        (_type(SampleV1, schema_version=0), "schema_version debe ser un entero"),
        (_type(SampleV1, schema_version=True), "schema_version debe ser un entero"),
        (_type(LaxModel), "content_model debe ser estricto"),
        (_type(SampleV1, writer_unit="U-02"), "writer_unit debe ser U-02, U-03 o U-04"),
        (_type(SampleV1, chain_level="plant"), "chain_level debe ser plant u organization"),
        (_type(SampleV1, free_text_paths=("code",)), "ruta declarada mal formada: 'code'"),
        (_type(SampleV1, free_text_paths=("/code/",)), "ruta declarada mal formada"),
        (_type(SampleV1, outbox_events=("Bad-Event",)), "nombre de evento mal formado"),
        (_type(SampleV1, outbox_events=("a", "a")), "outbox_events tiene entradas repetidas"),
        (_type(FreeTextModel, free_text_paths=("/note", "/note")), "entradas repetidas"),
        (_type(FreeTextModel, free_text_paths=("/note", "/missing")), "/missing: ruta de free"),
        (
            _type(FreeTextModel, free_text_paths=("/note", "/sample_id")),
            "/sample_id: ruta de free_text_paths que no es una cadena de texto libre",
        ),
        (
            _type(FreeTextModel, free_text_paths=("/note",), evidence_paths=("/sample_id",)),
            "/sample_id: ruta de evidence_paths que no es una referencia de clip",
        ),
        (
            _type(FreeTextModel, free_text_paths=("/note",), source_key_path="/optional_id"),
            "/optional_id: source_key_path debe ser una cadena cerrada y obligatoria",
        ),
        (
            _type(FreeTextModel, free_text_paths=("/note",), source_key_path="/note"),
            "/note: source_key_path debe ser una cadena cerrada",
        ),
        (
            _type(FreeTextModel, free_text_paths=("/note",), source_key_path="/items[*]"),
            "/items[*]: source_key_path debe ser una cadena cerrada y obligatoria, no una lista",
        ),
        (
            _type(
                FreeTextModel,
                free_text_paths=("/note",),
                label_rule=dataclasses.replace(_RULE, reason_category_path="/note"),
            ),
            "/note: label_rule.reason_category_path apunta a texto libre",
        ),
        (
            _type(
                FreeTextModel,
                free_text_paths=("/note",),
                label_rule=dataclasses.replace(_RULE, family_path="/family"),
            ),
            "/family: label_rule.family_path no existe",
        ),
    ],
)
def test_malformed_declarations_are_rejected(definition: RecordType, expected: str) -> None:
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(definition)
    assert expected in str(raised.value)


def test_well_formed_declarations_are_accepted() -> None:
    compiled = RecordTypeRegistry().register(
        _type(
            FreeTextModel,
            free_text_paths=("/note",),
            source_key_path="/sample_id",
            label_rule=_RULE,
            outbox_events=("sample_recorded",),
        )
    )
    assert compiled.free_text_fields["/note"].max_length == 200
    assert compiled.to_persisted().label_rule == dict(_RULE.paths())


# --- esquema estricto (PAT-NUC-SEG-07) -----------------------------------------------------


class RecursiveNode(ContentModel):
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    child: RecursiveNode | None = None


class UnboundedMap(ContentModel):
    values: dict[str, Annotated[StrictInt, Field(ge=0, le=10)]]


class UnboundedList(ContentModel):
    codes: tuple[Short, ...]


class UnboundedNumber(ContentModel):
    level: StrictInt


class UnboundedString(ContentModel):
    code: Annotated[StrictStr, Field(pattern=r"^[a-z]+$")]


class AnyField(ContentModel):
    payload: Any


_MAP_KEYS: dict[str, Any] = {
    "propertyNames": {"pattern": r"^[a-z][a-z0-9_]{0,31}$", "maxLength": 32},
    "maxProperties": 16,
}


class BoundedMap(ContentModel):
    previous_values: Annotated[
        dict[str, Annotated[StrictStr, Field(min_length=1, max_length=80)]],
        Field(json_schema_extra=_MAP_KEYS),
    ]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (RecursiveNode, "anidamiento excesivo o esquema recursivo"),
        (UnboundedMap, "/values: objeto que admite propiedades adicionales"),
        (UnboundedList, "/codes: lista sin número máximo de elementos"),
        (UnboundedNumber, "/level: número sin mínimo"),
        (UnboundedString, "/code: cadena sin longitud máxima"),
        (AnyField, "/payload: campo sin tipo declarado"),
    ],
)
def test_loose_schemas_are_rejected(model: type[BaseModel], expected: str) -> None:
    with pytest.raises(RecordTypeRejected) as raised:
        RecordTypeRegistry().register(_type(model))
    assert expected in str(raised.value)


def test_bounded_map_with_closed_keys_is_accepted_and_its_values_are_free_text() -> None:
    with pytest.raises(RecordTypeRejected, match="/previous_values/\\*: texto libre fuera"):
        RecordTypeRegistry().register(_type(BoundedMap))
    compiled = RecordTypeRegistry().register(
        _type(BoundedMap, free_text_paths=("/previous_values/*",))
    )
    assert compiled.free_text_fields["/previous_values/*"].max_length == 80


# --- validador compilado -------------------------------------------------------------------


_VALID = b'{"sample_id":"0190a8a0-0000-7000-8000-000000000000","code":"abc","level":3,"kind":"a"}'


@pytest.mark.parametrize(
    "document",
    [
        _VALID.replace(b'"level":3', b'"level":"3"'),  # sin coerción
        _VALID.replace(b'"level":3', b'"level":3.0'),
        _VALID.replace(b'"level":3', b'"level":' + b"9" * 5000),  # entero enorme
        _VALID.replace(b'"level":3', b'"level":NaN'),
        _VALID.replace(b'"level":3', b'"level":Infinity'),
        _VALID.replace(b'"kind":"a"', b'"kind":"a","extra":1'),  # propiedad adicional
        _VALID.replace(b',"kind":"a"', b""),  # falta un campo
        _VALID.replace(b'"code":"abc"', b'"code":"' + b"a" * 1_000_000 + b'"'),
        b"[" * 100_000 + b"]" * 100_000,  # anidamiento profundo
        b"",
    ],
)
def test_compiled_validator_rejects_invalid_content_without_other_exceptions(
    document: bytes,
) -> None:
    compiled = RecordTypeRegistry().register(_type(SampleV1))
    with pytest.raises(ValidationError):
        compiled.validate_json(document)


def test_compiled_validator_accepts_valid_content() -> None:
    compiled = RecordTypeRegistry().register(_type(SampleV1))
    assert compiled.validate_json(_VALID).model_dump()["level"] == 3


# --- propiedad: ampliar nunca da problemas; estrechar siempre --------------------------------


@st.composite
def _field_specs(draw: st.DrawFn) -> dict[str, tuple[str, int, int]]:
    names = draw(
        st.lists(
            st.sampled_from(["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    specs: dict[str, tuple[str, int, int]] = {}
    for name in names:
        kind = draw(st.sampled_from(["string", "integer"]))
        low = draw(st.integers(min_value=0, max_value=50))
        high = draw(st.integers(min_value=max(low, 1), max_value=100))
        specs[name] = (kind, low, high)
    return specs


def _model(specs: Mapping[str, tuple[str, int, int]], optional: tuple[str, ...] = ()) -> Any:
    fields: dict[str, Any] = {}
    for name, (kind, low, high) in specs.items():
        if kind == "string":
            annotation: Any = Annotated[StrictStr, Field(min_length=low, max_length=high)]
        else:
            annotation = Annotated[StrictInt, Field(ge=low, le=high)]
        fields[name] = (annotation | None, None) if name in optional else (annotation, ...)
    return create_model("Generated", __base__=ContentModel, **fields)


@given(
    specs=_field_specs(),
    widen=st.integers(min_value=0, max_value=20),
    add_optional=st.booleans(),
)
def test_widening_never_reports_problems(
    specs: dict[str, tuple[str, int, int]], widen: int, add_optional: bool
) -> None:
    widened = {
        name: (kind, max(0, low - widen), high + widen) for name, (kind, low, high) in specs.items()
    }
    extra: tuple[str, ...] = ()
    if add_optional:
        widened["added_field"] = ("integer", 0, 10)
        extra = ("added_field",)
    old = _model(specs).model_json_schema()
    new = _model(widened, optional=extra).model_json_schema()
    assert compatibility_problems(old, new) == []


@given(
    specs=_field_specs(),
    data=st.data(),
    change=st.sampled_from(["remove", "raise_low", "lower_high"]),
)
def test_narrowing_always_reports_the_field(
    specs: dict[str, tuple[str, int, int]], data: st.DataObject, change: str
) -> None:
    name = data.draw(st.sampled_from(sorted(specs)))
    kind, low, high = specs[name]
    narrowed = dict(specs)
    if change == "remove":
        del narrowed[name]
    elif change == "raise_low":
        narrowed[name] = (kind, low + 1, max(high, low + 1))
    else:
        if high == 0 or high - 1 < low:
            narrowed[name] = (kind, low, high)
            del narrowed[name]
        else:
            narrowed[name] = (kind, low, high - 1)
    problems = compatibility_problems(
        _model(specs).model_json_schema(), _model(narrowed).model_json_schema()
    )
    assert any(problem.path == f"/{name}" for problem in problems)
