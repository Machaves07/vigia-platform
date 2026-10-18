"""Validador ``person_attribution`` (LC-LAZ-16; BLM §14.5; BR-LAZ-84; A-45 punto 3; PR-LAZ-25).

Rechaza con **fallo cerrado** (BR-LAZ-80) todo texto libre que atribuya una causa a una persona o
que la identifique. No es configurable por la organización (BLM §14.5): son los patrones de la
tabla de §14.5 más los datos de contacto de A-45 punto 3.

Sobre la **forma de validación** (``folding.validation_form``: forma canónica de U-02, ``casefold``
y confusables de UTS #39), sin caracteres invisibles:

- ``role_action``: un cargo como sujeto de una acción u omisión causal («el operario omitió», «la
  supervisora de turno no…»);
- ``role_as_cause``: un cargo declarado como causa («causa raíz: el operario», «atribuible al
  técnico»);
- ``human_error``: fórmulas de error humano como causa («falta de atención», «falla humana»,
  «error del operador»);
- ``identity_document``: un documento de identidad con su número (cinco dígitos o más);
- ``email``: un correo electrónico, también escrito con «arroba» y «punto» (A-45 punto 3);
- ``phone``: un teléfono, **siete dígitos o más** con separadores opcionales (espacio, punto,
  guion, paréntesis). No cuentan las fechas (``2026-10-18``, ``18/10/2026``) ni las cifras con
  separador de miles (``1.500.000``);
- ``vehicle_plate``: una placa (tres letras y tres dígitos, o tres letras, dos dígitos y una
  letra) **a menos de cuatro palabras** de «placa», «matrícula», «vehículo», «carro», «moto»,
  «camión»… Sin esa palabra, un código como «PRO-001» es el de un procedimiento o un equipo.

Y sobre la **forma con mayúsculas** (NFKC, sin tildes ni invisibles, espacios Unicode como un
espacio y los confusables de UTS #39 conservando la mayúscula: la je cirílica U+0408 se lee «J»):

- ``proper_name``: dos palabras seguidas con mayúscula inicial (o todo en mayúsculas), como la
  expresión de §14.5, **salvo** que una de las dos sea una palabra común
  (``resources/vocabulary/common_words.es.json``, versión ``COMMON_WORDS_VERSION``: «Zona Norte»,
  «Prensa Hidráulica») o que el par pertenezca al **vocabulario declarado** de la organización:
  los nombres de sus plantas y zonas (``DeclaredNames``), que lee la caché por proceso
  (``cache.DeclaredNamesCache``) al abrir la escritura y **nunca** aquí.

**Clases de campo** (``field_kind``). El validador corre en todas las rutas
(``FreeTextPolicyRegistry`` aplica cada validador a cada campo), pero algunas rutas existen
**para** llevar un nombre o un identificador técnico:

- ``name``: los nombres visibles de usuarios (``…/display_name``, ``…_display_name``) y los de
  organización, planta y zona. Un nombre ahí es lo esperado: no se aplica ``proper_name``; el
  resto sí (un correo en un nombre se rechaza, N-14);
- ``identifier``: licencias profesionales, versiones, claves de almacenamiento y de firma, zona
  horaria, fuente del reloj, nombres de archivo y referencias de concepto legal. No se aplican
  ``proper_name``, ``phone`` ni ``vehicle_plate`` (sus cifras y códigos no son de una persona);
- ``prose``: todo lo demás, con **todos** los patrones. Una ruta nueva sin declarar es ``prose``.

Las rutas exentas de §2.12 (``/added[*]/value`` y ``/removed[*]/value`` de
``vocabulary_list_changed``, ``VALIDATOR_EXEMPT_PATHS``) no pasan por aquí, igual que en
``blocked_vocabulary``. La metapropiedad PR-LAZ-24 fija las tres clases sobre el registro real.

El rechazo es ``PersonAttributionRejected``: un ``FreeTextRejected`` con
``detail_code = loop_person_attribution_detected``, que el escritor responde como
``free_text_rejected`` sin escribir nada. El veredicto dice **qué patrón** cayó y propone una
alternativa; nunca repite el texto ni lo que coincidió (es un dato de una persona, P3).

Sin red, sin base y sin reloj. **Módulo crítico** (LC-LAZ-16): se importa sin FastAPI ni
SQLAlchemy.
"""

from __future__ import annotations

import enum
import functools
import hashlib
import itertools
import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from vigia_platform.ledger.free_text import FreeTextCandidate, FreeTextField, FreeTextRejected
from vigia_platform.loop.record_types import VALIDATOR_EXEMPT_PATHS
from vigia_platform.loop.validators.folding import confusable_map, validation_form

__all__ = [
    "COMMON_WORDS_PATH",
    "COMMON_WORDS_VERSION",
    "DETAIL_CODE",
    "NO_DECLARED_NAMES",
    "PERSON_ATTRIBUTION",
    "PERSON_ATTRIBUTION_DETECTED",
    "AttributionPattern",
    "DeclaredNames",
    "FieldKind",
    "PersonAttributionRejected",
    "PersonAttributionValidator",
    "PersonAttributionVerdict",
    "cased_form",
    "common_words",
    "declared_names",
    "field_kind",
    "find_attribution",
]

PERSON_ATTRIBUTION: Final = "person_attribution"
"""Nombre con el que se registra en ``FreeTextPolicyRegistry`` (BLM §9)."""

PERSON_ATTRIBUTION_DETECTED: Final = "person_attribution_detected"
"""Código del veredicto (``loop_error_code`` de domain-entities §1.2, sin prefijo)."""

DETAIL_CODE: Final = "loop_person_attribution_detected"
"""``LoopDetailCode.PERSON_ATTRIBUTION_DETECTED``, escrito aquí para no importar la capa HTTP; una
prueba exige que coincidan."""

_MESSAGE: Final = "el texto atribuye la causa a una persona o la identifica (RF-PLA-15)"
"""Mensaje genérico: no repite el texto ni lo que coincidió."""

COMMON_WORDS_PATH: Final = (
    Path(__file__).resolve().parents[4] / "resources" / "vocabulary" / "common_words.es.json"
)


class AttributionPattern(enum.StrEnum):
    """El patrón de §14.5 o de A-45 punto 3 que cayó."""

    ROLE_ACTION = "role_action"
    ROLE_AS_CAUSE = "role_as_cause"
    HUMAN_ERROR = "human_error"
    IDENTITY_DOCUMENT = "identity_document"
    PROPER_NAME = "proper_name"
    EMAIL = "email"
    PHONE = "phone"
    VEHICLE_PLATE = "vehicle_plate"


class FieldKind(enum.Enum):
    """La clase de un campo de texto libre (ver el docstring del módulo)."""

    PROSE = "prose"
    NAME = "name"
    IDENTIFIER = "identifier"


_SKIPPED: Final[Mapping[FieldKind, frozenset[AttributionPattern]]] = {
    FieldKind.PROSE: frozenset(),
    FieldKind.NAME: frozenset({AttributionPattern.PROPER_NAME}),
    FieldKind.IDENTIFIER: frozenset(
        {AttributionPattern.PROPER_NAME, AttributionPattern.PHONE, AttributionPattern.VEHICLE_PLATE}
    ),
}

_SUGGESTIONS: Final[Mapping[AttributionPattern, str]] = {
    AttributionPattern.ROLE_ACTION: (
        "Describa la condición de la zona, del equipo o del procedimiento, no lo que hizo una"
        " persona."
    ),
    AttributionPattern.ROLE_AS_CAUSE: (
        "Declare la causa como de proceso, de ingeniería o de organización."
    ),
    AttributionPattern.HUMAN_ERROR: (
        "Describa qué condición del proceso o de la ingeniería permitió el evento."
    ),
    AttributionPattern.IDENTITY_DOCUMENT: "Retire el documento de identidad del texto.",
    AttributionPattern.PROPER_NAME: (
        "Retire el nombre de la persona; nombre la zona, el equipo o el cargo sin persona."
    ),
    AttributionPattern.EMAIL: "Retire el correo electrónico del texto.",
    AttributionPattern.PHONE: "Retire el número de teléfono del texto.",
    AttributionPattern.VEHICLE_PLATE: "Retire la placa del vehículo del texto.",
}

# --- Clases de campo ------------------------------------------------------------------------------

_NAME_FIELDS: Final = frozenset(
    {
        ("organization", "/name"),
        ("plant", "/name"),
        ("zone", "/name"),
        ("organization_created", "/name"),
        ("plant_created", "/name"),
        ("zone_created", "/name"),
    }
)
"""Nombres de organización, planta y zona (los de ``identity`` y sus registros de alta)."""

_IDENTIFIER_SEGMENTS: Final = frozenset(
    {
        "professional_license",
        "contract_version",
        "software_version",
        "target_version",
        "storage_key",
        "key_id",
        "timezone",
        "filename",
        "legal_opinion_reference",
    }
)
_IDENTIFIER_SUFFIXES: Final = ("/clock/source",)


def field_kind(field: FreeTextField) -> FieldKind:
    """La clase de ``field``: ``name``, ``identifier`` o, si no se declaró, ``prose``."""
    last = field.path.rsplit("/", 1)[-1]
    if last == "display_name" or last.endswith("_display_name"):
        return FieldKind.NAME
    if (field.record_type, field.path) in _NAME_FIELDS:
        return FieldKind.NAME
    if last in _IDENTIFIER_SEGMENTS or field.path.endswith(_IDENTIFIER_SUFFIXES):
        return FieldKind.IDENTIFIER
    return FieldKind.PROSE


# --- Formas ---------------------------------------------------------------------------------------

_INVISIBLE_RANGES: Final = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
"""Los mismos rangos que la política base de U-02 rechaza (``Default_Ignorable_Code_Point`` y
U+2800). La política base ya los rechaza antes de llegar aquí; el validador los quita además de su
forma para que su veredicto no dependa de ese orden."""

_REPEATED_SPACES: Final = re.compile(" {2,}")


def _invisible(char: str) -> bool:
    code = ord(char)
    if unicodedata.category(char) == "Cf":
        return True
    return any(low <= code <= high for low, high in _INVISIBLE_RANGES)


def _visible(text: str) -> str:
    if text.isascii():
        return text
    return _REPEATED_SPACES.sub(" ", "".join(char for char in text if not _invisible(char)))


def cased_form(text: str) -> str:
    """La forma del patrón de nombre propio: como la canónica de U-02, pero **con mayúsculas**.

    NFKC, sin marcas diacríticas ni invisibles, todo espacio ``Zs`` como un espacio ASCII y cada
    confusable no ASCII de UTS #39 sustituido por su prototipo ASCII, en mayúscula si el carácter
    lo era. Solo para validar; nunca se guarda.
    """
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    bare = unicodedata.normalize(
        "NFKC", "".join(char for char in decomposed if not unicodedata.combining(char))
    )
    table = confusable_map()
    out: list[str] = []
    for char in bare:
        if char.isascii():
            out.append(char)
        elif unicodedata.category(char) == "Zs":
            out.append(" ")
        elif _invisible(char):
            continue
        else:
            prototype = table.get(ord(char))
            if prototype is None:
                out.append(char)
            else:
                out.append(prototype.upper() if char.isupper() else prototype)
    return _REPEATED_SPACES.sub(" ", "".join(out))


# --- Vocabulario común y declarado ----------------------------------------------------------------

_TOKEN: Final = re.compile(r"[^\W\d_]+")


@functools.cache
def _common_document() -> tuple[str, frozenset[str]]:
    raw = COMMON_WORDS_PATH.read_bytes()
    document = json.loads(raw)
    words: list[str] = document["words"]
    digest = hashlib.sha256(raw).hexdigest()[:12]
    version = f"common_words.es/v{int(document['version'])}+sha256:{digest}"
    return version, frozenset(cased_form(word).casefold() for word in words)


def common_words() -> frozenset[str]:
    """Las palabras comunes, plegadas como el texto (minúsculas, sin tildes)."""
    return _common_document()[1]


COMMON_WORDS_VERSION: Final = _common_document()[0]
"""Versión del archivo de palabras comunes (su ``version`` y el SHA-256 del archivo)."""


@dataclass(frozen=True)
class DeclaredNames:
    """El vocabulario declarado de una organización: los pares de palabras de sus nombres."""

    pairs: frozenset[tuple[str, str]]


NO_DECLARED_NAMES: Final = DeclaredNames(pairs=frozenset())
"""Sin vocabulario declarado (la organización aún no se cargó, o el llamador no dio organización):
solo exime el vocabulario común, de modo que nunca es más laxo que con la lista cargada."""


def declared_names(names: Iterable[str]) -> DeclaredNames:
    """Compila los nombres de plantas y zonas (y, con TASK-350, equipos y máquinas) en pares."""
    pairs: set[tuple[str, str]] = set()
    for name in names:
        words = [found.group().casefold() for found in _TOKEN.finditer(cased_form(name))]
        pairs.update(itertools.pairwise(words))
    return DeclaredNames(pairs=frozenset(pairs))


# --- Patrones -------------------------------------------------------------------------------------

_B: Final = r"(?<![^\W_])"
_E: Final = r"(?![^\W_])"
_WORD: Final = r"[^\W\d_]+"

_ROLES: Final = (
    r"(?:operari[oa]s?|operador(?:a|es|as)?|trabajador(?:a|es|as)?|emplead[oa]s?"
    r"|colaborador(?:a|es|as)?|supervisor(?:a|es|as)?|tecnic[oa]s?|auxiliar(?:es)?"
    r"|contratistas?|jef[ea]s?|encargad[oa]s?|mecanic[oa]s?|electricistas?"
    r"|soldador(?:a|es|as)?|montacarguistas?|coordinador(?:a|es|as)?|inspector(?:a|es|as)?"
    r"|aprendiz|aprendices|practicantes?|lider(?:es|esa|esas)?|vigilantes?"
    r"|celador(?:a|es|as)?|analistas?|ingenier[oa]s?|asistentes?|ayudantes?|obrer[oa]s?"
    r"|personal|brigadistas?)"
)
"""Cargos (§14.5 y sus géneros, números y los cargos de planta de uso corriente)."""

_DETERMINERS: Final = (
    r"(?:el|la|los|las|un|una|unos|unas|este|esta|estos|estas|ese|esa|esos|esas|aquel|aquella"
    r"|nuestro|nuestra|nuestros|nuestras|dicho|dicha)"
)
_VERBS: Final = (
    r"(?:no|omiti\w*|olvid\w*|ingres\w*|entr(?:o|aron)|decidi\w*|fall(?:o|aron|a|an)"
    r"|incumpli\w*|viol(?:o|aron)|ignor(?:o|aron)|desactiv(?:o|aron)|desobedeci\w*"
    r"|salt(?:o|aron)|se (?:salt\w*|distraj\w*|confi\w*|meti\w*|durmi\w*))"
)
"""Verbos de acción u omisión causal (§14.5 en sus tiempos y personas)."""

_PLATE: Final = r"(?:[a-z]{3}[ -]?\d{3}|[a-z]{3}[ -]?\d{2}[a-z])"
"""Placa de carro (tres letras y tres dígitos) o de moto (tres letras, dos dígitos y una letra)."""

_COMPLEMENT: Final = rf"(?: (?:de|del|de la|de los|de las) {_WORD})?(?: {_WORD})?"
"""Lo que puede ir entre el cargo y el verbo: «de turno», «nuevo»."""

_PATTERNS: Final[tuple[tuple[AttributionPattern, re.Pattern[str]], ...]] = (
    (
        AttributionPattern.ROLE_ACTION,
        re.compile(rf"{_B}{_DETERMINERS} {_ROLES}{_COMPLEMENT} {_VERBS}{_E}"),
    ),
    (
        AttributionPattern.ROLE_AS_CAUSE,
        re.compile(
            rf"{_B}(?:causa(?: (?:raiz|principal|probable|basica|inmediata))?"
            r"|atribuible|atribuida|atribuido|imputable|responsabilidad)"
            r"(?: (?:fue|es|son|fueron|seria))?[ :]*"
            rf"(?:(?:a|al|a la|a los|a las|de|del|de la|de los|de las) )?"
            rf"(?:{_DETERMINERS} )?{_ROLES}{_E}"
        ),
    ),
    (
        AttributionPattern.HUMAN_ERROR,
        re.compile(
            rf"{_B}(?:falta|faltas|falla|fallas|fallo|fallos|error|errores)"
            r" (?:human[oa]s?|de atencion|de cuidado|de concentracion"
            rf"|(?:del|de la|de los|de las) {_ROLES}){_E}"
        ),
    ),
    (
        AttributionPattern.IDENTITY_DOCUMENT,
        re.compile(
            rf"{_B}(?:cedula|cc|c\.c|documento|identificacion|carnet|carne|dni|pasaporte|nuip"
            r"|tarjeta de identidad)(?: de (?:ciudadania|identidad|extranjeria))?"
            r"[\W_]{0,3}(?:(?:n|no|nro|num|numero)[\W_]{0,3})?\d(?:[ .,-]?\d){4,}(?!\d)"
        ),
    ),
    (
        AttributionPattern.EMAIL,
        re.compile(
            rf"{_B}[a-z0-9][a-z0-9._%+-]*(?: ?@ ?| ?[(\[]at[)\]] ?| arroba )"
            r"[a-z0-9-]+(?:(?:\.| punto | ?[(\[]dot[)\]] ?)[a-z0-9-]+)*"
            rf"(?:\.| punto | ?[(\[]dot[)\]] ?)[a-z]{{2,}}{_E}"
        ),
    ),
    (
        AttributionPattern.VEHICLE_PLATE,
        re.compile(
            rf"{_B}(?:placas?|matriculas?|vehiculos?|carros?|motos?|motocicletas?|camion|camiones"
            r"|camionetas?|automovil(?:es)?|taxis?|bus|buses|busetas?)"
            rf"(?:[\W_]+{_WORD}){{0,3}}?[\W_]+{_PLATE}{_E}"
        ),
    ),
)

_PHONE: Final = re.compile(r"(?<![\w+])\+?\(?\d(?:[ .\-()]{0,2}\d){6,}(?!\w)")
_NOT_A_PHONE: Final = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"  # fecha ISO o con barras
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"  # fecha día/mes/año
    r"|\d{1,3}(?:\.\d{3})+(?:,\d+)?"  # miles con punto
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"  # miles con coma
)


def _phone(form: str) -> bool:
    return any(_NOT_A_PHONE.fullmatch(found.group()) is None for found in _PHONE.finditer(form))


def _name_shaped(word: str) -> bool:
    if len(word) < 2:
        return False
    return word.isupper() or (word[0].isupper() and word[1:].islower())


def _proper_name(text: str, declared: DeclaredNames) -> bool:
    cased = cased_form(text)
    common = common_words()
    previous: re.Match[str] | None = None
    for found in _TOKEN.finditer(cased):
        if (
            previous is not None
            and cased[previous.end() : found.start()] == " "
            and _name_shaped(previous.group())
            and _name_shaped(found.group())
        ):
            first, second = previous.group().casefold(), found.group().casefold()
            if (
                first not in common
                and second not in common
                and (first, second) not in declared.pairs
            ):
                return True
        previous = found
    return False


@dataclass(frozen=True)
class PersonAttributionVerdict:
    """``TextValidationResult`` de un rechazo: el patrón y la alternativa, sin el texto."""

    pattern: AttributionPattern
    suggestion_es: str
    code: Literal["person_attribution_detected"] = PERSON_ATTRIBUTION_DETECTED


def find_attribution(
    candidate: FreeTextCandidate,
    *,
    kind: FieldKind = FieldKind.PROSE,
    declared: DeclaredNames = NO_DECLARED_NAMES,
) -> PersonAttributionVerdict | None:
    """El primer patrón (en el orden de ``AttributionPattern``) que cae en ``candidate``."""
    skipped = _SKIPPED[kind]
    form = _visible(validation_form(candidate))
    for pattern, compiled in _PATTERNS:
        if pattern not in skipped and compiled.search(form) is not None:
            return _verdict(pattern)
    if AttributionPattern.PHONE not in skipped and _phone(form):
        return _verdict(AttributionPattern.PHONE)
    if AttributionPattern.PROPER_NAME not in skipped and _proper_name(candidate.text, declared):
        return _verdict(AttributionPattern.PROPER_NAME)
    return None


def _verdict(pattern: AttributionPattern) -> PersonAttributionVerdict:
    return PersonAttributionVerdict(pattern=pattern, suggestion_es=_SUGGESTIONS[pattern])


class PersonAttributionRejected(FreeTextRejected):
    """Rechazo por atribución a persona, con su veredicto (que nunca llega al expediente)."""

    def __init__(self, field: FreeTextField, verdict: PersonAttributionVerdict) -> None:
        super().__init__(PERSON_ATTRIBUTION_DETECTED, field, _MESSAGE, detail_code=DETAIL_CODE)
        self.verdict = verdict


class PersonAttributionValidator:
    """El validador enchufable: memoria y CPU, nunca red ni base.

    ``names`` da el vocabulario declarado vigente de una organización (el de la caché por
    proceso; ``NO_DECLARED_NAMES`` si la organización es ``None`` o aún no se cargó).
    """

    def __init__(
        self, names: Callable[[uuid.UUID | None], DeclaredNames] = lambda _: NO_DECLARED_NAMES
    ) -> None:
        self._names = names

    def verdict(
        self, candidate: FreeTextCandidate, kind: FieldKind = FieldKind.PROSE
    ) -> PersonAttributionVerdict | None:
        """El veredicto de ``candidate`` en un campo de clase ``kind`` (``None``: aceptado)."""
        return find_attribution(
            candidate, kind=kind, declared=self._names(candidate.organization_id)
        )

    def __call__(self, candidate: FreeTextCandidate, field: FreeTextField) -> None:
        if field.path in VALIDATOR_EXEMPT_PATHS.get(field.record_type, ()):
            return  # BR-LAZ-83: el valor de un término de la lista se valida solo con la base
        verdict = self.verdict(candidate, field_kind(field))
        if verdict is not None:
            raise PersonAttributionRejected(field, verdict)
