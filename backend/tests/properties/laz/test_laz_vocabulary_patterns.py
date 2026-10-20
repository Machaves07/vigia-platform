"""La gramática segura de los patrones del cliente (TASK-350; LC-LAZ-16 y 17; NFR-LAZ-04).

- **Rechazo**: los patrones con retroceso exponencial que la revisión de la ronda 1 midió (más de
  3 s sobre una frase de 41 caracteres), los de retroceso polinómico (cuantificadores seguidos de lo
  que comparte caracteres con ellos), las piezas fuera de la gramática (búsquedas, referencias
  atrás, grupos con nombre, indicadores en línea) y las alternancias que pasan del tope.
- **Aceptación**: los patrones que un cliente escribe de verdad (palabras separadas por huecos,
  cifras y porcentajes, alternativas de una palabra).
- **Tiempo**: ``find_blocked`` con la lista inicial más los patrones aceptados más caros, sobre
  textos de 1000 caracteres pensados para hacer retroceder, termina muy por debajo de un segundo
  (el objetivo de NFR-LAZ-04 es un p95 de 20 ms; aquí el tope es holgado para un runner cargado,
  porque lo que se prueba es la ausencia de retroceso exponencial o polinómico, no el p95).
- **Propiedad**: para todo patrón generado con las piezas de ``re`` (también grupos cuantificados y
  alternancias anidadas), si la gramática lo acepta, buscarlo sobre los textos adversarios termina
  dentro del mismo tope.

Solo datos generados.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm, BlockedTermOrigin
from vigia_platform.loop.validators.vocabulary import (
    VocabularyList,
    VocabularyTerm,
    compile_vocabulary,
    find_blocked,
)
from vigia_platform.loop.vocabulary.patterns import UnsafePattern, check_pattern

SECONDS: Final = 1.0
"""Tope por búsqueda: holgado (un runner cargado), muy por encima de un retroceso lineal (ms) y
muy por debajo de uno exponencial (los de la revisión pasaban de 3 s con 41 caracteres)."""

REVIEW_ROUND_1: Final = ("(\\w+\\s?)+x", "((a+))+b", "(a|a)+b", "(a+b?)+c", "(.*a){12}x")
"""Los cinco que la revisión de la ronda 1 midió por encima de 3 s."""

POLYNOMIAL: Final = (
    "\\w*\\w*x",
    "a+a+a+b",
    ".*a.*b",
    "[a-z]+[a-z]+",
    "\\w+[\\W_]*x",
    "a*a?b",
    ".+z",
    "x.*y.*",
)
"""Cuantificadores seguidos de algo que comparte caracteres con ellos."""

OUTSIDE_GRAMMAR: Final = (
    "(?=x)a",
    "a(?!b)",
    "(?<=a)b",
    "(a)\\1",
    "(?P<n>a)b",
    "(?i)zona",
    "(?i:zona)",
    "(?(1)a|b)",
    "(?>ab)",
    "(ab|c)(de|f)(gh|i)(jk|l)(mn|o)",
    "(a|(b|c)+)",
    "(sin cerrar",
    "a*",
    "",
)

ACCEPTED: Final = (
    "zona[\\W_]*norte",
    "linea[\\W_]*sur",
    "parada[\\W_]*de[\\W_]*emergencia",
    "sabotaje",
    "reduc(cion|ira)[\\W_]+del[\\W_]+\\d+ ?%",
    "cero[\\W_]+(accidentes|incidentes)",
    "(no|nunca)[\\W_]+hubo[\\W_]+nadie",
    "turno[\\W_]*\\d{1,2}",
    "[^x]*x",
    "a[\\W_]+[^\\W_].*",
)


@pytest.mark.parametrize("pattern", REVIEW_ROUND_1 + POLYNOMIAL + OUTSIDE_GRAMMAR)
def test_unsafe_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(UnsafePattern):
        check_pattern(pattern)


@pytest.mark.parametrize("pattern", ACCEPTED)
def test_the_patterns_a_customer_writes_are_accepted(pattern: str) -> None:
    assert check_pattern(pattern).pattern == pattern


ADVERSARIAL: Final = (
    "a" * 1000,
    "a " * 500,
    "ab" * 500,
    "_" * 1000,
    " " * 1000,
    "x" * 1000,
    "zona " * 200,
    "30 " * 333 + "a",
    "Revisión del resguardo de la prensa norte " * 24,
)
"""Textos de unos 1000 caracteres que hacen retroceder a un patrón inseguro."""


def _bounded(pattern: str) -> re.Pattern[str]:
    """Como lo compila el validador: entre límites de palabra (``vocabulary._bounded``)."""
    return re.compile(rf"(?<![^\W_])(?:{pattern})(?![^\W_])")


def _term(value: str) -> VocabularyTerm:
    return VocabularyTerm(
        term_id=uuid.uuid4(),
        category=BlockedTermCategory.ABSENCE_CLAIM,
        form=BlockedTermForm.PATTERN,
        value=value,
        suggestion_es="Alternativa sintética del cliente",
        origin=BlockedTermOrigin.CUSTOMER_ADDED,
    )


def test_find_blocked_over_1000_characters_stays_within_the_bound() -> None:
    compiled = compile_vocabulary(
        VocabularyList(version=2, terms=tuple(_term(value) for value in ACCEPTED))
    )
    for text in ADVERSARIAL:
        started = time.perf_counter()
        find_blocked(text, compiled)
        elapsed = time.perf_counter() - started
        assert elapsed < SECONDS, (text[:20], elapsed)


_PIECES: Final = st.sampled_from(
    ("a", "b", "x", "_", " ", "%", "\\w", "\\W", "\\d", "\\s", ".", "[\\W_]", "[a-z]", "[^x]")
)
_QUANTIFIERS: Final = st.sampled_from(("", "", "*", "+", "?", "{0,3}", "{2}", "*?"))


@st.composite
def _patterns(draw: st.DrawFn, depth: int = 0) -> str:
    parts: list[str] = []
    for _ in range(draw(st.integers(1, 4))):
        if depth < 2 and draw(st.booleans()) and draw(st.booleans()):
            alternatives = draw(st.lists(_patterns(depth + 1), min_size=1, max_size=3))
            piece = "(" + "|".join(alternatives) + ")"
        else:
            piece = draw(_PIECES)
        parts.append(piece + draw(_QUANTIFIERS))
    return "".join(parts)


@given(pattern=_patterns())
def test_every_accepted_generated_pattern_runs_in_linear_time(pattern: str) -> None:
    try:
        check_pattern(pattern)
    except UnsafePattern:
        return
    bounded = _bounded(pattern)
    for text in ADVERSARIAL:
        started = time.perf_counter()
        bounded.search(text)
        elapsed = time.perf_counter() - started
        assert elapsed < SECONDS, (pattern, text[:20], elapsed)
