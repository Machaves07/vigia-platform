"""Gramática segura de los patrones que añade el cliente a su lista (TASK-350; LC-LAZ-16 y 17).

Un patrón de la lista corre con ``re`` (motor con retroceso) en la ruta caliente de **toda**
escritura con texto libre de la organización, dentro del proceso y sin soltar el GIL: un patrón
con retroceso exponencial o polinómico de grado alto colgaría el proceso para todas las
organizaciones que atiende (y rompería el p95 ≤ 20 ms de NFR-LAZ-04). Por eso un patrón del cliente
se acepta solo si su árbol (el del analizador de ``re``) cumple, **a cualquier profundidad**:

1. **Solo piezas conocidas**: caracteres, clases (``[...]``, ``\\w``, ``\\d``, ``\\s`` y sus
   negaciones), el punto, anclas (``^``, ``$``, ``\\b``, ``\\B``), grupos y alternancias.
   Nada de referencias atrás, búsquedas hacia delante o hacia atrás, grupos condicionales ni
   indicadores en línea (``(?i)`` cambiaría las clases que se comparan abajo).
2. **Un cuantificador solo repite un carácter**: su cuerpo es exactamente un carácter, una
   clase o el punto; nunca un grupo, una alternancia ni otro cuantificador (``(a+)+``,
   ``(a|a)+``, ``(\\w+\\s?)+``, ``(.*a){12}`` quedan fuera).
3. **Los cuantificadores van en la secuencia principal** (o en una alternativa de la alternancia
   principal), nunca dentro de un grupo.
4. **Lo que sigue a un cuantificador no comparte caracteres con él**: cada carácter que puede venir
   detrás (hasta el primer elemento que no puede faltar) es disjunto de la clase que se repite. Así
   la extensión de cada repetición queda decidida sin ambigüedad y el retroceso es lineal:
   ``zona[\\W_]*norte`` sí; ``\\w*\\w*x``, ``a+a+``, ``.*a.*b`` no.
5. **Alternancias acotadas**: el producto del número de alternativas de todas las alternancias no
   pasa de ``MAX_ALTERNATIVES``.

La disjunción se calcula sobre un alfabeto fijo (``ALPHABET``: Latín básico y extendido, signos
de puntuación generales y espacios de U+2000 a U+206F y U+3000), que cubre la forma de validación
(plegada, sin tildes y en minúsculas).

Sin red, sin base y sin reloj; el analizador es el de la biblioteca estándar (``re._parser``).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Sequence
from typing import Any, Final

__all__ = ["ALPHABET", "MAX_ALTERNATIVES", "UnsafePattern", "check_pattern"]

_PARSER: Final[Any] = importlib.import_module("re._parser")
"""El analizador de ``re`` (sin tipos públicos: se usa por nombre de código de operación)."""

MAX_ALTERNATIVES: Final = 16
"""Producto máximo de alternativas de las alternancias de un patrón `[estimación propia]`."""

ALPHABET: Final[tuple[str, ...]] = tuple(
    chr(code)
    for code in (*range(0x300), *range(0x2000, 0x2070), 0x3000)
    if not 0xD800 <= code <= 0xDFFF
)
"""Los caracteres sobre los que se decide si dos clases comparten alguno."""

_CATEGORIES: Final[dict[str, frozenset[str]]] = {
    name: frozenset(char for char in ALPHABET if re.fullmatch(expression, char))
    for name, expression in (
        ("CATEGORY_DIGIT", r"\d"),
        ("CATEGORY_NOT_DIGIT", r"\D"),
        ("CATEGORY_SPACE", r"\s"),
        ("CATEGORY_NOT_SPACE", r"\S"),
        ("CATEGORY_WORD", r"\w"),
        ("CATEGORY_NOT_WORD", r"\W"),
    )
}
_ALL: Final = frozenset(ALPHABET)
_CHARACTER_OPS: Final = frozenset({"LITERAL", "NOT_LITERAL", "ANY", "IN"})
_REPEAT_OPS: Final = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
_DEFAULT_FLAGS: Final = int(re.UNICODE)


class UnsafePattern(ValueError):
    """El patrón no cumple la gramática segura (o no compila)."""


def _name(op: object) -> str:
    return str(op)


def _class_set(items: Iterable[tuple[object, Any]]) -> frozenset[str]:
    chars: set[str] = set()
    negate = False
    for op, value in items:
        name = _name(op)
        if name == "NEGATE":
            negate = True
        elif name == "LITERAL":
            chars.add(chr(value))
        elif name == "RANGE":
            low, high = value
            chars.update(char for char in ALPHABET if low <= ord(char) <= high)
        elif name == "CATEGORY":
            category = _CATEGORIES.get(_name(value))
            if category is None:
                raise UnsafePattern("clase de caracteres no admitida")
            chars |= category
        else:
            raise UnsafePattern("pieza de una clase de caracteres no admitida")
    matched = frozenset(chars) & _ALL
    return _ALL - matched if negate else matched


def _character_set(op: object, value: Any) -> frozenset[str]:
    name = _name(op)
    if name == "LITERAL":
        return frozenset({chr(value)}) & _ALL
    if name == "NOT_LITERAL":
        return _ALL - {chr(value)}
    if name == "ANY":
        return _ALL - {"\n"}
    return _class_set(value)


def _repeated(body: Sequence[tuple[object, Any]]) -> frozenset[str]:
    """Los caracteres que repite un cuantificador, cuyo cuerpo es exactamente un carácter."""
    if len(body) != 1 or _name(body[0][0]) not in _CHARACTER_OPS:
        raise UnsafePattern("un cuantificador repite más que un carácter")
    op, value = body[0]
    return _character_set(op, value)


class _Checker:
    def __init__(self) -> None:
        self.alternatives = 1

    def _branch(self, alternatives: Sequence[Any]) -> None:
        self.alternatives *= len(alternatives)
        if self.alternatives > MAX_ALTERNATIVES:
            raise UnsafePattern("demasiadas alternativas")

    def first(self, items: Sequence[tuple[object, Any]]) -> tuple[frozenset[str], bool]:
        """Los caracteres con que puede empezar ``items`` y si puede no consumir ninguno."""
        found: set[str] = set()
        for op, value in items:
            chars, nullable = self.first_of(op, value)
            found |= chars
            if not nullable:
                return frozenset(found), False
        return frozenset(found), True

    def first_of(self, op: object, value: Any) -> tuple[frozenset[str], bool]:
        name = _name(op)
        if name in _CHARACTER_OPS:
            return _character_set(op, value), False
        if name == "AT":
            return frozenset(), True
        if name in _REPEAT_OPS:
            minimum, _maximum, body = value
            return _repeated(body.data), minimum == 0
        if name == "SUBPATTERN":
            return self.first(value[3].data)
        if name == "BRANCH":
            found: set[str] = set()
            nullable = False
            for alternative in value[1]:
                chars, empty = self.first(alternative.data)
                found |= chars
                nullable = nullable or empty
            return frozenset(found), nullable
        raise UnsafePattern("pieza del patrón no admitida")

    def group(self, items: Sequence[tuple[object, Any]]) -> None:
        """Dentro de un grupo: sin cuantificadores, a cualquier profundidad."""
        for op, value in items:
            name = _name(op)
            if name in _CHARACTER_OPS:
                _character_set(op, value)
            elif name == "AT":
                continue
            elif name == "SUBPATTERN":
                self._subpattern(value)
                self.group(value[3].data)
            elif name == "BRANCH":
                self._branch(value[1])
                for alternative in value[1]:
                    self.group(alternative.data)
            elif name in _REPEAT_OPS:
                raise UnsafePattern("un cuantificador dentro de un grupo o una alternancia")
            else:
                raise UnsafePattern("pieza del patrón no admitida")

    def _subpattern(self, value: Any) -> None:
        _group, add_flags, del_flags, _body = value
        if add_flags or del_flags:
            raise UnsafePattern("indicadores en línea no admitidos")

    def sequence(self, items: Sequence[tuple[object, Any]]) -> None:
        """La secuencia principal: los cuantificadores solo repiten un carácter y lo que les sigue
        no comparte caracteres con ellos."""
        for index, (op, value) in enumerate(items):
            name = _name(op)
            if name in _REPEAT_OPS:
                repeated = _repeated(value[2].data)
                for follower_op, follower_value in items[index + 1 :]:
                    chars, nullable = self.first_of(follower_op, follower_value)
                    if chars & repeated:
                        raise UnsafePattern("lo que sigue a un cuantificador comparte caracteres")
                    if not nullable:
                        break
            elif name == "BRANCH" and len(items) == 1:
                self._branch(value[1])
                for alternative in value[1]:
                    self.sequence(alternative.data)
            else:
                self.group([(op, value)])


def check_pattern(pattern: str) -> re.Pattern[str]:
    """El patrón compilado si cumple la gramática segura; si no, ``UnsafePattern``."""
    try:
        compiled = re.compile(pattern)
        parsed = _PARSER.parse(pattern)
    except (re.error, RecursionError, OverflowError) as error:
        raise UnsafePattern("el patrón no compila") from error
    if int(compiled.flags) != _DEFAULT_FLAGS or compiled.groupindex:
        raise UnsafePattern("indicadores en línea o grupos con nombre no admitidos")
    _Checker().sequence(list(parsed.data))
    if compiled.fullmatch("") is not None:
        raise UnsafePattern("el patrón casa con la cadena vacía")
    return compiled
