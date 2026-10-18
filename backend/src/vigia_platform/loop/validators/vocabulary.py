"""Validador ``blocked_vocabulary`` (LC-LAZ-16; BR-LAZ-77 a 83 y 86; PR-LAZ-22, 23 y 53).

Rechaza con **fallo cerrado** (BR-LAZ-80) todo texto libre que, en su forma de validación
(``folding.validation_form``: A-45 con ``casefold`` y confusables de UTS #39), contenga un término
de la lista vigente de su organización:

- un **lema** cae con cualquiera de sus flexiones (``lexicon``: diccionario español de simplemma,
  expandido al compilar la lista, de modo que validar no lematiza ni carga el diccionario);
- un **patrón** (``re`` de la biblioteca estándar, entre límites de palabra) cae con cualquier
  instancia.

La lista compilada (``CompiledVocabulary``) es la inicial de §14 (el mínimo, BR-LAZ-86) más lo
que la organización añadió; la da la caché por proceso (``cache.VocabularyCache``), que comprueba
la versión **al abrir la escritura** y nunca aquí: el validador no abre red ni base (PR-LAZ-53).

**D-9** (revisión de coherencia, 2026-09-23): las cuatro expresiones que **niegan** la ausencia
(«no significa zona despejada», «no equivale a zona despejada» y sus variantes con «segura») se
aceptan; la excepción es cerrada y se aplica antes de buscar, sobre la forma de validación.

El veredicto (``BlockedVocabularyVerdict``) nombra el término (``term_id`` y el tramo de la forma
de validación que coincidió), propone la alternativa (``suggestion_es``) y dice con qué lista se
validó (``list_version``). El rechazo es ``BlockedVocabularyRejected``: un ``FreeTextRejected``
con ``detail_code = loop_blocked_vocabulary_term``, que el escritor responde como
``free_text_rejected`` sin escribir nada. Las rutas exentas de §2.12 (``/added[*]/value`` y
``/removed[*]/value`` de ``vocabulary_list_changed``, BR-LAZ-83) no pasan por aquí.

**Módulo crítico** (LC-LAZ-16): se importa sin FastAPI ni SQLAlchemy.
"""

from __future__ import annotations

import functools
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from vigia_platform.ledger.free_text import FreeTextCandidate, FreeTextField, FreeTextRejected
from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm, BlockedTermOrigin
from vigia_platform.loop.record_types import VALIDATOR_EXEMPT_PATHS
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.folding import validation_form
from vigia_platform.loop.validators.initial_list import INITIAL_LIST_VERSION, INITIAL_TERMS

__all__ = [
    "BLOCKED_VOCABULARY",
    "BLOCKED_VOCABULARY_TERM",
    "DETAIL_CODE",
    "NEGATED_ABSENCE",
    "BlockedVocabularyRejected",
    "BlockedVocabularyValidator",
    "BlockedVocabularyVerdict",
    "CompiledVocabulary",
    "VocabularyCompileError",
    "VocabularyList",
    "VocabularyTerm",
    "compile_vocabulary",
    "find_blocked",
    "initial_vocabulary",
]

BLOCKED_VOCABULARY: Final = "blocked_vocabulary"
"""Nombre con el que se registra en ``FreeTextPolicyRegistry`` (BLM §9)."""

BLOCKED_VOCABULARY_TERM: Final = "blocked_vocabulary_term"
"""Código del veredicto (``loop_error_code`` de domain-entities §1.2, sin prefijo)."""

DETAIL_CODE: Final = "loop_blocked_vocabulary_term"
"""``LoopDetailCode.BLOCKED_VOCABULARY_TERM``, escrito aquí para no importar la capa HTTP
(``loop.detail_codes`` importa ``shared.api.errors``); una prueba exige que coincidan."""

_MESSAGE: Final = "el texto contiene un término del vocabulario bloqueado (RF-PLA-15)"
"""Mensaje genérico: no repite el texto ni el término."""

_WORD: Final = re.compile(r"[^\W_]+")

NEGATED_ABSENCE: Final = re.compile(
    r"(?<![^\W_])no[\W_]+(?:significa|equivale[\W_]+a)[\W_]+zona[\W_]+(?:despejada|segura)"
    r"(?![^\W_])"
)
"""Las cuatro expresiones de D-9, cerradas, sobre la forma de validación."""


@dataclass(frozen=True)
class VocabularyTerm:
    """Un ``BlockedTerm`` vigente (domain-entities §2.12), con su valor ya plegado."""

    term_id: uuid.UUID
    category: BlockedTermCategory
    form: BlockedTermForm
    value: str
    suggestion_es: str
    origin: BlockedTermOrigin


@dataclass(frozen=True)
class VocabularyList:
    """La lista de una organización en una versión: lo que se lee de la base (o la inicial)."""

    version: int
    terms: tuple[VocabularyTerm, ...]
    lemmatizer_version: str | None = None
    """El de ``BlockedVocabularyList`` (NFR-LAZ-24); ``None`` si la lista no lo registró."""


class VocabularyCompileError(ValueError):
    """La lista no se puede compilar (un patrón inválido): la caché conserva la anterior."""


@dataclass(frozen=True)
class CompiledVocabulary:
    """Una lista lista para validar: formas plegadas y patrones compilados."""

    list_version: int
    lemmatizer_version: str
    words: Mapping[str, VocabularyTerm]
    patterns: tuple[tuple[re.Pattern[str], VocabularyTerm], ...]
    terms: tuple[VocabularyTerm, ...]
    order: Mapping[uuid.UUID, int]
    """Posición de cada término en la lista: desempata dos coincidencias que empiezan igual."""


@dataclass(frozen=True)
class BlockedVocabularyVerdict:
    """``TextValidationResult`` de un rechazo (domain-entities §2.12)."""

    term_id: uuid.UUID
    span: tuple[int, int]
    """Tramo ``[inicio, fin)`` de la forma de validación que coincidió."""
    matched_value: str
    suggestion_es: str
    list_version: int
    code: Literal["blocked_vocabulary_term"] = BLOCKED_VOCABULARY_TERM


class BlockedVocabularyRejected(FreeTextRejected):
    """Rechazo del vocabulario bloqueado, con su veredicto (que nunca llega al expediente)."""

    def __init__(self, field: FreeTextField, verdict: BlockedVocabularyVerdict) -> None:
        super().__init__(
            BLOCKED_VOCABULARY_TERM,
            field,
            _MESSAGE,
            detail_code=DETAIL_CODE,
        )
        self.verdict = verdict


def _initial_terms() -> tuple[VocabularyTerm, ...]:
    return tuple(
        VocabularyTerm(
            term_id=term.term_id,
            category=term.category,
            form=term.form,
            value=term.value,
            suggestion_es=term.suggestion_es,
            origin=BlockedTermOrigin.DERIVED,
        )
        for term in INITIAL_TERMS
    )


def _bounded(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(rf"(?<![^\W_])(?:{pattern})(?![^\W_])")
    except re.error as error:
        raise VocabularyCompileError("un patrón de la lista no compila") from error


def compile_vocabulary(vocabulary: VocabularyList) -> CompiledVocabulary:
    """Compila la lista inicial (siempre, BR-LAZ-86) más los términos de ``vocabulary``.

    Un término repetido (misma forma y mismo valor plegado) cuenta una vez, con el ``term_id``
    del primero: la lista inicial va primero. Expandir lemas nuevos lee el diccionario (CPU): la
    caché lo llama fuera del bucle de eventos.
    """
    seen: set[tuple[BlockedTermForm, str]] = set()
    terms: list[VocabularyTerm] = []
    for term in (*_initial_terms(), *vocabulary.terms):
        value = (
            term.value if term.form is BlockedTermForm.PATTERN else lexicon.fold_word(term.value)
        )
        key = (term.form, value)
        if key in seen:
            continue
        seen.add(key)
        terms.append(term if value == term.value else _with_value(term, value))
    lemmas = [term.value for term in terms if term.form is BlockedTermForm.LEMMA]
    expansions = lexicon.expand(lemmas)
    words: dict[str, VocabularyTerm] = {}
    patterns: list[tuple[re.Pattern[str], VocabularyTerm]] = []
    for term in terms:
        if term.form is BlockedTermForm.LEMMA:
            for form in sorted(expansions[term.value].forms):
                words.setdefault(form, term)
        else:
            patterns.append((_bounded(term.value), term))
    return CompiledVocabulary(
        list_version=vocabulary.version,
        lemmatizer_version=vocabulary.lemmatizer_version or lexicon.LEMMATIZER_VERSION,
        words=words,
        patterns=tuple(patterns),
        terms=tuple(terms),
        order={term.term_id: index for index, term in enumerate(terms)},
    )


def _with_value(term: VocabularyTerm, value: str) -> VocabularyTerm:
    return VocabularyTerm(
        term_id=term.term_id,
        category=term.category,
        form=term.form,
        value=value,
        suggestion_es=term.suggestion_es,
        origin=term.origin,
    )


@functools.cache
def initial_vocabulary() -> CompiledVocabulary:
    """La lista inicial compilada (versión 1), una vez por proceso."""
    return compile_vocabulary(VocabularyList(version=INITIAL_LIST_VERSION, terms=()))


def _masked(form: str) -> str:
    """La forma con las negaciones de D-9 tapadas por espacios (los tramos no se mueven)."""
    return NEGATED_ABSENCE.sub(lambda found: " " * len(found.group()), form)


def find_blocked(form: str, compiled: CompiledVocabulary) -> BlockedVocabularyVerdict | None:
    """El primer término de ``compiled`` en la forma de validación ``form``, o ``None``.

    «Primero» es el que empieza antes; a igual inicio, el que va antes en la lista.
    """
    masked = _masked(form)
    order = compiled.order
    best: tuple[int, int, int, VocabularyTerm] | None = None
    for word in _WORD.finditer(masked):
        term = compiled.words.get(word.group())
        if term is not None:
            best = _earliest(best, (word.start(), order[term.term_id], word.end(), term))
            break  # las palabras salen en orden: la primera que cae es la más temprana
    for pattern, term in compiled.patterns:
        found = pattern.search(masked)
        if found is not None:
            best = _earliest(best, (found.start(), order[term.term_id], found.end(), term))
    if best is None:
        return None
    start, _, end, term = best
    return BlockedVocabularyVerdict(
        term_id=term.term_id,
        span=(start, end),
        matched_value=form[start:end],
        suggestion_es=term.suggestion_es,
        list_version=compiled.list_version,
    )


def _earliest(
    best: tuple[int, int, int, VocabularyTerm] | None,
    candidate: tuple[int, int, int, VocabularyTerm],
) -> tuple[int, int, int, VocabularyTerm]:
    if best is None or candidate[:2] < best[:2]:
        return candidate
    return best


class BlockedVocabularyValidator:
    """El validador enchufable: memoria y CPU, nunca red ni base (PR-LAZ-53).

    ``lists`` da la lista compilada vigente de una organización (la de la caché por proceso; la
    inicial si la organización es ``None`` o aún no se cargó).
    """

    def __init__(self, lists: Callable[[uuid.UUID | None], CompiledVocabulary]) -> None:
        self._lists = lists

    def verdict(self, candidate: FreeTextCandidate) -> BlockedVocabularyVerdict | None:
        """El veredicto de ``candidate`` con la lista de su organización (``None``: aceptado)."""
        return find_blocked(validation_form(candidate), self._lists(candidate.organization_id))

    def __call__(self, candidate: FreeTextCandidate, field: FreeTextField) -> None:
        if field.path in VALIDATOR_EXEMPT_PATHS.get(field.record_type, ()):
            return  # BR-LAZ-83: el término que se prohíbe no se valida contra la lista
        verdict = self.verdict(candidate)
        if verdict is not None:
            raise BlockedVocabularyRejected(field, verdict)
