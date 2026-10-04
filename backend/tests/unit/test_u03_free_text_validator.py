"""Validador mínimo de texto libre de U-03 (D-7, RNF-OBS-03; BLM §5).

Rechaza, sobre la forma canónica de A-45, los textos que afirman intención («sabotaje»,
«manipulación deliberada») en cualquier variante de género, número, tildes, espacios Unicode,
mayúsculas, letras de ancho completo u homoglifos; acepta el resto. Registrado en
``FreeTextPolicyRegistry``, el escritor responde ``free_text_rejected`` sin repetir el texto.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.gob.u03_records import u03_registry
from vigia_platform.catalog.application.free_text_validator import (
    INTENT_ATTRIBUTION,
    U03_FREE_TEXT_VALIDATOR,
    register_u03_free_text_validator,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    LedgerRejectionCode,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.clock import SystemClock

FIELD = FreeTextField(record_type="node_revoked", path="/reason_es", min_length=1, max_length=500)


def _policy() -> FreeTextPolicyRegistry:
    registry = FreeTextPolicyRegistry()
    register_u03_free_text_validator(registry)
    registry.seal()
    return registry


POLICY = _policy()

NBSP = chr(0x00A0)
IDEOGRAPHIC_SPACE = chr(0x3000)
FIGURE_SPACE = chr(0x2007)
NARROW_NBSP = chr(0x202F)
CYRILLIC_A = chr(0x0430)
GREEK_OMICRON = chr(0x03BF)
FULLWIDTH_SABOTAJE = "".join(chr(ord(char) + 0xFEE0) for char in "Sabotaje")

REJECTED = [
    "sabotaje",
    "Sabotajes",
    "manipulación deliberada",
    f"MANIPULACION{NBSP}{NBSP}deliberada",
    f"MANIPULACION{IDEOGRAPHIC_SPACE}deliberada",
    f"manipulación{NBSP}deliberada",
    f"manipulación{IDEOGRAPHIC_SPACE}{IDEOGRAPHIC_SPACE}deliberada",
    "SABOTAJE",
    "Sabotáje en la línea 3",
    "Hubo un sabotaje del sensor.",
    "manipulaciones deliberadas del encuadre",
    "Manipulación Deliberada",
    "manipulado deliberadamente",
    "deliberadamente manipuladas",
    "manipulación intencionada",
    "saboteo",
    "saboteadora",
    "saboteadores",
    "sabotearon la cámara",
    FULLWIDTH_SABOTAJE,
    "sabotaje".replace("a", CYRILLIC_A),
    "manipulacion deliberada".replace("a", CYRILLIC_A).replace("o", GREEK_OMICRON),
    "manipulación-deliberada",
    "manipulacióndeliberada",
]
"""Los cuatro de la tarea y sus variantes de género, número, tildes, espacios y alfabetos."""

ACCEPTED = [
    "La cámara 3 quedó ocluida por una caja durante el turno",
    "Cambio del umbral de revisión por recomendación del instalador",
    "manipulación del sensor de puerta durante el mantenimiento",
    "deliberación del comité paritario",
    "sabor de la prueba",
    "sabotage",
    "Revisión deliberada del encuadre con el COPASST",
    "El operario manipula la carga con el montacargas",
]
"""Textos sin las expresiones: ninguno afirma intención."""


@pytest.mark.parametrize("text", REJECTED)
def test_texts_that_affirm_intent_are_rejected(text: str) -> None:
    with pytest.raises(FreeTextRejected) as rejected:
        POLICY.apply(text, FIELD)
    assert rejected.value.reason == INTENT_ATTRIBUTION
    assert text not in str(rejected.value)


@pytest.mark.parametrize("text", ACCEPTED)
def test_texts_without_the_expressions_are_accepted(text: str) -> None:
    assert POLICY.apply(text, FIELD) == text


def test_the_validator_is_registered_under_its_name() -> None:
    assert POLICY.validator_names == (U03_FREE_TEXT_VALIDATOR,)
    with pytest.raises(ValueError, match="ya está registrado"):
        register_u03_free_text_validator(_unsealed_with_validator())


def _unsealed_with_validator() -> FreeTextPolicyRegistry:
    registry = FreeTextPolicyRegistry()
    register_u03_free_text_validator(registry)
    return registry


# --- propiedad ------------------------------------------------------------------------------

_WORDS = st.sampled_from(
    [
        "la",
        "cámara",
        "zona",
        "turno",
        "encuadre",
        "umbral",
        "revisión",
        "señal",
        "prueba",
        "sensor",
        "puerta",
        "caja",
        "mantenimiento",
        "deliberación",
        "manipula",
        "sabor",
    ]
)
_SPACES = st.sampled_from([" ", NBSP, IDEOGRAPHIC_SPACE, FIGURE_SPACE, NARROW_NBSP, "  "])
_sentences = st.lists(st.tuples(_WORDS, _SPACES), min_size=1, max_size=12).map(
    lambda parts: "".join(word + space for word, space in parts).strip()
)


def _variant(draw: st.DrawFn, text: str) -> str:
    """Mayúsculas, tildes o espacios Unicode al azar sobre ``text``."""
    letters = []
    for char in text:
        if char == " ":
            letters.append(draw(_SPACES))
        elif draw(st.booleans()):
            letters.append(char.upper())
        else:
            letters.append(char)
    return "".join(letters)


@st.composite
def _intent_texts(draw: st.DrawFn) -> str:
    expression = draw(
        st.sampled_from(
            [
                "sabotaje",
                "sabotajes",
                "manipulación deliberada",
                "manipulacion deliberada",
                "manipulaciones deliberadas",
                "manipulación deliberado",
            ]
        )
    )
    before = draw(_sentences)
    after = draw(_sentences)
    return f"{before} {_variant(draw, expression)} {after}"


@given(_sentences)
def test_property_texts_without_the_expressions_pass(text: str) -> None:
    assert POLICY.apply(text, FIELD)


@given(_intent_texts())
def test_property_any_variant_of_the_expressions_is_rejected(text: str) -> None:
    with pytest.raises(FreeTextRejected) as rejected:
        POLICY.apply(text, FIELD)
    assert rejected.value.reason == INTENT_ATTRIBUTION


# --- el escritor responde free_text_rejected ------------------------------------------------


def test_the_ledger_writer_answers_free_text_rejected() -> None:
    compiled = u03_registry().get("node_revoked")
    writer = EscritorExpediente(
        database=cast(Any, None),
        registry=u03_registry(),
        free_text=POLICY,
        evidence=cast(Any, None),
        outbox=cast(Any, None),
        clock=SystemClock(),
    )
    document: dict[str, Any] = json.loads(
        json.dumps(
            {
                "node_id": "0190a8a0-0000-7000-8000-000000000001",
                "reason_es": "Revocado por sabotaje del equipo",
                "revoked_at": "2026-10-03T10:00:00.000Z",
                "revoked_by": "0190a8a0-0000-7000-8000-000000000002",
            }
        )
    )
    with pytest.raises(Exception) as raised:  # el escritor envuelve el rechazo en _Rejected
        writer._apply_free_text(compiled, document)
    rejection: LedgerRejection = raised.value.rejection  # type: ignore[attr-defined]
    assert rejection.code is LedgerRejectionCode.FREE_TEXT_REJECTED
    assert rejection.field == "/reason_es"
    assert "sabotaje" not in f"{raised.value} {rejection.message_es}"
