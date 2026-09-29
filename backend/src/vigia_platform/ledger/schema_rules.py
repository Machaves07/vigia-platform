"""Reglas sobre los esquemas de contenido de los tipos de registro (LC-NUC-08, PAT-NUC-SEG-07).

``ledger.registry`` las aplica a cada ``RecordType`` al registrarlo y al contrastarlo con la
versión ya persistida; ningún tipo que las incumpla llega a registrarse, así que la aplicación
no arranca (BR-NUC-44, 51, 52). Trabajan sobre el JSON Schema que Pydantic deriva del modelo
de contenido, resolviendo las referencias locales ``#/$defs/...``.

Rutas de contenido: puntero JSON con ``[*]`` para los elementos de una lista, como en
``domain-entities.md`` de U-04 (``/why_steps[*]/statement``).

- ``structure_problems``: esquema estricto de verdad: todo objeto con ``additionalProperties:
  false`` (sin mapas), toda cadena con longitud máxima o lista cerrada, ninguna cadena binaria,
  ninguna cadena cerrada de más de 1024 caracteres, toda lista con tope de elementos, todo
  número con mínimo y máximo, nombres de campo en ``snake_case``.
- ``privacy_problems`` (metapropiedad PR-NUC-16, parte de esquemas): ningún campo con nombre de
  la lista prohibida (nombre de persona, documento de identidad, empleado, ``track_id``, rostro,
  apariencia, contacto, imagen) y ningún texto libre fuera de ``free_text_paths``. Es texto
  libre toda cadena sin lista cerrada ni patrón cerrado (``is_free_text``); ``format`` no
  cierra nada.
- ``compatibility_problems``: una versión nueva solo amplía a la anterior (BR-NUC-52): no retira
  campos, no estrecha rangos, longitudes ni listas cerradas y no añade campos obligatorios.

Todos los mensajes van en español: son los que ve quien arranca la plataforma.
"""

from __future__ import annotations

import itertools
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "FORBIDDEN_NAME_PAIRS",
    "FORBIDDEN_NAME_STEMS",
    "FORBIDDEN_NAME_TOKENS",
    "FieldNode",
    "SchemaProblem",
    "compatibility_problems",
    "field_nodes",
    "forbidden_name_reason",
    "is_free_text",
    "normalized_schema",
    "privacy_problems",
    "resolve_path",
    "structure_problems",
]

JsonSchema = Mapping[str, Any]

FIELD_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""Nombre de campo admitido en un contenido: ``snake_case`` en inglés, como el contrato."""

_REF_PREFIX: Final = "#/$defs/"
_MAX_DEPTH: Final = 32
"""Profundidad máxima de anidamiento que se recorre; más allá, el esquema se rechaza."""

_ANNOTATIONS: Final = frozenset({"title", "description", "examples", "$comment"})
"""Palabras que no cambian lo que el esquema admite: se ignoran al comparar versiones."""

_HANDLED: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "maxProperties",
        "minProperties",
        "items",
        "maxItems",
        "minItems",
        "uniqueItems",
        "maxLength",
        "minLength",
        "pattern",
        "format",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "anyOf",
        "oneOf",
        "$ref",
        "$defs",
        "default",
        *_ANNOTATIONS,
    }
)
"""Palabras que estas reglas entienden; cualquier otra cambiada entre versiones es un cambio
que no se puede demostrar compatible y se rechaza (fallo cerrado)."""

FORBIDDEN_NAME_TOKENS: Final = frozenset(
    {
        # la persona observada y su relación laboral
        "person",
        "persons",
        "persona",
        "personas",
        "people",
        "employee",
        "employees",
        "empleado",
        "empleados",
        "trabajador",
        "trabajadores",
        # documentos de identidad
        "cedula",
        "dni",
        "passport",
        "pasaporte",
        # seguimiento
        "track",
        "tracks",
        "tracking",
        "tracklet",
        "reid",
        # rostro
        "face",
        "faces",
        "facial",
        "rostro",
        "rostros",
        # apariencia y rasgos
        "appearance",
        "apariencia",
        "clothing",
        "ropa",
        "biometric",
        "biometrics",
        "gait",
        "tattoo",
        # nombres de persona
        "surname",
        "surnames",
        "apellido",
        "apellidos",
        "nombre",
        "nombres",
        "documento",
        "documentos",
        # la persona como trabajador
        "worker",
        "workers",
        "operario",
        "operarios",
        "badge",
        # contacto
        "email",
        "correo",
        "phone",
        "telefono",
        "mobile",
        # imágenes y rasgos
        "photo",
        "photos",
        "foto",
        "fotos",
        "snapshot",
        "thumbnail",
        "embedding",
        "embeddings",
        "iris",
        "voice",
        "voz",
        # vehículo
        "plate",
        "placa",
    }
)
"""Palabras de un nombre de campo que identifican a una persona observada (BR-NUC-51).

Quedan fuera a propósito, porque nombran entidades o usuarios y no personas observadas:
``name`` (de organización, planta o zona), ``display_name`` (instantánea del usuario que firma),
``document_ref`` y ``document_kind`` (documentos de gobierno de U-03), ``image_digest``
(imagen de software) y ``hardware_fingerprint`` (huella del equipo, ver
``_DEVICE_QUALIFIERS``). Un campo ``const`` (``workers_role: "copasst"``) no se examina."""

_DEVICE_QUALIFIERS: Final = frozenset({"hardware", "device", "certificate", "key", "public"})
"""``fingerprint`` solo se admite tras una de estas palabras: huella de equipo o de clave."""

FORBIDDEN_NAME_PAIRS: Final = frozenset(
    {
        ("first", "name"),
        ("last", "name"),
        ("full", "name"),
        ("given", "name"),
        ("family", "name"),
        ("middle", "name"),
        ("nombre", "completo"),
        ("document", "number"),
        ("identity", "document"),
        ("id", "document"),
        ("national", "id"),
        ("numero", "documento"),
        ("re", "id"),
        ("id", "number"),
        ("license", "plate"),
    }
)
"""Pares de palabras consecutivas que nombran un dato de identidad de persona."""

FORBIDDEN_NAME_STEMS: Final = (
    "person",
    "employee",
    "empleado",
    "trabajador",
    "cedula",
    "passport",
    "pasaporte",
    "trackid",
    "rostro",
    "appearance",
    "apariencia",
    "biometric",
    "firstname",
    "lastname",
    "fullname",
    "surname",
    "apellido",
)
"""Raíces que se buscan también dentro de nombres sin separador (``personname``, ``trackid``).
``face`` no está aquí: aparece en palabras legítimas como ``interface``."""


@dataclass(frozen=True)
class SchemaProblem:
    """Un incumplimiento en una ruta del contenido (``""`` es la raíz)."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path or '/'}: {self.message}"


@dataclass(frozen=True)
class FieldNode:
    """Un nodo alcanzable del esquema: su ruta, el nombre del campo y el subesquema resuelto."""

    path: str
    name: str | None
    schema: JsonSchema
    required: bool
    """Si el campo es obligatorio en su objeto y en todos sus antecesores."""


class _Resolver:
    """Resuelve ``$ref`` locales y aplana ``anyOf``/``oneOf`` en alternativas."""

    def __init__(self, root: JsonSchema) -> None:
        defs = root.get("$defs", {})
        self._defs: Mapping[str, Any] = defs if isinstance(defs, Mapping) else {}

    def resolve(self, node: Any, problems: list[SchemaProblem], path: str) -> JsonSchema | None:
        seen: set[str] = set()
        while isinstance(node, Mapping) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
                problems.append(SchemaProblem(path, f"referencia no admitida: {ref!r}"))
                return None
            name = ref[len(_REF_PREFIX) :]
            if name in seen or name not in self._defs:
                problems.append(SchemaProblem(path, f"referencia sin resolver: {ref!r}"))
                return None
            seen.add(name)
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            target = self._defs[name]
            node = {**target, **siblings} if isinstance(target, Mapping) else target
        if not isinstance(node, Mapping):
            problems.append(SchemaProblem(path, "subesquema que no es un objeto"))
            return None
        return node

    def alternatives(self, node: Any, problems: list[SchemaProblem], path: str) -> list[JsonSchema]:
        resolved = self.resolve(node, problems, path)
        if resolved is None:
            return []
        for keyword in ("anyOf", "oneOf"):
            if keyword in resolved:
                branches = resolved[keyword]
                if not isinstance(branches, Sequence) or isinstance(branches, str):
                    problems.append(SchemaProblem(path, f"«{keyword}» mal formado"))
                    return []
                shared = {k: v for k, v in resolved.items() if k != keyword}
                flat: list[JsonSchema] = []
                for branch in branches:
                    for alternative in self.alternatives(branch, problems, path):
                        flat.append({**shared, **alternative})
                return flat
        return [resolved]


def _kind(node: JsonSchema) -> str:
    """Tipo JSON de una alternativa: el declarado o, sin él, el de su ``const`` o ``enum``."""
    declared = node.get("type")
    if isinstance(declared, str):
        return declared
    values = _closed_values(node)
    if values:
        kinds = {_json_kind(value) for value in values}
        if len(kinds) == 1:
            return kinds.pop()
    return "any"


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _closed_values(node: JsonSchema) -> list[Any] | None:
    """Valores de la lista cerrada (``enum`` o ``const``), o ``None`` si no la hay."""
    if "const" in node:
        return [node["const"]]
    values = node.get("enum")
    if isinstance(values, Sequence) and not isinstance(values, str):
        return list(values)
    return None


def _escape(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def field_nodes(schema: JsonSchema) -> tuple[list[FieldNode], list[SchemaProblem]]:
    """Recorre el esquema y devuelve cada alternativa alcanzable con su ruta de contenido."""
    resolver = _Resolver(schema)
    nodes: list[FieldNode] = []
    problems: list[SchemaProblem] = []

    def visit(node: Any, path: str, name: str | None, required: bool, depth: int) -> None:
        if depth > _MAX_DEPTH:
            problems.append(SchemaProblem(path, "anidamiento excesivo o esquema recursivo"))
            return
        for alternative in resolver.alternatives(node, problems, path):
            nodes.append(FieldNode(path, name, alternative, required))
            kind = _kind(alternative)
            if kind == "object":
                properties = alternative.get("properties", {})
                mandatory = alternative.get("required", [])
                if isinstance(properties, Mapping):
                    for child, child_schema in properties.items():
                        visit(
                            child_schema,
                            f"{path}/{_escape(str(child))}",
                            str(child),
                            required and child in mandatory,
                            depth + 1,
                        )
            elif kind == "array" and "items" in alternative:
                visit(alternative["items"], f"{path}[*]", None, False, depth + 1)

    visit(schema, "", None, True, 0)
    return nodes, problems


_DIGITS: Final = frozenset("0123456789")
_UPPER: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_LOWER: Final = frozenset("abcdefghijklmnopqrstuvwxyz")
_LITERAL_ESCAPES: Final = frozenset(".-/+\\{}()[]*?|^$_:")
"""Escapes que solo significan su propio carácter; ``\\d`` es el único escape de clase admitido."""
_OPERATORS: Final = frozenset("()|?*+")
_PRINTABLE_ASCII: Final = frozenset(chr(code) for code in range(0x21, 0x7F))
"""ASCII visible sin espacio: lo único que un patrón cerrado puede contener."""
_MARKUP_CHARACTERS: Final = frozenset("<>&")
"""Un patrón que admite estos caracteres podría llevar marcado: se trata como texto libre."""
MAX_CLOSED_LENGTH: Final = 1024
"""Tope de una cadena cerrada no declarada: no cabe una imagen en base64."""
_BINARY_FORMATS: Final = frozenset({"binary", "base64", "byte"})


def _admitted_characters(pattern: str) -> frozenset[str] | None:
    """Caracteres que admite un patrón cerrado, o ``None`` si el patrón es abierto.

    Lista blanca, porque Pydantic valida con el motor de Rust y cualquier sintaxis que el
    tokenizador no entienda (clases POSIX ``[[:print:]]``, ``\\x20``, ``\\u0020``, ``\\t``, ``\\w``,
    ``\\s``, ``\\p{…}``, banderas ``(?i)`` o ``(?x)``, clases negadas, ``.``) cuenta como abierta.
    Solo se admite ASCII visible, anclado con ``^…$``.
    """
    if len(pattern) < 2 or pattern[0] != "^" or pattern[-1] != "$" or pattern.endswith("\\$"):
        return None
    body = pattern[1:-1]
    if any(char not in _PRINTABLE_ASCII for char in body) or "[:" in body or "(?" in body:
        return None
    admitted: set[str] = set()
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            escaped = body[index + 1 : index + 2]
            if escaped == "d":
                admitted |= _DIGITS
            elif escaped in _LITERAL_ESCAPES:
                admitted.add(escaped)
            else:
                return None
            index += 2
        elif char == "[":
            end = _class_members(body, index + 1, admitted)
            if end is None:
                return None
            index = end + 1
        elif char == "{":
            close = body.find("}", index)
            if close == -1 or not re.fullmatch(r"[0-9]+(,[0-9]*)?", body[index + 1 : close]):
                return None
            index = close + 1
        elif char in "^$.]}":
            return None
        else:
            if char not in _OPERATORS:
                admitted.add(char)
            index += 1
    return frozenset(admitted)


def _class_members(body: str, start: int, admitted: set[str]) -> int | None:
    """Añade a ``admitted`` los miembros de la clase que empieza en ``start``; devuelve su ``]``."""
    if body[start : start + 1] == "^":
        return None
    # Cada miembro es un carácter; ``None`` es un guion sin escapar y "" es ``\d`` (ya sumado),
    # que no puede ser extremo de un rango.
    tokens: list[str | None] = []
    index = start
    while index < len(body) and body[index] != "]":
        char = body[index]
        if char == "\\":
            escaped = body[index + 1 : index + 2]
            if escaped == "d":
                admitted |= _DIGITS
                tokens.append("")
            elif escaped in _LITERAL_ESCAPES:
                tokens.append(escaped)
            else:
                return None
            index += 2
        elif char == "[":
            return None
        else:
            tokens.append(None if char == "-" else char)
            index += 1
    if index >= len(body) or not tokens:
        return None
    position = 0
    while position < len(tokens):
        low = tokens[position]
        high = tokens[position + 2] if position + 2 < len(tokens) else None
        if low and high and tokens[position + 1] is None:
            if ord(low) > ord(high):
                return None
            admitted |= {chr(code) for code in range(ord(low), ord(high) + 1)}
            position += 3
            continue
        if low is None:
            admitted.add("-")
        elif low:
            admitted.add(low)
        position += 1
    return index


def _pattern_is_closed(pattern: str, *, fixed_length: bool) -> bool:
    """Un patrón es cerrado si no admite espacio ni nada fuera del ASCII visible y, salvo que la
    longitud sea fija (una firma o una clave en base64), no mezcla mayúsculas y minúsculas: así
    ``^[A-Za-z]{1,64}$`` o ``^[A-Z][a-z]+$``, que admiten un nombre, siguen siendo texto libre."""
    admitted = _admitted_characters(pattern)
    if admitted is None or admitted & _MARKUP_CHARACTERS:
        return False
    return fixed_length or not (admitted & _UPPER and admitted & _LOWER)


def is_free_text(node: JsonSchema) -> bool:
    """Una cadena sin lista cerrada ni patrón cerrado es texto libre.

    ``format`` no cierra nada: Pydantic no impone los formatos que no conoce (fallo cerrado).
    """
    if _kind(node) != "string" or _closed_values(node) is not None:
        return False
    pattern = node.get("pattern")
    if not isinstance(pattern, str):
        return True
    fixed = node.get("minLength") == node.get("maxLength") and "maxLength" in node
    return not _pattern_is_closed(pattern, fixed_length=fixed)


def _fold(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def forbidden_name_reason(name: str) -> str | None:
    """Motivo por el que un nombre de campo identificaría a una persona, o ``None``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    folded = _fold(spaced)
    tokens = [token for token in re.split(r"[^a-z0-9]+", folded) if token]
    for position, token in enumerate(tokens):
        if token in FORBIDDEN_NAME_TOKENS:
            return f"la palabra «{token}» identifica a una persona observada"
        if token in {"fingerprint", "fingerprints", "huella"} and (
            position == 0 or tokens[position - 1] not in _DEVICE_QUALIFIERS
        ):
            return f"«{token}» sin calificar de equipo o de clave identifica a una persona"
    for pair in itertools.pairwise(tokens):
        if pair in FORBIDDEN_NAME_PAIRS:
            return f"«{'_'.join(pair)}» identifica a una persona observada"
    joined = "".join(tokens)
    for stem in FORBIDDEN_NAME_STEMS:
        if stem in joined:
            return f"contiene «{stem}», que identifica a una persona observada"
    return None


def structure_problems(schema: JsonSchema) -> list[SchemaProblem]:
    """Incumplimientos del esquema estricto (PAT-NUC-SEG-07)."""
    nodes, problems = field_nodes(schema)
    if not any(node.path == "" and _kind(node.schema) == "object" for node in nodes):
        problems.append(SchemaProblem("", "el contenido debe ser un objeto"))
    for node in nodes:
        problems.extend(_node_structure(node))
    return problems


def _node_structure(node: FieldNode) -> list[SchemaProblem]:
    schema, path = node.schema, node.path
    found: list[SchemaProblem] = []
    if node.name is not None and not FIELD_NAME.match(node.name):
        found.append(SchemaProblem(path, "nombre de campo fuera de snake_case en inglés"))
    kind = _kind(schema)
    closed = _closed_values(schema) is not None
    if kind == "any":
        found.append(SchemaProblem(path, "campo sin tipo declarado"))
    elif kind == "object":
        if schema.get("additionalProperties", True) is not False or "patternProperties" in schema:
            found.append(
                SchemaProblem(
                    path,
                    "objeto que admite propiedades adicionales o un mapa (exige "
                    "additionalProperties: false; un mapa se modela como lista de pares)",
                )
            )
    elif kind == "string":
        found.extend(_string_structure(schema, path, closed=closed))
    elif kind == "array":
        if not isinstance(schema.get("maxItems"), int):
            found.append(SchemaProblem(path, "lista sin número máximo de elementos"))
        if "items" not in schema:
            found.append(SchemaProblem(path, "lista sin esquema de elementos"))
    elif kind in {"integer", "number"} and not closed:
        if "minimum" not in schema and "exclusiveMinimum" not in schema:
            found.append(SchemaProblem(path, "número sin mínimo"))
        if "maximum" not in schema and "exclusiveMaximum" not in schema:
            found.append(SchemaProblem(path, "número sin máximo"))
    return found


def _string_structure(schema: JsonSchema, path: str, *, closed: bool) -> list[SchemaProblem]:
    found: list[SchemaProblem] = []
    if (
        schema.get("format") in _BINARY_FORMATS
        or "contentEncoding" in schema
        or "contentMediaType" in schema
    ):
        found.append(
            SchemaProblem(
                path,
                "contenido binario no admitido: el expediente no incrusta imágenes ni archivos; "
                "las evidencias se referencian con ClipReference",
            )
        )
    if closed:
        return found
    maximum = schema.get("maxLength")
    if not isinstance(maximum, int):
        found.append(SchemaProblem(path, "cadena sin longitud máxima"))
    elif not is_free_text(schema) and maximum > MAX_CLOSED_LENGTH:
        found.append(
            SchemaProblem(
                path, f"cadena cerrada de más de {MAX_CLOSED_LENGTH} caracteres no admitida"
            )
        )
    return found


def privacy_problems(schema: JsonSchema, free_text_paths: Sequence[str]) -> list[SchemaProblem]:
    """Metapropiedad PR-NUC-16 sobre un esquema (BR-NUC-51).

    Un campo con valor constante (``no_identifiable_person_declared: const true``) no puede
    llevar la identidad de nadie y no se examina por su nombre.
    """
    nodes, problems = field_nodes(schema)
    declared = set(free_text_paths)
    reported: set[str] = set()
    for node in nodes:
        if node.path in reported:
            continue
        if node.name is not None and "const" not in node.schema:
            reason = forbidden_name_reason(node.name)
            if reason is not None:
                reported.add(node.path)
                problems.append(
                    SchemaProblem(node.path, f"campo prohibido por BR-NUC-51: {reason}")
                )
                continue
        if is_free_text(node.schema) and node.path not in declared:
            reported.add(node.path)
            problems.append(
                SchemaProblem(node.path, "texto libre fuera de las rutas de free_text_paths")
            )
    return problems


def resolve_path(schema: JsonSchema, path: str) -> list[FieldNode]:
    """Alternativas del esquema en una ruta de contenido (vacía si la ruta no existe)."""
    nodes, _ = field_nodes(schema)
    return [node for node in nodes if node.path == path]


def normalized_schema(schema: Any) -> Any:
    """Forma comparable: sin anotaciones y con ``required`` y ``enum`` ordenados."""
    if isinstance(schema, Mapping):
        result: dict[str, Any] = {}
        for key, value in schema.items():
            if key in _ANNOTATIONS:
                continue
            if key == "properties" and isinstance(value, Mapping):
                result[key] = {name: normalized_schema(sub) for name, sub in value.items()}
            elif key in {"required", "enum"} and isinstance(value, list):
                result[key] = sorted(value, key=repr)
            else:
                result[key] = normalized_schema(value)
        return result
    if isinstance(schema, list):
        return [normalized_schema(item) for item in schema]
    return schema


# --- compatibilidad entre versiones (BR-NUC-52) -------------------------------------------


def compatibility_problems(old: JsonSchema, new: JsonSchema) -> list[SchemaProblem]:
    """Por qué ``new`` no es una ampliación de ``old`` (vacía si lo es)."""
    problems: list[SchemaProblem] = []
    _compare(old, _Resolver(old), new, _Resolver(new), "", problems, 0)
    return problems


def _compare(
    old: Any,
    old_resolver: _Resolver,
    new: Any,
    new_resolver: _Resolver,
    path: str,
    problems: list[SchemaProblem],
    depth: int,
) -> None:
    if depth > _MAX_DEPTH:
        problems.append(SchemaProblem(path, "anidamiento excesivo o esquema recursivo"))
        return
    old_alternatives = old_resolver.alternatives(old, problems, path)
    new_alternatives = new_resolver.alternatives(new, problems, path)
    for old_alternative in old_alternatives:
        kind = _kind(old_alternative)
        candidates = [n for n in new_alternatives if _kind(n) == kind]
        if not candidates:
            problems.append(SchemaProblem(path, f"ya no admite valores de tipo {kind}"))
            continue
        best: list[SchemaProblem] | None = None
        for candidate in candidates:
            found: list[SchemaProblem] = []
            _compare_same(
                old_alternative, old_resolver, candidate, new_resolver, path, found, depth
            )
            if not found:
                best = []
                break
            if best is None:
                best = found
        problems.extend(best or [])


def _lower_bound(node: JsonSchema) -> tuple[float, bool]:
    if "exclusiveMinimum" in node:
        return float(node["exclusiveMinimum"]), True
    if "minimum" in node:
        return float(node["minimum"]), False
    return -math.inf, False


def _upper_bound(node: JsonSchema) -> tuple[float, bool]:
    if "exclusiveMaximum" in node:
        return float(node["exclusiveMaximum"]), True
    if "maximum" in node:
        return float(node["maximum"]), False
    return math.inf, False


def _narrower_lower(old: tuple[float, bool], new: tuple[float, bool]) -> bool:
    return new[0] > old[0] or (new[0] == old[0] and new[1] and not old[1])


def _narrower_upper(old: tuple[float, bool], new: tuple[float, bool]) -> bool:
    return new[0] < old[0] or (new[0] == old[0] and new[1] and not old[1])


def _limit(node: JsonSchema, key: str, default: float) -> float:
    value = node.get(key)
    return (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default
    )


def _compare_same(
    old: JsonSchema,
    old_resolver: _Resolver,
    new: JsonSchema,
    new_resolver: _Resolver,
    path: str,
    problems: list[SchemaProblem],
    depth: int,
) -> None:
    old_values = _closed_values(old)
    new_values = _closed_values(new)
    if new_values is not None:
        if old_values is None:
            problems.append(SchemaProblem(path, "pasó a una lista cerrada de valores"))
        else:
            removed = [v for v in old_values if v not in new_values]
            if removed:
                problems.append(SchemaProblem(path, f"valores retirados de la lista: {removed}"))

    if _limit(new, "maxLength", math.inf) < _limit(old, "maxLength", math.inf):
        problems.append(
            SchemaProblem(
                path,
                f"longitud máxima reducida de {old.get('maxLength')} a {new.get('maxLength')}",
            )
        )
    if _limit(new, "minLength", 0) > _limit(old, "minLength", 0):
        problems.append(
            SchemaProblem(
                path,
                f"longitud mínima elevada de {old.get('minLength', 0)} a {new.get('minLength')}",
            )
        )
    for key in ("pattern", "format", "multipleOf"):
        if key in new and new.get(key) != old.get(key):
            problems.append(SchemaProblem(path, f"«{key}» cambiado o añadido"))

    if _narrower_lower(_lower_bound(old), _lower_bound(new)):
        problems.append(SchemaProblem(path, "mínimo elevado (rango estrechado)"))
    if _narrower_upper(_upper_bound(old), _upper_bound(new)):
        problems.append(SchemaProblem(path, "máximo reducido (rango estrechado)"))

    for maximum, minimum, label in (
        ("maxItems", "minItems", "elementos"),
        ("maxProperties", "minProperties", "entradas"),
    ):
        if _limit(new, maximum, math.inf) < _limit(old, maximum, math.inf):
            problems.append(SchemaProblem(path, f"número máximo de {label} reducido"))
        if _limit(new, minimum, 0) > _limit(old, minimum, 0):
            problems.append(SchemaProblem(path, f"número mínimo de {label} elevado"))
    if new.get("uniqueItems") is True and old.get("uniqueItems") is not True:
        problems.append(SchemaProblem(path, "pasó a exigir elementos únicos"))

    for key in sorted((set(old) | set(new)) - _HANDLED):
        if normalized_schema(old.get(key)) != normalized_schema(new.get(key)):
            problems.append(
                SchemaProblem(path, f"cambio en «{key}» que no se puede verificar compatible")
            )

    if _kind(old) == "object":
        _compare_object(old, old_resolver, new, new_resolver, path, problems, depth)
    elif _kind(old) == "array" and "items" in old and "items" in new:
        _compare(
            old["items"],
            old_resolver,
            new["items"],
            new_resolver,
            f"{path}[*]",
            problems,
            depth + 1,
        )


def _compare_object(
    old: JsonSchema,
    old_resolver: _Resolver,
    new: JsonSchema,
    new_resolver: _Resolver,
    path: str,
    problems: list[SchemaProblem],
    depth: int,
) -> None:
    old_properties: Mapping[str, Any] = old.get("properties", {})
    new_properties: Mapping[str, Any] = new.get("properties", {})
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    for name, old_schema in old_properties.items():
        child = f"{path}/{_escape(name)}"
        if name not in new_properties:
            problems.append(SchemaProblem(child, "campo retirado"))
            continue
        if name in new_required and name not in old_required:
            problems.append(SchemaProblem(child, "campo opcional que pasó a obligatorio"))
        _compare(
            old_schema, old_resolver, new_properties[name], new_resolver, child, problems, depth + 1
        )
    for name in new_properties:
        if name not in old_properties and name in new_required:
            problems.append(SchemaProblem(f"{path}/{_escape(name)}", "campo obligatorio nuevo"))
    old_extra = old.get("additionalProperties", True)
    new_extra = new.get("additionalProperties", True)
    if new_extra is False and old_extra is not False:
        problems.append(SchemaProblem(path, "dejó de admitir entradas adicionales"))
    elif isinstance(old_extra, Mapping) and isinstance(new_extra, Mapping):
        _compare(old_extra, old_resolver, new_extra, new_resolver, f"{path}/*", problems, depth + 1)
    if (
        normalized_schema(old.get("propertyNames")) != normalized_schema(new.get("propertyNames"))
        and "propertyNames" in new
    ):
        problems.append(SchemaProblem(path, "«propertyNames» cambiado o añadido"))
