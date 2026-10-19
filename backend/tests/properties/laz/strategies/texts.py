"""Textos de los validadores de U-04 (TASK-303; PR-LAZ-22, 23 y 53; A-45).

- ``blocked_vocabulary_texts``: un texto con **una** flexión de un lema de la lista inicial (una
  forma de la tabla ``resources/vocabulary/inflections.es.json``, con sus tildes) o con
  **una** instancia de un patrón (``PATTERN_INSTANCES``: la columna «Qué cae» de BLM §14 y sus
  variantes), entre palabras del vocabulario permitido, con mayúsculas al azar. Devuelve el texto
  y el ``term_id`` que el rechazo debe nombrar.
- ``allowed_texts``: frases del vocabulario permitido (``ALLOWED_WORDS``), que ningún término
  bloquea, con mayúsculas y signos al azar.
- ``unicode_texts``: un texto bloqueado y una variante que esquiva la comparación ASCII con
  homoglifos cirílicos, espacios no ASCII (NBSP, U+3000, U+2009), dígitos y letras de ancho
  completo o invisibles (``UnicodeVariant.kind``).

Para ``person_attribution`` (TASK-340; PR-LAZ-25; A-45 punto 3):

- ``person_attribution_texts``: un cargo con verbo de acción u omisión, un cargo como causa, una
  fórmula de error humano, un documento de identidad, un nombre propio, un correo, un teléfono o
  una placa, entre palabras permitidas en minúsculas; devuelve el patrón que debe caer;
- ``attribution_unicode_texts``: uno de esos textos y su variante con las mismas evasiones
  Unicode que ``unicode_texts``;
- ``zone_equipment_texts``: textos de control que deben pasar: zonas, equipos y máquinas con
  mayúsculas, códigos de procedimiento, normas, fechas y cifras; los equipos con marca citan su
  nombre declarado (``ZoneText.declared``).

Solo datos generados: los nombres de persona son sintéticos y de uso corriente (``FIRST_NAMES`` y
``SURNAMES``), y los correos y teléfonos son de dominios ``.test`` o dígitos al azar.
"""

from __future__ import annotations

import functools
import json
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st

from vigia_platform.loop.domain.enums import BlockedTermForm
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.initial_list import INITIAL_TERMS, InitialTerm

__all__ = [
    "ALLOWED_WORDS",
    "ATTRIBUTION_KINDS",
    "DECLARED_BRANDS",
    "FIRST_NAMES",
    "PATTERN_INSTANCES",
    "SURNAMES",
    "AttributionText",
    "AttributionVariant",
    "BlockedText",
    "UnicodeVariant",
    "ZoneText",
    "allowed_texts",
    "attribution_unicode_texts",
    "blocked_vocabulary_texts",
    "inflections",
    "person_attribution_texts",
    "unicode_texts",
    "zone_equipment_texts",
    "zone_equipment_unicode_texts",
]

ALLOWED_WORDS: Final = (
    "zona", "planta", "guarda", "barrera", "revisión", "turno", "equipo", "mantenimiento",
    "acta", "cámara", "señal", "cobertura", "observable", "periodo", "norte", "sur", "prensa",
    "línea", "operación", "registro", "inspección", "protección", "resguardo", "válvula",
    "sensor", "baliza", "bocina", "estándar", "procedimiento", "condición", "proceso",
    "ingeniería", "organización", "hallazgo", "acción", "correctiva", "evidencia", "nodo",
    "latido", "umbral", "calibración", "iluminación", "pasillo", "montacargas", "cinta",
    "transportadora", "acceso", "puerta", "enclavamiento", "parada", "emergencia", "verificada",
    "instalada", "ajustada", "revisada", "programada", "durante", "con", "en", "el", "la",
    "los", "las", "de", "del", "y", "para",
)  # fmt: skip
"""Palabras que ningún término de la lista inicial bloquea, solas o seguidas."""

PATTERN_INSTANCES: Final[Mapping[str, tuple[str, ...]]] = {
    term.value: instances
    for term, instances in zip(
        (term for term in INITIAL_TERMS if term.form is BlockedTermForm.PATTERN),
        (
            ("no hay nadie en la zona", "No hay personas", "no hay gente"),
            ("zona sin personas", "sin gente", "sin nadie", "sin presencia"),
            ("no ocurrió nada en el turno", "No pasó nada", "no sucedió nada"),
            (
                "la zona está segura",
                "la zona quedó segura",
                "zona permanece segura",
                "la zona se encuentra segura",
            ),
            ("zona libre de riesgo", "libre de personas", "libre de peligro"),
            ("cero presencias", "cero personas", "cero cruces", "cero presencia"),
            ("nadie entró a la zona", "nadie ingresó", "nadie estuvo", "nadie pasó"),
            (
                "95 % de detección",
                "99,5% de precisión",
                "80 % de efectividad",
                "100 % de cobertura",
                "90 % de éxito",
            ),
            ("precisión del 95", "efectividad del 99", "detección del 100"),
            ("100 % seguro", "100% efectivo", "100 % fiable", "100 % confiable"),
            ("cero falsos negativos", "cero accidentes", "cero incidentes", "cero errores"),
            ("detecta todos los cruces", "detectamos todo", "detecta siempre"),
            ("el sistema nunca falla", "nunca falla"),
            (
                "reducirá los accidentes",
                "reduce los incidentes",
                "reducción de lesiones",
                "reduciremos los accidentes",
                "reduce riesgo",
            ),
            ("reducción del 50 %", "reducción del 12,5%"),
            ("evitará accidentes", "prevendrá lesiones", "impedirá incidentes"),
            ("elimina el riesgo", "eliminará el peligro"),
            ("mejorará la seguridad en un 30 %", "mejora la seguridad en 20"),
            ("manipulación deliberada", "Manipulación  deliberada"),
            ("a propósito", "A propósito"),
            ("mala fe",),
            ("omisión voluntaria",),
            ("no quiso", "no le importó", "no hizo caso"),
            ("por culpa del", "por descuido de la", "por error del", "por negligencia de la"),
        ),
        strict=True,
    )
}
"""Instancias de cada patrón de la lista inicial (valor del patrón → textos que caen)."""


@dataclass(frozen=True)
class BlockedText:
    text: str
    term_ids: frozenset[uuid.UUID]
    """Los términos que el rechazo puede nombrar: el del que salió la instancia y, si la forma
    es también flexión de otro lema de la lista («culpa» de ``culpa`` y de ``culpar``), ese."""


@functools.cache
def inflections() -> Mapping[uuid.UUID, tuple[str, ...]]:
    """Por término de lema, sus formas de la tabla tal como están escritas (con tildes).

    Se leen del archivo directamente, sin pasar por ``lexicon``: la prueba comprueba que el
    validador pliega y reconoce cada forma escrita.
    """
    document = json.loads(lexicon.INFLECTIONS_PATH.read_text(encoding="utf-8"))
    table: dict[str, list[str]] = document["lemmas"]
    return {
        term.term_id: tuple(sorted({term.value, *table[term.value]}))
        for term in INITIAL_TERMS
        if term.form is BlockedTermForm.LEMMA
    }


def _cased(word: str, how: str) -> str:
    return {"lower": word, "upper": word.upper(), "title": word.capitalize()}[how]


_CASES: Final = st.sampled_from(("lower", "upper", "title"))
_SEPARATORS: Final = st.sampled_from((" ", " ", " ", ", ", "; ", ". "))


@st.composite
def _phrase(draw: st.DrawFn, max_words: int = 6) -> str:
    words = draw(st.lists(st.sampled_from(ALLOWED_WORDS), min_size=0, max_size=max_words))
    parts: list[str] = []
    for index, word in enumerate(words):
        if index:
            parts.append(draw(_SEPARATORS))
        parts.append(_cased(word, draw(_CASES)))
    return "".join(parts)


@functools.cache
def _lemma_owners() -> Mapping[str, frozenset[uuid.UUID]]:
    """Forma plegada → los términos de lema de cuyas flexiones forma parte."""
    owners: dict[str, set[uuid.UUID]] = {}
    for term_id, forms in inflections().items():
        for form in forms:
            owners.setdefault(lexicon.fold_word(form), set()).add(term_id)
    return {form: frozenset(ids) for form, ids in owners.items()}


def _term(term: InitialTerm) -> st.SearchStrategy[tuple[frozenset[uuid.UUID], str]]:
    if term.form is BlockedTermForm.LEMMA:
        return st.sampled_from(inflections()[term.term_id]).map(
            lambda form: (_lemma_owners()[lexicon.fold_word(form)], form)
        )
    return st.sampled_from(PATTERN_INSTANCES[term.value]).map(
        lambda instance: (frozenset({term.term_id}), instance)
    )


@st.composite
def blocked_vocabulary_texts(draw: st.DrawFn) -> BlockedText:
    """Una flexión de un lema bloqueado o una instancia de patrón, en un texto permitido."""
    term = draw(st.sampled_from(INITIAL_TERMS))
    term_ids, instance = draw(_term(term))
    if draw(st.booleans()):
        instance = instance.upper() if draw(st.booleans()) else instance.capitalize()
    before, after = draw(_phrase()), draw(_phrase())
    text = " ".join(part for part in (before, instance, after) if part)
    return BlockedText(text=text, term_ids=term_ids)


@st.composite
def allowed_texts(draw: st.DrawFn) -> str:
    """Una frase del vocabulario permitido (al menos una palabra)."""
    return draw(_phrase(max_words=40).filter(bool))


_HOMOGLYPHS: Final = {
    latin: chr(code)
    for latin, code in (
        ("a", 0x0430),
        ("c", 0x0441),
        ("e", 0x0435),
        ("o", 0x043E),
        ("p", 0x0440),
        ("x", 0x0445),
        ("y", 0x0443),
        ("i", 0x0456),
        ("j", 0x0458),
        ("s", 0x0455),
        ("A", 0x0410),
        ("C", 0x0421),
        ("E", 0x0415),
        ("O", 0x041E),
        ("P", 0x0420),
    )
}
"""Letras latinas → la cirílica que se ve igual (confusables de UTS #39)."""
_SPACES: Final = tuple(map(chr, (0x00A0, 0x3000, 0x2009, 0x202F)))
"""NBSP, espacio ideográfico, espacio fino y NBSP estrecho."""
_INVISIBLES: Final = tuple(map(chr, (0x200B, 0x2060, 0xFEFF, 0x00AD, 0x034F, 0x200D)))
"""Anchura cero, unión de palabras, BOM, guion blando, CGJ y unión de anchura cero."""

Kind = Literal["homoglyph", "space", "fullwidth", "invisible", "mixed"]


@dataclass(frozen=True)
class UnicodeVariant:
    blocked: BlockedText
    """El texto bloqueado en su forma ASCII de partida."""
    text: str
    """La variante que esquiva una comparación ingenua."""
    kind: Kind


def _fullwidth(char: str) -> str:
    if "!" <= char <= "~":
        return chr(ord(char) - 0x21 + 0xFF01)
    return char


@st.composite
def unicode_texts(draw: st.DrawFn, kinds: tuple[Kind, ...] | None = None) -> UnicodeVariant:
    """Un texto bloqueado y su variante con homoglifos, espacios, ancho completo o invisibles."""
    blocked = draw(blocked_vocabulary_texts())
    kind: Kind = draw(
        st.sampled_from(kinds or ("homoglyph", "space", "fullwidth", "invisible", "mixed"))
    )
    return UnicodeVariant(blocked=blocked, text=_disguise(draw, blocked.text, kind), kind=kind)


def _disguise(draw: st.DrawFn, text: str, kind: Kind) -> str:
    """``text`` con homoglifos, espacios no ASCII, ancho completo o invisibles (``kind``)."""
    out: list[str] = []
    changed = False
    for index, char in enumerate(text):
        replacement = char
        mode = (
            kind if kind != "mixed" else draw(st.sampled_from(("homoglyph", "space", "fullwidth")))
        )
        if mode == "homoglyph" and char in _HOMOGLYPHS and draw(st.booleans()):
            replacement = _HOMOGLYPHS[char]
        elif mode == "space" and char == " ":
            replacement = draw(st.sampled_from(_SPACES))
        elif mode == "fullwidth" and draw(st.booleans()):
            replacement = _fullwidth(char)
        elif mode == "invisible" and char.isalpha() and index and not changed:
            replacement = draw(st.sampled_from(_INVISIBLES)) + char
        changed = changed or replacement != char
        out.append(replacement)
    if not changed and kind == "invisible":
        out.insert(1, _INVISIBLES[0])
    return "".join(out)


# --- person_attribution (TASK-340; PR-LAZ-25; A-45 punto 3) ---------------------------------------

FIRST_NAMES: Final = (
    "Juan", "María", "Carlos", "Ana", "Luis", "Diana", "Jorge", "Paola", "Andrés", "Camila",
    "Felipe", "Laura", "Sergio", "Natalia", "Óscar", "Lucía",
)  # fmt: skip
SURNAMES: Final = (
    "Pérez", "Gómez", "Rodríguez", "Martínez", "López", "Ramírez", "Torres", "Castro", "Vargas",
    "Moreno", "Rojas", "Herrera", "Jiménez", "Muñoz", "Díaz", "Ortiz",
)  # fmt: skip
"""Nombres y apellidos sintéticos de uso corriente: ninguna persona real ni ningún registro."""

_ROLES: Final = (
    "el operario", "la operaria", "el operador", "el trabajador", "el técnico", "la supervisora",
    "el auxiliar", "el contratista", "el jefe de turno", "el mecánico", "la electricista",
    "los operarios", "el operario nuevo", "la técnica de mantenimiento",
)  # fmt: skip
_ACTIONS: Final = (
    "omitió el bloqueo", "olvidó la guarda", "no usó el arnés", "no verificó la barrera",
    "ingresó a la zona", "decidió continuar", "falló en la parada", "incumplió el procedimiento",
    "violó la norma", "se saltó el enclavamiento", "ignoró la señal", "desactivó el sensor",
)  # fmt: skip
_CAUSES: Final = (
    "causa raíz: {role}", "Causa: {role}", "la causa fue {role}", "causa principal {role}",
    "atribuible a {role}", "responsabilidad de {role}",
)  # fmt: skip
_HUMAN_ERRORS: Final = (
    "falta de atención", "falla humana", "error humano", "fallo humano", "errores humanos",
    "falta de cuidado", "error del operario", "falla de la supervisora", "error del técnico",
)  # fmt: skip
_DOCUMENTS: Final = (
    "cédula {n}", "C.C. {n}", "documento n° {n}", "identificación número {n}", "carnet {n}",
    "cédula de ciudadanía No. {n}", "CC {n}", "pasaporte {n}",
)  # fmt: skip
_EMAILS: Final = ("{u}@example.test", "{u}@correo.test", "{u} arroba example punto test",
                  "{u}(at)example(dot)test", "{u} @ correo.test")  # fmt: skip
_PHONES: Final = ("{a} {b} {c}", "{a}{b}{c}", "+57 {a} {b} {c}", "({a}) {b}-{c}",
                  "{a}-{b}-{c}", "{a}.{b}.{c}", "{b} {c}")  # fmt: skip
_PLATES: Final = ("placa {p}", "carro de placas {p}", "vehículo con placa {p}", "la moto {p}",
                  "camión {p}", "matrícula {p}")  # fmt: skip

AttributionKind = Literal[
    "role_action",
    "role_as_cause",
    "human_error",
    "identity_document",
    "proper_name",
    "email",
    "phone",
    "vehicle_plate",
]


@dataclass(frozen=True)
class AttributionText:
    text: str
    pattern: AttributionKind
    """El patrón de ``AttributionPattern`` que el rechazo debe nombrar."""


_DIGITS: Final = st.text(alphabet="0123456789", min_size=1, max_size=1)


def _digits(count: int) -> st.SearchStrategy[str]:
    return st.lists(_DIGITS, min_size=count, max_size=count).map("".join)


def _plate() -> st.SearchStrategy[str]:
    letters = st.lists(st.sampled_from("ABCDEFGHJKLMNPRSTUVWXYZ"), min_size=3, max_size=3).map(
        "".join
    )
    return st.tuples(
        letters,
        st.sampled_from(("", " ", "-")),
        st.one_of(_digits(3), st.tuples(_digits(2), st.sampled_from("ABCDEFZ")).map("".join)),
    ).map("".join)


@st.composite
def _instance(draw: st.DrawFn, pattern: AttributionKind) -> str:
    if pattern == "role_action":
        role, action = draw(st.sampled_from(_ROLES)), draw(st.sampled_from(_ACTIONS))
        return f"{role} {action}"
    if pattern == "role_as_cause":
        return draw(st.sampled_from(_CAUSES)).format(role=draw(st.sampled_from(_ROLES)))
    if pattern == "human_error":
        return draw(st.sampled_from(_HUMAN_ERRORS))
    if pattern == "identity_document":
        number = draw(st.integers(min_value=5, max_value=10).flatmap(_digits))
        if draw(st.booleans()) and len(number) > 6:
            number = f"{number[:-6]}.{number[-6:-3]}.{number[-3:]}"
        return draw(st.sampled_from(_DOCUMENTS)).format(n=number)
    if pattern == "proper_name":
        first, last = draw(st.sampled_from(FIRST_NAMES)), draw(st.sampled_from(SURNAMES))
        name = f"{first} {last}"
        return name.upper() if draw(st.booleans()) else name
    if pattern == "email":
        user = draw(st.sampled_from(FIRST_NAMES)) + draw(st.sampled_from((".", "_", "")))
        user = ascii_lower(user + draw(st.sampled_from(SURNAMES)) + draw(_digits(2)))
        return draw(st.sampled_from(_EMAILS)).format(u=user)
    if pattern == "phone":
        return draw(st.sampled_from(_PHONES)).format(
            a=draw(_digits(3)), b=draw(_digits(3)), c=draw(_digits(4))
        )
    return draw(st.sampled_from(_PLATES)).format(p=draw(_plate()))


def ascii_lower(word: str) -> str:
    """``word`` en minúsculas ASCII sin tildes (la parte local de un correo sintético)."""
    decomposed = unicodedata.normalize("NFD", word.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


ATTRIBUTION_KINDS: Final[tuple[AttributionKind, ...]] = (
    "role_action",
    "role_as_cause",
    "human_error",
    "identity_document",
    "proper_name",
    "email",
    "phone",
    "vehicle_plate",
)


@st.composite
def _lower_phrase(draw: st.DrawFn, max_words: int = 5) -> str:
    """Palabras del vocabulario permitido en minúsculas: no forman ningún patrón."""
    return " ".join(draw(st.lists(st.sampled_from(ALLOWED_WORDS), max_size=max_words)))


@st.composite
def person_attribution_texts(
    draw: st.DrawFn, kinds: tuple[AttributionKind, ...] = ATTRIBUTION_KINDS
) -> AttributionText:
    """Una atribución a persona o un dato de contacto (``kinds``) entre palabras permitidas."""
    pattern = draw(st.sampled_from(kinds))
    instance = draw(_instance(pattern))
    if pattern != "proper_name" and draw(st.booleans()):
        instance = instance.capitalize()
    before, after = draw(_lower_phrase()), draw(_lower_phrase())
    text = " ".join(part for part in (before, instance, after) if part)
    return AttributionText(text=text, pattern=pattern)


@dataclass(frozen=True)
class AttributionVariant:
    original: AttributionText
    text: str
    kind: Kind


@st.composite
def attribution_unicode_texts(
    draw: st.DrawFn, kinds: tuple[Kind, ...] | None = None
) -> AttributionVariant:
    """Una atribución y su variante con homoglifos, espacios, ancho completo o invisibles."""
    original = draw(person_attribution_texts())
    kind: Kind = draw(
        st.sampled_from(kinds or ("homoglyph", "space", "fullwidth", "invisible", "mixed"))
    )
    return AttributionVariant(original, _disguise(draw, original.text, kind), kind)


EQUIPMENT: Final = (
    "Prensa", "Torno", "Compresor", "Caldera", "Montacargas", "Banda Transportadora", "Robot",
    "Molino", "Horno", "Grúa", "Tablero Eléctrico", "Línea de Empaque", "Bomba", "Mezclador",
)  # fmt: skip
QUALIFIERS: Final = ("Norte", "Sur", "Principal", "Central", "Oriente", "Occidente", "II", "III")
DECLARED_BRANDS: Final = (
    "Schuler Kompakt", "Krones Modulfill", "Atlas Copco", "Kaeser Sigma", "Haas Okuma",
    "Fanuc Vulcano",
)  # fmt: skip
"""Nombres de equipo de dos palabras poco comunes: solo pasan si la organización los declaró."""
_CONTROL_TAILS: Final = (
    "revisada durante el turno", "con la guarda instalada", "verificada el 2026-10-18",
    "según el procedimiento PRO-001", "conforme a NTC 2050 e ISO 45001:2018",
    "con un costo de 1.500.000 pesos", "y la placa de anclaje ajustada",
    "tras la capacitación del operario", "en la Zona Norte de la Planta Principal",
    "sin novedad en el registro", "del 18/10/2026 a las 10:30",
)  # fmt: skip


@dataclass(frozen=True)
class ZoneText:
    text: str
    declared: tuple[str, ...]
    """Los nombres de plantas y zonas declarados que el texto cita (vocabulario declarado)."""


@st.composite
def zone_equipment_texts(draw: st.DrawFn) -> ZoneText:
    """Un texto de control con zonas, equipos y máquinas (con mayúsculas) que debe aceptarse.

    Los equipos con marca de dos palabras (``DECLARED_BRANDS``) se citan con su nombre declarado.
    """
    parts: list[str] = []
    declared: list[str] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        equipment = draw(st.sampled_from(EQUIPMENT))
        if draw(st.booleans()):
            brand = draw(st.sampled_from(DECLARED_BRANDS))
            name = f"{equipment} {brand}"
            declared.append(name)
        else:
            name = f"{equipment} {draw(st.sampled_from(QUALIFIERS))}"
        if draw(st.booleans()):
            name = name.upper()
        parts.append(f"{draw(st.sampled_from(('la', 'el', 'en la zona de la')))} {name}")
    head = draw(st.sampled_from(("Revisión de", "Mantenimiento de", "Inspección de")))
    tail = draw(st.sampled_from(_CONTROL_TAILS))
    return ZoneText(text=f"{head} {' y '.join(parts)} {tail}", declared=tuple(declared))


@st.composite
def zone_equipment_unicode_texts(
    draw: st.DrawFn, kinds: tuple[Kind, ...] = ("homoglyph", "space", "fullwidth", "mixed")
) -> ZoneText:
    """Un texto de control con homoglifos, espacios no ASCII o ancho completo: debe pasar igual."""
    control = draw(zone_equipment_texts())
    text = _disguise(draw, control.text, draw(st.sampled_from(kinds)))
    return ZoneText(text=text, declared=control.declared)
