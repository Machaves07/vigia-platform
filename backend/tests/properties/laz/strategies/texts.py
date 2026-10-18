"""Textos de los validadores de U-04 (TASK-303; PR-LAZ-22, 23 y 53; A-45).

- ``blocked_vocabulary_texts``: un texto con **una** flexión de un lema de la lista inicial (una
  forma del diccionario español de simplemma, con sus tildes, cuyo lema es el del término) o con
  **una** instancia de un patrón (``PATTERN_INSTANCES``: la columna «Qué cae» de BLM §14 y sus
  variantes), entre palabras del vocabulario permitido, con mayúsculas al azar. Devuelve el texto
  y el ``term_id`` que el rechazo debe nombrar.
- ``allowed_texts``: frases del vocabulario permitido (``ALLOWED_WORDS``), que ningún término
  bloquea, con mayúsculas y signos al azar.
- ``unicode_texts``: un texto bloqueado y una variante que esquiva la comparación ASCII con
  homoglifos cirílicos, espacios no ASCII (NBSP, U+3000, U+2009), dígitos y letras de ancho
  completo o invisibles (``UnicodeVariant.kind``).

Solo datos generados: ninguna persona, ningún nombre, ningún texto real.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st
from simplemma.strategies.dictionaries.dictionary_factory import DefaultDictionaryFactory

from vigia_platform.loop.domain.enums import BlockedTermForm
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.initial_list import INITIAL_TERMS, InitialTerm

__all__ = [
    "ALLOWED_WORDS",
    "PATTERN_INSTANCES",
    "BlockedText",
    "UnicodeVariant",
    "allowed_texts",
    "blocked_vocabulary_texts",
    "inflections",
    "unicode_texts",
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
    """Por término de lema, sus formas del diccionario (con tildes) cuyo lema es el guardado."""
    lemma_terms = [term for term in INITIAL_TERMS if term.form is BlockedTermForm.LEMMA]
    expansions = lexicon.expand(term.value for term in lemma_terms)  # una sola pasada
    wanted = {term.term_id: expansions[term.value].lemmas for term in lemma_terms}
    by_lemma: dict[str, set[str]] = {}
    targets = frozenset().union(*wanted.values())
    dictionary = DefaultDictionaryFactory(cache_max_size=0).get_dictionary(lexicon.LANGUAGE)
    for form, lemma in dictionary.items():
        folded = lexicon.fold_word(lemma)
        if folded in targets and form.isalpha():
            by_lemma.setdefault(folded, set()).add(form)
    result: dict[uuid.UUID, tuple[str, ...]] = {}
    for term in lemma_terms:
        forms = {term.value}.union(*(by_lemma.get(lemma, set()) for lemma in wanted[term.term_id]))
        result[term.term_id] = tuple(sorted(forms))
    return result


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
    out: list[str] = []
    changed = False
    for index, char in enumerate(blocked.text):
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
    return UnicodeVariant(blocked=blocked, text="".join(out), kind=kind)
