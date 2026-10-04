"""Validador mínimo de texto libre de U-03 (D-7, RNF-OBS-03; business-rules.md §11).

Ningún texto que guarde la plataforma puede **afirmar intención**: ni «sabotaje» ni
«manipulación deliberada». El nodo nunca lo afirma (sus causas son listas cerradas, RF-BOR-07) y
la plataforma tampoco deja que una persona lo escriba en un motivo, un encuadre o un texto
declarado. Mientras U-04 no registre su validador de vocabulario, este sigue aplicando junto con
la política base de U-02 (BLM §5).

Se enchufa en ``FreeTextPolicyRegistry`` (``register_u03_free_text_validator``) y busca sobre la
**forma canónica** de A-45 (NFKC, espacios Unicode como un espacio, sin tildes, en minúsculas),
plegada además con ``casefold`` y con los homoglifos cirílicos y griegos de las letras de las
dos expresiones (nota de A-45: la forma canónica no pliega confusables). Así no se esquiva con
mayúsculas, tildes, NBSP o U+3000, dígitos o letras de ancho completo ni letras de otro alfabeto
que se ven iguales. Cubre las variantes de género y número («sabotajes», «saboteadora»,
«manipulaciones deliberadas», «manipulado deliberadamente»). El rechazo lleva el motivo cerrado
``intent_attribution`` y nunca el texto; el escritor lo responde como ``free_text_rejected``.
"""

from __future__ import annotations

import re
from typing import Final

from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextRejected,
)

__all__ = [
    "INTENT_ATTRIBUTION",
    "U03_FREE_TEXT_VALIDATOR",
    "affirms_intent",
    "register_u03_free_text_validator",
    "u03_free_text_validator",
]

U03_FREE_TEXT_VALIDATOR: Final = "u03_intent_attribution"
"""Nombre con el que se registra en ``FreeTextPolicyRegistry``."""

INTENT_ATTRIBUTION: Final = "intent_attribution"
"""Motivo cerrado del rechazo (``FreeTextRejected.reason``)."""

_MESSAGE: Final = "el texto afirma intención (sabotaje o manipulación deliberada)"

_CONFUSABLES: Final = str.maketrans(
    {
        chr(code): latin
        for code, latin in (
            # cirílico
            (0x0430, "a"),
            (0x0432, "b"),
            (0x0431, "b"),
            (0x0441, "c"),
            (0x0501, "d"),
            (0x0435, "e"),
            (0x0451, "e"),
            (0x04BB, "h"),
            (0x0456, "i"),
            (0x0457, "i"),
            (0x0458, "j"),
            (0x043A, "k"),
            (0x04CF, "l"),
            (0x043C, "m"),
            (0x043F, "n"),
            (0x043E, "o"),
            (0x0440, "p"),
            (0x0455, "s"),
            (0x0442, "t"),
            (0x0443, "y"),
            (0x0445, "x"),
            # griego
            (0x03B1, "a"),
            (0x03B2, "b"),
            (0x03F2, "c"),
            (0x03B5, "e"),
            (0x03B9, "i"),
            (0x03F3, "j"),
            (0x03BA, "k"),
            (0x03BC, "m"),
            (0x03BD, "v"),
            (0x03BF, "o"),
            (0x03C1, "p"),
            (0x03C4, "t"),
            (0x03C5, "u"),
            (0x03C7, "x"),
            # latinas sin descomposición que se ven como las de las expresiones
            (0x0131, "i"),
            (0x0237, "j"),
            (0x0251, "a"),
            (0x0261, "g"),
            (0x029F, "l"),
        )
    }
)
"""Homoglifos de las letras de «sabotaje» y «manipulación deliberada» (subconjunto de los
confusables de UTS #39); se aplica sobre la forma canónica, ya sin tildes y en minúsculas."""

_GAP: Final = r"[^a-z0-9]*"
"""Entre dos palabras: cualquier separador o ninguno (espacio, guion, punto, nada)."""

_INTENT: Final = re.compile(
    r"(?<![a-z])(?:"
    # sabotaje, sabotajes; sabotear y sus formas; saboteador, saboteadora y plurales
    r"sabotaj(?:e|es)"
    r"|sabote(?:o|os|a|as|an|ar|amos|aron|ado|ada|ados|adas|ador|adora|adores|adoras)"
    # manipulación deliberada / intencionada en género y número, en los dos órdenes
    r"|manipulaci(?:on|ones)" + _GAP + r"(?:deliberad|intencionad)(?:a|as|o|os)"
    r"|manipulad(?:a|as|o|os)" + _GAP + r"(?:deliberad|intencionad)a?mente"
    r"|(?:deliberad|intencionad)a?mente" + _GAP + r"manipulad(?:a|as|o|os)"
    r"|(?:deliberad|intencionad)(?:a|as|o|os)" + _GAP + r"manipulaci(?:on|ones)"
    r")(?![a-z])"
)
"""Las expresiones de RNF-OBS-03 en la forma plegada, como palabras completas."""


def _folded(candidate: FreeTextCandidate) -> str:
    return candidate.canonical.casefold().translate(_CONFUSABLES)


def affirms_intent(candidate: FreeTextCandidate) -> bool:
    """``True`` si el texto afirma intención según RNF-OBS-03."""
    return _INTENT.search(_folded(candidate)) is not None


def u03_free_text_validator(candidate: FreeTextCandidate, field: FreeTextField) -> None:
    """Validador enchufable: ``FreeTextRejected(intent_attribution)`` si afirma intención."""
    if affirms_intent(candidate):
        raise FreeTextRejected(INTENT_ATTRIBUTION, field, _MESSAGE)


def register_u03_free_text_validator(registry: FreeTextPolicyRegistry) -> None:
    """Registra el validador mínimo de U-03 al arrancar (antes de sellar la política)."""
    registry.register(U03_FREE_TEXT_VALIDATOR, u03_free_text_validator)
