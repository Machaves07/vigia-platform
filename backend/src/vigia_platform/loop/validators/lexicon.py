"""Léxico español del validador de vocabulario: lemas y flexiones de simplemma (BR-LAZ-79).

La lista bloquea **lemas** («garantizar» cubre «garantizamos» y «garantizado») sobre la forma de
validación, que ya no lleva tildes (A-45). Un lematizador sobre palabras sin tilde falla (simplemma
lee «vacios» como desconocida), así que la comparación se hace al revés: al compilar la lista, cada
lema bloqueado se **expande** a todas sus flexiones del diccionario español de simplemma, plegadas
igual que el texto (``fold_text``), y validar es buscar cada palabra del texto en ese conjunto:
sin lematizar nada en la ruta caliente y sin cargar el diccionario mientras se valida.

- ``lemmas_of(value)``: el lema guardado de un término (BR-LAZ-79, «ya lematizado»): el propio
  valor si es un lema del diccionario («vacio», de «vacío»); si no, los lemas de las formas que
  pliegan igual («despejado» → «despejar», «disminuira» → «disminuir»); si tampoco, el valor.
- ``forms_of(value)``: el valor, sus lemas y todas las formas plegadas de esos lemas.

``LEMMATIZER_VERSION`` identifica la biblioteca y sus datos de idioma (versión de simplemma y
SHA-256 de ``es.plzma``) y se guarda en ``BlockedVocabularyList.lemmatizer_version`` (NFR-LAZ-24):
una validación pasada se reproduce con la misma pareja.

El diccionario (unas 665 000 formas, ~90 MB en memoria) se lee **solo al expandir** un valor nuevo
y se suelta después; las expansiones quedan en memoria del proceso. Expandir la lista inicial
cuesta del orden de un segundo y se hace una vez por proceso, al arrancar.

**Licencias** (NFR-LAZ-24): el código de simplemma es MIT; sus datos de idioma tienen licencias
propias (carpeta ``licenses`` de simplemma; el español deriva de ``lemmatization-lists``, Open
Database License). Constan en el PR de TASK-303 para la decisión del dueño.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from simplemma.strategies.dictionaries.dictionary_factory import (
    DATA_FOLDER,
    DefaultDictionaryFactory,
)

from vigia_platform.ledger.free_text import canonical_form
from vigia_platform.loop.validators.folding import fold_text

__all__ = [
    "LANGUAGE",
    "LEMMATIZER_VERSION",
    "Expansion",
    "fold_word",
    "forms_of",
    "lemmas_of",
]

LANGUAGE: Final = "es"
_DATA_FILE: Final = Path(DATA_FOLDER) / f"{LANGUAGE}.plzma"


def _lemmatizer_version() -> str:
    digest = hashlib.sha256(_DATA_FILE.read_bytes()).hexdigest()
    return f"simplemma {importlib.metadata.version('simplemma')} {LANGUAGE} sha256:{digest[:16]}"


LEMMATIZER_VERSION: Final = _lemmatizer_version()
"""Biblioteca y datos de idioma, para ``blocked_vocabulary_list.lemmatizer_version`` (≤ 64)."""


@functools.lru_cache(maxsize=65_536)
def fold_word(word: str) -> str:
    """Una palabra plegada como el texto que se valida (forma canónica de U-02 y ``fold_text``)."""
    if word.isascii() and " " not in word:
        return word.lower()
    return fold_text(canonical_form(word))


class Expansion:
    """Los lemas guardados de un valor y todas sus formas plegadas."""

    __slots__ = ("forms", "lemmas")

    def __init__(self, lemmas: frozenset[str], forms: frozenset[str]) -> None:
        self.lemmas = lemmas
        self.forms = forms


_LOCK: Final = threading.Lock()
_EXPANSIONS: Final[dict[str, Expansion]] = {}


def _scan(values: frozenset[str]) -> dict[str, Expansion]:
    """Dos pasadas por el diccionario: los lemas de cada valor y las formas de esos lemas."""
    # Sin caché en la fábrica: el diccionario se suelta al terminar la expansión.
    dictionary = DefaultDictionaryFactory(cache_max_size=0).get_dictionary(LANGUAGE)
    is_lemma: set[str] = set()
    candidates: dict[str, set[str]] = {value: set() for value in values}
    for raw_form, raw_lemma in dictionary.items():
        form, lemma = fold_word(raw_form), fold_word(raw_lemma)
        if lemma in values:
            is_lemma.add(lemma)
        if form in values:
            candidates[form].add(lemma)
    resolved = {
        value: frozenset({value} if value in is_lemma else candidates[value] or {value})
        for value in values
    }
    wanted = frozenset().union(*resolved.values())
    forms_by_lemma: dict[str, set[str]] = {lemma: {lemma} for lemma in wanted}
    for raw_form, raw_lemma in dictionary.items():
        lemma = fold_word(raw_lemma)
        if lemma in wanted:
            forms_by_lemma[lemma].add(fold_word(raw_form))
    return {
        value: Expansion(
            lemmas=lemmas,
            forms=frozenset({value}.union(*(forms_by_lemma[lemma] for lemma in lemmas))),
        )
        for value, lemmas in resolved.items()
    }


def expand(values: Iterable[str]) -> Mapping[str, Expansion]:
    """Las expansiones de ``values`` (ya plegados); lee el diccionario solo para los nuevos."""
    wanted = frozenset(values)
    with _LOCK:
        missing = wanted - _EXPANSIONS.keys()
        if missing:
            _EXPANSIONS.update(_scan(missing))
        return {value: _EXPANSIONS[value] for value in wanted}


def lemmas_of(value: str) -> frozenset[str]:
    """El lema guardado de un término de la lista (``value`` ya plegado)."""
    return expand((value,))[value].lemmas


def forms_of(value: str) -> frozenset[str]:
    """Todas las formas plegadas que caen con el término ``value``."""
    return expand((value,))[value].forms
