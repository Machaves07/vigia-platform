"""PR-LAZ-23: el plegado de validación es idempotente y el veredicto no cambia al plegar.

TASK-303 (BR-LAZ-79; A-45 y su nota del 2026-09-30). ``normalizar`` es ``validation_form`` (forma
canónica de U-02, ``casefold`` y confusables de UTS #39) y ``validar`` es el veredicto de
``blocked_vocabulary`` con la lista inicial. Para todo texto Unicode generado (cualquier plano,
marcas combinantes, mayúsculas con plegado especial, espacios e invisibles) y para los textos
bloqueados, permitidos y con homoglifos de ``tests/properties/laz/strategies/texts.py``:

- ``normalizar(normalizar(t)) = normalizar(t)``;
- ``validar(normalizar(t)) = validar(t)``: mismo término, mismo tramo, misma versión de lista.
"""

from __future__ import annotations

from typing import Final

from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.texts import (
    UnicodeVariant,
    allowed_texts,
    blocked_vocabulary_texts,
    unicode_texts,
)
from vigia_platform.ledger.free_text import FreeTextCandidate, canonical_form
from vigia_platform.loop.validators.folding import validation_form
from vigia_platform.loop.validators.vocabulary import (
    BlockedVocabularyValidator,
    BlockedVocabularyVerdict,
    initial_vocabulary,
)

VALIDATOR = BlockedVocabularyValidator(lambda _organization: initial_vocabulary())

_ANY_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=200)
"""Cualquier carácter (también sin asignar, de control o de uso privado), salvo sustitutos."""

_TRICKY_CODES: Final = (
    *(0x00DF, 0x1E9E, 0x017F, 0x0130, 0x0131, 0xFB03, 0xFB00, 0x01C5, 0x01C8, 0x0390),
    *(0x03A3, 0x03C3, 0x03C2, 0x13DA, 0xABAA, 0x13A0, 0xAB70, 0x0345),
    *(0x0301, 0x0308, 0x0327, 0x00A0, 0x3000, 0x2009, 0x200B, 0x2060, 0xFEFF, 0x00AD),
    *(0x0430, 0x0435, 0x043E, 0x0440, 0x0441, 0x0456, 0x03BF, 0xFF53, 0xFF10),
)
_TRICKY = st.text(
    alphabet=st.sampled_from((*"aAeEoOsSiI0123456789 %.,", *map(chr, _TRICKY_CODES))),
    max_size=80,
)
"""Caracteres con plegados especiales: eszett, s larga, i con punto, ligaduras, títulos, sigma
final, Cherokee (cuyo ``casefold`` sube a mayúsculas), marcas combinantes, espacios, invisibles y
homoglifos."""

TEXTS = st.one_of(
    _ANY_TEXT,
    _TRICKY,
    blocked_vocabulary_texts().map(lambda blocked: blocked.text),
    allowed_texts(),
    unicode_texts().map(lambda variant: variant.text),
)


def normalize(text: str) -> str:
    return validation_form(FreeTextCandidate(text, canonical_form(text)))


def validate(text: str) -> BlockedVocabularyVerdict | None:
    return VALIDATOR.verdict(FreeTextCandidate(text, canonical_form(text)))


@given(text=TEXTS)
def test_folding_is_idempotent(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once


@given(text=TEXTS)
def test_the_verdict_of_the_folded_text_is_the_verdict_of_the_text(text: str) -> None:
    assert validate(normalize(text)) == validate(text)


@given(variant=unicode_texts(kinds=("homoglyph", "space", "fullwidth", "mixed")))
def test_a_homoglyph_variant_folds_like_its_ascii_form(variant: UnicodeVariant) -> None:
    assert normalize(variant.text) == normalize(variant.blocked.text)
