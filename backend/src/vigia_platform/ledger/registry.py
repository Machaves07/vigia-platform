"""Registro cerrado de tipos de registro del expediente (LC-NUC-08; BR-NUC-44, 51, 52).

El único lugar donde se decide qué puede escribirse en el expediente. Cada unidad (U-02, U-03,
U-04) registra al arrancar sus tipos con ``RecordTypeRegistry.register(RecordType)``:

- el **modelo de contenido** es un modelo Pydantic 2 estricto (``ContentModel`` o un modelo del
  contrato: ``extra="forbid"``, ``strict=True``); su validador se construye una sola vez por
  proceso y su JSON Schema es el ``content_schema`` que se persiste;
- la unidad escritora, la cadena, la clave de idempotencia (``source_key_path``), las rutas de
  texto libre y de evidencias, la regla de etiqueta y los eventos de la bandeja;
- al registrar se comprueban el esquema estricto, la **metapropiedad de privacidad** PR-NUC-16
  (ningún campo que identifique a una persona observada ni texto libre fuera de
  ``free_text_paths``) y la coherencia de cada ruta declarada. Cualquier fallo lanza
  ``RecordTypeRejected`` con el tipo y la ruta: la aplicación no arranca.

**Versiones** (BR-NUC-52): un tipo se registra con todas sus versiones en orden ascendente, para
que todo registro histórico pueda presentarse con el esquema de su ``schema_version``
(``get(tipo, schema_version=n)``). Cada versión debe ampliar a la anterior: retirar un campo,
estrechar un rango o una longitud, retirar un valor de una lista cerrada o añadir un campo
obligatorio impide arrancar. Cambiar el esquema exige subir ``schema_version``.

**Persistencia**: ``synchronize(store)`` contrasta lo registrado con la tabla global
``ledger.record_type`` (una fila por tipo con su última versión): un tipo o una versión que la
base conoce y el código ya no, un esquema que cambió sin subir la versión o una versión nueva
incompatible con la persistida lanzan ``RegistryStartupError``; si todo es coherente, guarda las
filas nuevas o ampliadas y **sella** el registro. Después de sellar no se registra nada más.

La validación de cada escritura y sus códigos de rechazo son del escritor (TASK-113).
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict
from vigia_contracts.models._base import ContractModel

from vigia_platform.ledger.free_text import FreeTextField
from vigia_platform.ledger.schema_rules import (
    FieldNode,
    SchemaProblem,
    compatibility_problems,
    is_free_text,
    normalized_schema,
    privacy_problems,
    resolve_path,
    structure_problems,
)
from vigia_platform.shared.context import ActorUnit

__all__ = [
    "ChainLevel",
    "CompiledType",
    "ContentModel",
    "InMemoryRecordTypeStore",
    "LabelRule",
    "PersistedRecordType",
    "RecordType",
    "RecordTypeRegistry",
    "RecordTypeRejected",
    "RecordTypeStore",
    "RecordTypeUnknown",
    "RegistryStartupError",
    "custom_json_schema_problems",
    "privacy_violations",
]

RECORD_TYPE_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""Nombre de tipo de registro y de evento: ``snake_case``, hasta 64 caracteres."""

CONTENT_PATH: Final = re.compile(r"^(?:/[a-z][a-z0-9_]{0,63}(?:\[\*\])?)+$")
"""Ruta declarada de contenido: puntero JSON con ``[*]`` para los elementos de una lista."""

MAX_PATHS: Final = 64
"""Tope de rutas declaradas por lista (``source_key_path`` cabe en 256 caracteres, tabla)."""

MAX_PATH_LENGTH: Final = 256


class ChainLevel(enum.StrEnum):
    """``chain_level``: la cadena en la que entra el registro (BR-NUC-45)."""

    PLANT = "plant"
    ORGANIZATION = "organization"


class ContentModel(BaseModel):
    """Base de los modelos de contenido de la plataforma: estricto, inmutable, sin extras."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


@dataclass(frozen=True, kw_only=True)
class LabelRule:
    """Cómo proyecta una ``Label`` un registro (domain-entities §3.7; U-04 §5.3).

    Cada campo es la ruta del contenido de la que sale el dato de la etiqueta; ninguna puede
    ser texto libre (una etiqueta nunca lo contiene). ``source_record_id`` es el propio registro
    y ``evidence_ids`` se resuelve por referencia: no son rutas.
    """

    subject_record_path: str
    family_path: str
    outcome_path: str
    reason_category_path: str
    labeled_by_path: str

    def paths(self) -> Mapping[str, str]:
        return {
            "subject_record_path": self.subject_record_path,
            "family_path": self.family_path,
            "outcome_path": self.outcome_path,
            "reason_category_path": self.reason_category_path,
            "labeled_by_path": self.labeled_by_path,
        }


@dataclass(frozen=True, kw_only=True)
class RecordType:
    """Declaración de un tipo de registro, tal como la hace su unidad al arrancar.

    ``chain_follows_scope``: para los tipos cuya cadena depende del alcance
    (``provider_concession_*``, ``provider_query``, ``checkpoint``): entran en la cadena de la
    planta si el alcance tiene planta y en la de ``chain_level`` si no.
    """

    record_type: str
    writer_unit: ActorUnit
    chain_level: ChainLevel
    schema_version: int
    content_model: type[BaseModel]
    source_key_path: str | None = None
    free_text_paths: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = ()
    label_rule: LabelRule | None = None
    outbox_events: tuple[str, ...] = ()
    chain_follows_scope: bool = False


@dataclass(frozen=True, kw_only=True)
class PersistedRecordType:
    """Una fila de la tabla global ``ledger.record_type`` (domain-entities §3.3 y §6)."""

    record_type: str
    writer_unit: str
    chain_level: str
    schema_version: int
    content_schema: Mapping[str, Any]
    source_key_path: str | None
    free_text_paths: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    label_rule: Mapping[str, str] | None
    outbox_events: tuple[str, ...]


@dataclass(frozen=True)
class CompiledType:
    """Un tipo registrado con su validador ya construido y su esquema derivado."""

    definition: RecordType
    content_schema: Mapping[str, Any]
    free_text_fields: Mapping[str, FreeTextField] = field(repr=False)

    @property
    def record_type(self) -> str:
        return self.definition.record_type

    @property
    def schema_version(self) -> int:
        return self.definition.schema_version

    @property
    def writer_unit(self) -> ActorUnit:
        return self.definition.writer_unit

    @property
    def chain_level(self) -> ChainLevel:
        return self.definition.chain_level

    def validate_json(self, document: bytes | str) -> BaseModel:
        """Valida un contenido JSON contra el esquema estricto (sin coerción ni extras).

        Lanza ``pydantic.ValidationError``; el escritor lo traduce a ``content_invalid`` con la
        ruta del primer campo que falla (TASK-113). Valida con el validador del núcleo, no con
        ``model_validate_json``: un modelo que redefiniera ese método no puede saltarse el esquema.
        """
        validated: BaseModel = self.definition.content_model.__pydantic_validator__.validate_json(
            document, strict=True
        )
        return validated

    def to_persisted(self) -> PersistedRecordType:
        definition = self.definition
        return PersistedRecordType(
            record_type=definition.record_type,
            writer_unit=definition.writer_unit.value,
            chain_level=definition.chain_level.value,
            schema_version=definition.schema_version,
            content_schema=self.content_schema,
            source_key_path=definition.source_key_path,
            free_text_paths=definition.free_text_paths,
            evidence_paths=definition.evidence_paths,
            label_rule=(
                None if definition.label_rule is None else dict(definition.label_rule.paths())
            ),
            outbox_events=definition.outbox_events,
        )


class RecordTypeRejected(Exception):
    """Un tipo que no puede registrarse; el mensaje nombra el tipo y cada ruta que falla."""

    def __init__(self, record_type: str, problems: Sequence[SchemaProblem | str]) -> None:
        self.record_type = record_type
        self.problems = tuple(str(problem) for problem in problems)
        detail = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            f"No se puede registrar el tipo de registro «{record_type}»; "
            f"la plataforma no arranca:\n{detail}"
        )


class RegistryStartupError(Exception):
    """Lo registrado es incompatible con la tabla ``ledger.record_type``: no se arranca."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        detail = "\n".join(f"  - {problem}" for problem in self.problems)
        super().__init__(
            "El registro de tipos es incompatible con el ya persistido; "
            f"la plataforma no arranca:\n{detail}"
        )


class RecordTypeUnknown(LookupError):
    """Tipo (o versión) no registrado: el escritor responde ``record_type_unknown``."""

    def __init__(self, record_type: str, schema_version: int | None = None) -> None:
        self.record_type = record_type
        self.schema_version = schema_version
        version = "" if schema_version is None else f" en su versión {schema_version}"
        super().__init__(f"El tipo de registro «{record_type}» no está registrado{version}")


class RecordTypeStore(Protocol):
    """Puerto de la tabla global ``ledger.record_type`` (sin datos de cliente, §6)."""

    async def load(self) -> Mapping[str, PersistedRecordType]:
        """Todas las filas, por nombre de tipo."""
        ...

    async def save(self, row: PersistedRecordType) -> None:
        """Inserta la fila o la sustituye por una versión mayor del mismo tipo."""
        ...


class InMemoryRecordTypeStore:
    """Adaptador en memoria del puerto (pruebas y arranque sin base)."""

    def __init__(self, rows: Mapping[str, PersistedRecordType] | None = None) -> None:
        self._rows: dict[str, PersistedRecordType] = dict(rows or {})

    async def load(self) -> Mapping[str, PersistedRecordType]:
        return dict(self._rows)

    async def save(self, row: PersistedRecordType) -> None:
        current = self._rows.get(row.record_type)
        if current is not None and current.schema_version >= row.schema_version:
            raise ValueError("solo se guarda una versión mayor que la persistida")
        self._rows[row.record_type] = row


def _json_copy(document: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = json.loads(json.dumps(document, allow_nan=False))
    return copied


def privacy_violations(types: Iterator[CompiledType] | Sequence[CompiledType]) -> list[str]:
    """Metapropiedad PR-NUC-16 sobre tipos ya compilados: ``tipo ruta: motivo`` por fallo."""
    return [
        f"{compiled.record_type} v{compiled.schema_version} {problem}"
        for compiled in types
        for problem in privacy_problems(
            compiled.content_schema, compiled.definition.free_text_paths
        )
    ]


class RecordTypeRegistry:
    """El registro cerrado de tipos de un proceso (``vigia-api`` o ``vigia-worker``)."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[int, CompiledType]] = {}
        self._sealed = False

    # --- registro ---------------------------------------------------------------------------

    def register(self, definition: RecordType) -> CompiledType:
        """Compila y registra una versión de un tipo; ``RecordTypeRejected`` si no cumple."""
        if self._sealed:
            raise RecordTypeRejected(
                getattr(definition, "record_type", "?"),
                ["el registro de tipos está sellado: los tipos se registran al arrancar"],
            )
        name = definition.record_type
        problems = _declaration_problems(definition)
        model: object = definition.content_model
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise RecordTypeRejected(name, problems)
        # Con la declaración mal formada se sigue examinando el esquema: el mensaje debe nombrar
        # también cualquier campo prohibido (fallo cerrado, con el motivo completo).
        schema = _json_copy(model.model_json_schema(mode="validation"))
        problems.extend(structure_problems(schema))
        problems.extend(privacy_problems(schema, definition.free_text_paths))
        problems.extend(_path_problems(definition, schema))
        previous = self._latest(name)
        if previous is not None:
            problems.extend(_version_problems(previous, definition, schema))
        elif definition.schema_version != 1:
            problems.append(
                f"la primera versión que se registra es la 1, no la {definition.schema_version}: "
                "las versiones se registran todas, en orden y sin huecos (BR-NUC-52)"
            )
        if problems:
            raise RecordTypeRejected(name, problems)
        compiled = CompiledType(definition, schema, _free_text_fields(name, definition, schema))
        self._versions.setdefault(name, {})[definition.schema_version] = compiled
        return compiled

    def seal(self) -> None:
        """Cierra el registro; ``synchronize`` lo hace al terminar bien."""
        self._sealed = True

    @property
    def sealed(self) -> bool:
        return self._sealed

    # --- consulta ---------------------------------------------------------------------------

    def get(self, record_type: str, *, schema_version: int | None = None) -> CompiledType:
        """El tipo compilado (su última versión, o la pedida); ``RecordTypeUnknown`` si no."""
        versions = self._versions.get(record_type)
        if not versions:
            raise RecordTypeUnknown(record_type)
        if schema_version is None:
            return versions[max(versions)]
        compiled = versions.get(schema_version)
        if compiled is None:
            raise RecordTypeUnknown(record_type, schema_version)
        return compiled

    def record_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._versions))

    def latest(self) -> tuple[CompiledType, ...]:
        """La última versión de cada tipo registrado."""
        return tuple(self.get(name) for name in self.record_types())

    def all_versions(self) -> tuple[CompiledType, ...]:
        return tuple(
            self._versions[name][version]
            for name in self.record_types()
            for version in sorted(self._versions[name])
        )

    def _latest(self, record_type: str) -> CompiledType | None:
        versions = self._versions.get(record_type)
        return versions[max(versions)] if versions else None

    # --- persistencia -----------------------------------------------------------------------

    async def synchronize(self, store: RecordTypeStore) -> None:
        """Contrasta con ``ledger.record_type``, guarda lo nuevo y sella el registro."""
        persisted = await store.load()
        problems: list[str] = []
        for name in sorted(persisted):
            problems.extend(self._persisted_problems(persisted[name]))
        if problems:
            raise RegistryStartupError(problems)
        for compiled in self.latest():
            row = persisted.get(compiled.record_type)
            if row is None or row.schema_version < compiled.schema_version:
                await store.save(compiled.to_persisted())
        self.seal()

    def _persisted_problems(self, row: PersistedRecordType) -> list[str]:
        name = row.record_type
        latest = self._latest(name)
        if latest is None:
            return [
                f"{name}: el tipo está en la base (versión {row.schema_version}) y ninguna "
                "unidad lo registra; retirar un tipo está prohibido (BR-NUC-52)"
            ]
        if row.schema_version > latest.schema_version:
            return [
                f"{name}: la base tiene la versión {row.schema_version} y el código solo llega "
                f"a la {latest.schema_version}; retirar una versión está prohibido (BR-NUC-52)"
            ]
        problems: list[str] = []
        current = latest.to_persisted()
        for attribute in ("writer_unit", "chain_level", "source_key_path"):
            if getattr(row, attribute) != getattr(current, attribute):
                problems.append(
                    f"{name}: «{attribute}» cambió de {getattr(row, attribute)!r} a "
                    f"{getattr(current, attribute)!r}; no puede cambiar entre versiones"
                )
        same_version = self._versions[name].get(row.schema_version)
        if same_version is None:
            problems.append(
                f"{name}: la versión {row.schema_version} está en la base y el código no la "
                "registra; sin ella los registros históricos no pueden presentarse (BR-NUC-52)"
            )
        elif normalized_schema(same_version.content_schema) != normalized_schema(
            row.content_schema
        ):
            problems.append(
                f"{name}: el esquema de la versión {row.schema_version} cambió sin subir "
                "schema_version"
            )
        if row.schema_version == latest.schema_version and _declared(row) != _declared(current):
            problems.append(
                f"{name}: las rutas, la regla de etiqueta o los eventos de la versión "
                f"{row.schema_version} cambiaron sin subir schema_version"
            )
        problems.extend(
            f"{name} v{row.schema_version} → v{latest.schema_version} {problem}"
            for problem in compatibility_problems(row.content_schema, latest.content_schema)
        )
        return problems


def _declared(row: PersistedRecordType) -> tuple[Any, ...]:
    return (
        row.free_text_paths,
        row.evidence_paths,
        None if row.label_rule is None else sorted(row.label_rule.items()),
        row.outbox_events,
    )


def _declaration_problems(definition: RecordType) -> list[SchemaProblem | str]:
    """Comprobaciones de la declaración que no dependen del esquema."""
    problems: list[SchemaProblem | str] = []
    # Las unidades construyen la declaración en Python sin validación de tipos: se comprueba
    # en tiempo de ejecución lo que el tipado estático ya promete.
    name: object = definition.record_type
    if not isinstance(name, str) or not RECORD_TYPE_NAME.match(name):
        problems.append("el nombre del tipo debe ser snake_case de 1 a 64 caracteres")
    writer_unit: object = definition.writer_unit
    if not isinstance(writer_unit, ActorUnit):
        problems.append("writer_unit debe ser U-02, U-03 o U-04")
    chain_level: object = definition.chain_level
    if not isinstance(chain_level, ChainLevel):
        problems.append("chain_level debe ser plant u organization")
    version: object = definition.schema_version
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 2**31 - 1:
        problems.append("schema_version debe ser un entero mayor o igual que 1")
    model: object = definition.content_model
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        problems.append("content_model debe ser un modelo Pydantic")
        return problems
    config = model.model_config
    if config.get("extra") != "forbid" or config.get("strict") is not True:
        problems.append(
            "content_model debe ser estricto: extra='forbid' y strict=True (PAT-NUC-SEG-07)"
        )
    problems.extend(custom_json_schema_problems(model))
    declared = [
        *definition.free_text_paths,
        *definition.evidence_paths,
        *([definition.source_key_path] if definition.source_key_path is not None else []),
        *(definition.label_rule.paths().values() if definition.label_rule is not None else []),
    ]
    for path in declared:
        if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH or not CONTENT_PATH.match(path):
            problems.append(f"ruta declarada mal formada: {path!r}")
    for label, paths in (
        ("free_text_paths", definition.free_text_paths),
        ("evidence_paths", definition.evidence_paths),
        ("outbox_events", definition.outbox_events),
    ):
        if len(paths) > MAX_PATHS:
            problems.append(f"{label} admite como mucho {MAX_PATHS} entradas")
        if len(set(paths)) != len(paths):
            problems.append(f"{label} tiene entradas repetidas")
    for event in definition.outbox_events:
        if not isinstance(event, str) or not RECORD_TYPE_NAME.match(event):
            problems.append(f"nombre de evento mal formado: {event!r}")
    return problems


_JS_HOOKS: Final = (
    "pydantic_js_functions",
    "pydantic_js_annotation_functions",
    "pydantic_js_extra",
    "pydantic_js_updates",
)
_HARMLESS_UPDATES: Final = frozenset({"title", "description", "examples"})
_DEFAULT_MODEL_HOOK: Final = BaseModel.__get_pydantic_json_schema__.__func__  # type: ignore[attr-defined]
_CONTRACT_RULES: Final = ContractModel.check_contract_rules.__func__


def _is_builtin_hook(hook: object) -> bool:
    """El gancho estándar de Pydantic: el de ``BaseModel`` sin redefinir o uno interno."""
    if getattr(hook, "__func__", None) is _DEFAULT_MODEL_HOOK:
        return True
    module = getattr(hook, "__module__", "") or ""
    return module.startswith("pydantic._internal.")


_REPLACING_VALIDATORS: Final = frozenset({"function-before", "function-wrap", "function-plain"})
"""Validadores de función que ven la entrada antes (o en lugar) del esquema: pueden aceptar lo
que el esquema rechaza. ``function-after`` recibe el valor ya validado y se admite."""
_SPLIT_SCHEMAS: Final = frozenset({"lax-or-strict", "json-or-python"})
"""Esquemas del núcleo con dos ramas: el JSON Schema describe una y se valida con la otra (la
estricta, o la de Python). Ningún modelo del contrato ni de U-02 los usa."""


def _is_contract_rules(function: object) -> bool:
    """El único validador envolvente admitido: el de ``ContractModel``, que llama a ``handler`` y
    devuelve su resultado (capa residual del esquema del contrato). Se identifica por la función."""
    return getattr(function, "__func__", None) is _CONTRACT_RULES


def custom_json_schema_problems(model: type[BaseModel]) -> list[str]:
    """El JSON Schema que se comprueba y se persiste debe describir lo que se valida.

    ``WithJsonSchema``, ``json_schema_extra``, ``Base64Str`` o un ``__get_pydantic_json_schema__``
    propio cambian el esquema sin cambiar lo que se valida; un validador ``before``, ``wrap`` o
    ``plain`` (de campo, de modelo o de un ``__get_pydantic_core_schema__`` propio), o un esquema
    de dos ramas (``lax_or_strict``, ``json_or_python``), cambia lo que se valida sin cambiar el
    esquema. En los dos casos la metapropiedad miraría un esquema y el
    validador aceptaría otra cosa. Se buscan en el esquema del núcleo, que es lo que valida.
    """
    found: set[str] = set()
    replacing: set[str] = set()
    split: set[str] = set()

    def visit(node: object, path: str) -> None:
        if isinstance(node, Mapping):
            if node.get("type") in _REPLACING_VALIDATORS:
                function = node.get("function")
                target = function.get("function") if isinstance(function, Mapping) else None
                if not (node.get("type") == "function-wrap" and _is_contract_rules(target)):
                    replacing.add(path or "/")
            if node.get("type") in _SPLIT_SCHEMAS:
                split.add(path or "/")
            owner = node.get("cls")
            if (
                node.get("type") == "model"
                and isinstance(owner, type)
                and issubclass(owner, BaseModel)
                and owner.model_config.get("json_schema_extra")
            ):
                found.add(path or "/")
            metadata = node.get("metadata")
            if isinstance(metadata, Mapping):
                for key in _JS_HOOKS:
                    value = metadata.get(key)
                    if not value:
                        continue
                    if key == "pydantic_js_functions" and all(map(_is_builtin_hook, value)):
                        continue
                    if key == "pydantic_js_updates" and set(value) <= _HARMLESS_UPDATES:
                        continue
                    found.add(path or "/")
            for key, value in node.items():
                if key == "metadata":
                    continue
                if key == "fields" and isinstance(value, Mapping):
                    for field_name, field_schema in value.items():
                        visit(field_schema, f"{path}/{field_name}")
                else:
                    visit(value, path)
        elif isinstance(node, list | tuple):
            for item in node:
                visit(item, path)

    visit(model.__pydantic_core_schema__, "")
    return (
        [
            f"{path}: JSON Schema personalizado (WithJsonSchema, json_schema_extra, Base64 o "
            "__get_pydantic_json_schema__); el esquema comprobado no describiría lo que se valida"
            for path in sorted(found)
        ]
        + [
            f"{path}: validador de función before, wrap o plain; lo validado no sería lo que "
            "describe el esquema comprobado (solo se admiten validadores after)"
            for path in sorted(replacing)
        ]
        + [
            f"{path}: esquema con dos ramas (lax-or-strict o json-or-python); el esquema "
            "comprobado describiría una rama y se validaría con la otra"
            for path in sorted(split)
        ]
    )


def _single(nodes: list[FieldNode]) -> list[FieldNode]:
    """Las alternativas de una ruta sin la de ``null`` (un campo opcional)."""
    return [node for node in nodes if node.schema.get("type") != "null"]


def _path_problems(definition: RecordType, schema: Mapping[str, Any]) -> list[SchemaProblem]:
    """Cada ruta declarada existe y tiene la forma que su papel exige."""
    problems: list[SchemaProblem] = []
    for path in definition.free_text_paths:
        nodes = _single(resolve_path(schema, path))
        if not nodes:
            problems.append(SchemaProblem(path, "ruta de free_text_paths que no existe"))
        elif not all(is_free_text(node.schema) for node in nodes):
            problems.append(
                SchemaProblem(path, "ruta de free_text_paths que no es una cadena de texto libre")
            )
    for path in definition.evidence_paths:
        nodes = _single(resolve_path(schema, path))
        if not nodes:
            problems.append(SchemaProblem(path, "ruta de evidence_paths que no existe"))
        elif not all(node.schema.get("type") == "object" for node in nodes):
            problems.append(
                SchemaProblem(path, "ruta de evidence_paths que no es una referencia de clip")
            )
    if definition.source_key_path is not None:
        path = definition.source_key_path
        nodes = resolve_path(schema, path)
        if not nodes:
            problems.append(SchemaProblem(path, "source_key_path no existe"))
        elif "[*]" in path or not all(
            node.required and node.schema.get("type") == "string" and not is_free_text(node.schema)
            for node in nodes
        ):
            problems.append(
                SchemaProblem(
                    path, "source_key_path debe ser una cadena cerrada y obligatoria, no una lista"
                )
            )
    if definition.label_rule is not None:
        for role, path in definition.label_rule.paths().items():
            nodes = _single(resolve_path(schema, path))
            if not nodes:
                problems.append(SchemaProblem(path, f"label_rule.{role} no existe"))
            elif any(is_free_text(node.schema) for node in nodes):
                problems.append(SchemaProblem(path, f"label_rule.{role} apunta a texto libre"))
    return problems


def _version_problems(
    previous: CompiledType, definition: RecordType, schema: Mapping[str, Any]
) -> list[SchemaProblem | str]:
    """Una versión nueva de un tipo ya registrado en este proceso."""
    problems: list[SchemaProblem | str] = []
    expected = previous.schema_version + 1
    if definition.schema_version != expected:
        problems.append(
            f"se esperaba la versión {expected} y llegó la {definition.schema_version}: las "
            "versiones se registran todas, en orden y sin huecos (BR-NUC-52)"
        )
        return problems
    for attribute in ("writer_unit", "chain_level", "source_key_path", "chain_follows_scope"):
        before = getattr(previous.definition, attribute)
        after = getattr(definition, attribute)
        if before != after:
            problems.append(
                f"«{attribute}» cambió de {before!r} a {after!r}; no puede cambiar entre versiones"
            )
    problems.extend(
        SchemaProblem(
            problem.path,
            f"{problem.message} (v{previous.schema_version} → v{definition.schema_version})",
        )
        for problem in compatibility_problems(previous.content_schema, schema)
    )
    return problems


def _free_text_fields(
    name: str, definition: RecordType, schema: Mapping[str, Any]
) -> Mapping[str, FreeTextField]:
    fields: dict[str, FreeTextField] = {}
    for path in definition.free_text_paths:
        nodes = _single(resolve_path(schema, path))
        fields[path] = FreeTextField(
            record_type=name,
            path=path,
            min_length=max(int(node.schema.get("minLength", 0)) for node in nodes),
            max_length=min(int(node.schema["maxLength"]) for node in nodes),
        )
    return fields
