"""Flexiones del vocabulario bloqueado: una tabla propia, versionada en el repositorio (BR-LAZ-79).

La lista bloquea **lemas**: «garantizar» cae con «garantizamos» y «garantizado». No hay
lematizador de terceros. Por decisión del dueño (2026-10-18), los datos de simplemma no caben en
NFR-CTR-26 ni en NFR-LAZ-24. Las flexiones de cada lema de la lista inicial están en
``backend/resources/vocabulary/inflections.es.json``, un archivo de datos escrito para Vigía, con
su origen documentado. Contiene:

- singular y plural;
- masculino y femenino;
- los adverbios en -mente;
- la conjugación regular de los verbos de la lista.

Al compilar la lista, cada lema se **expande** a esas formas, plegadas igual que el texto
(``fold_word``: forma canónica de U-02 y ``fold_text``). Validar es buscar cada palabra del texto
en ese conjunto, sin lematizar nada en la ruta caliente.

- ``lemmas_of(value)``: el lema guardado de un término. Es su propio valor, ya plegado: la lista
  se escribe en lemas («vacio», «despejado», «disminuira»).
- ``forms_of(value)``: el valor y sus flexiones.
  - Si el lema está en la tabla, son las de la tabla.
  - Si no está (un lema que añade un cliente, TASK-350), son el valor, su plural (-s, -es) y, si
    acaba en -o o -a, el otro género y su plural. Un verbo de cliente solo cae en la forma escrita.

``LEMMATIZER_VERSION`` identifica la tabla (su versión y el SHA-256 del archivo) y se guarda en
``BlockedVocabularyList.lemmatizer_version`` (NFR-LAZ-24): una validación pasada se reproduce con
la misma tabla. Determinista: ni red ni base, y el archivo se lee una vez por proceso.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from vigia_platform.ledger.free_text import canonical_form
from vigia_platform.loop.validators.folding import fold_text

__all__ = [
    "INFLECTIONS_PATH",
    "LANGUAGE",
    "LEMMATIZER_VERSION",
    "Expansion",
    "expand",
    "fold_word",
    "forms_of",
    "inflection_table",
    "lemmas_of",
]

LANGUAGE: Final = "es"
INFLECTIONS_PATH: Final = (
    Path(__file__).resolve().parents[4]
    / "resources"
    / "vocabulary"
    / f"inflections.{LANGUAGE}.json"
)
"""La tabla de flexiones, empaquetada en la imagen con ``backend/resources``."""


@functools.lru_cache(maxsize=65_536)
def fold_word(word: str) -> str:
    """Una palabra plegada como el texto que se valida (forma canónica de U-02 y ``fold_text``)."""
    return fold_text(canonical_form(word))


@functools.cache
def _table() -> tuple[str, Mapping[str, tuple[str, ...]]]:
    raw = INFLECTIONS_PATH.read_bytes()
    document = json.loads(raw)
    if document.get("language") != LANGUAGE or not isinstance(document.get("version"), int):
        raise ValueError("la tabla de flexiones no declara su idioma y su versión")
    lemmas: dict[str, tuple[str, ...]] = {}
    for lemma, forms in document["lemmas"].items():
        folded = fold_word(lemma)
        if folded != lemma or folded in lemmas:
            raise ValueError(f"lema mal escrito o repetido en la tabla de flexiones: {lemma}")
        lemmas[lemma] = tuple(sorted({folded, *(fold_word(form) for form in forms)}))
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"vigia-inflections {document['version']} {LANGUAGE} sha256:{digest}", lemmas


def inflection_table() -> Mapping[str, tuple[str, ...]]:
    """Lema plegado → sus formas plegadas, tal como las da la tabla."""
    return _table()[1]


LEMMATIZER_VERSION: Final = _table()[0]
"""Tabla de flexiones y su contenido, para ``blocked_vocabulary_list.lemmatizer_version``."""


class Expansion:
    """Los lemas guardados de un valor y todas sus formas plegadas."""

    __slots__ = ("forms", "lemmas")

    def __init__(self, lemmas: frozenset[str], forms: frozenset[str]) -> None:
        self.lemmas = lemmas
        self.forms = forms


def _nominal(value: str) -> frozenset[str]:
    """Un lema fuera de la tabla: el valor, su plural y, en -o o -a, el otro género."""
    stems = {value}
    if value.endswith("o"):
        stems.add(value[:-1] + "a")
    elif value.endswith("a"):
        stems.add(value[:-1] + "o")
    forms = set(stems)
    for stem in stems:
        forms.add(stem + ("s" if stem[-1:] in "aeiou" else "es"))
    return frozenset(forms)


def expand(values: Iterable[str]) -> Mapping[str, Expansion]:
    """Las expansiones de ``values`` (lemas ya plegados)."""
    table = inflection_table()
    result: dict[str, Expansion] = {}
    for value in values:
        forms = frozenset(table[value]) if value in table else _nominal(value)
        result[value] = Expansion(lemmas=frozenset({value}), forms=forms)
    return result


def lemmas_of(value: str) -> frozenset[str]:
    """El lema guardado de un término de la lista (``value`` ya plegado)."""
    return expand((value,))[value].lemmas


def forms_of(value: str) -> frozenset[str]:
    """Todas las formas plegadas que caen con el término ``value``."""
    return expand((value,))[value].forms
