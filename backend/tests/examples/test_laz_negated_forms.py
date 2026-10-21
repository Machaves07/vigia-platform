"""D-9 y los casos de ejemplo de BLM §14 en las dos direcciones (TASK-303; BR-LAZ-80 y 81).

Decisión D-9 (revisión de coherencia, 2026-09-23; nota de BLM §14.1): las cuatro expresiones que
**niegan** la ausencia se aceptan, también dentro de la leyenda de U-05 («Observable no significa
zona despejada»); la excepción es cerrada: «zona despejada», «área despejada» y «la zona está
segura» siguen cayendo, igual que una afirmación junto a una negación. Además, cada ejemplo de las
columnas «Qué cae» y «Qué no cae» de §14.1 a §14.4 cae o pasa como dice el documento.

Se comparan códigos y ``term_id``, nunca subcadenas del texto (VIG-194). Solo datos generados.
"""

from __future__ import annotations

import uuid

import pytest

from tests.vocabulary_support import FIELD, initial_policy
from vigia_platform.loop.domain.enums import BlockedTermForm
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.initial_list import INITIAL_TERMS
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY_TERM,
    BlockedVocabularyRejected,
)

POLICY = initial_policy()
pytestmark = pytest.mark.laz_examples


def _lemma(value: str) -> uuid.UUID:
    """El término de lema de la lista inicial con ese valor."""
    return next(
        term.term_id
        for term in INITIAL_TERMS
        if term.form is BlockedTermForm.LEMMA and term.value == lexicon.fold_word(value)
    )


def _pattern(index: int) -> uuid.UUID:
    """El patrón ``index`` de la lista inicial, en el orden de §14.1 a §14.4."""
    patterns = [term for term in INITIAL_TERMS if term.form is BlockedTermForm.PATTERN]
    return patterns[index].term_id


DESPEJADO = _lemma("despejado")
ZONA_SEGURA = _pattern(3)

NEGATED = (
    "No significa zona despejada",
    "no equivale a zona despejada",
    "No significa zona segura",
    "no equivale a zona segura",
    "Observable no significa zona despejada",
    "Observable: NO SIGNIFICA ZONA DESPEJADA.",
    "Observable no significa zona despejada; Degradada no equivale a zona segura",
)


@pytest.mark.parametrize("text", NEGATED)
def test_the_four_negated_forms_are_accepted(text: str) -> None:
    assert POLICY.apply(text, FIELD, organization_id=uuid.uuid4()) == text


AFFIRMED = (
    ("Zona despejada", DESPEJADO),
    ("Área despejada", DESPEJADO),
    ("La zona está segura", ZONA_SEGURA),
    ("Zona despejada, no significa zona despejada", DESPEJADO),
    ("No significa zona despejada; la zona quedó despejada", DESPEJADO),
    ("no significa que la zona esté despejada", DESPEJADO),
)


@pytest.mark.parametrize(("text", "term_id"), AFFIRMED)
def test_affirmations_still_fall(text: str, term_id: uuid.UUID) -> None:
    with pytest.raises(BlockedVocabularyRejected) as raised:
        POLICY.apply(text, FIELD, organization_id=uuid.uuid4())
    assert raised.value.verdict.code == BLOCKED_VOCABULARY_TERM
    assert raised.value.verdict.term_id == term_id


FALLS = (
    # §14.1
    ("zona vacía", _lemma("vacio")),
    ("quedó vacía", _lemma("vacio")),
    ("zona despejada", _lemma("despejado")),
    ("área despejada", _lemma("despejado")),
    ("ausencia de personas", _lemma("ausencia")),
    ("ausencia de eventos", _lemma("ausencia")),
    ("no hay nadie en la zona", _pattern(0)),
    ("zona sin personas", _pattern(1)),
    ("no ocurrió nada en el turno", _pattern(2)),
    ("la zona está segura", _pattern(3)),
    ("zona libre de riesgo", _pattern(4)),
    ("cero presencias", _pattern(5)),
    ("nadie entró a la zona", _pattern(6)),
    # §14.2
    ("garantizamos", _lemma("garantizar")),
    ("garantizado", _lemma("garantizar")),
    ("garantía de detección", _lemma("garantia")),
    ("detección infalible", _lemma("infalible")),
    ("95 % de detección", _pattern(7)),
    ("precisión del 95", _pattern(8)),
    ("100 % seguro", _pattern(9)),
    ("cero accidentes", _pattern(10)),
    ("detecta todos los cruces", _pattern(11)),
    ("el sistema nunca falla", _pattern(12)),
    # §14.3
    ("reducirá los accidentes", _pattern(13)),
    ("reducción del 50 %", _pattern(14)),
    ("evitará accidentes", _pattern(15)),
    ("elimina el riesgo", _pattern(16)),
    ("disminuirá la siniestralidad", _lemma("disminuira")),
    ("mejorará la seguridad en un 30 %", _pattern(17)),
    # §14.4
    ("hubo sabotaje", _lemma("sabotaje")),
    ("manipulación deliberada", _pattern(18)),
    ("lo hizo a propósito", _pattern(19)),
    ("fue por negligencia del turno", _pattern(23)),
    ("actuó de mala fe", _pattern(20)),
    ("omisión voluntaria", _pattern(21)),
    ("no hizo caso", _pattern(22)),
)


@pytest.mark.parametrize(("text", "term_id"), FALLS, ids=[text for text, _ in FALLS])
def test_what_section_14_says_falls_falls(text: str, term_id: uuid.UUID) -> None:
    with pytest.raises(BlockedVocabularyRejected) as raised:
        POLICY.apply(text, FIELD, organization_id=uuid.uuid4())
    assert raised.value.verdict.term_id == term_id


PASSES = (
    "sin señal válida",
    "sin acuerdo",
    "sistema de gestión de seguridad",
    "95 % de cobertura observable medida en el periodo",
    "cero falsos negativos en 12 pases del acta",
    "reincidencia con tendencia decreciente medida",
    "Revisión de la guarda norte durante el turno de mantenimiento",
)


@pytest.mark.parametrize("text", PASSES)
def test_what_section_14_says_does_not_fall_passes(text: str) -> None:
    assert POLICY.apply(text, FIELD, organization_id=uuid.uuid4()) == text
