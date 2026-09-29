"""Política base de texto libre (domain-entities §4.6; BR-NUC-44).

Criterio de aceptación 3: un texto con caracteres de control o marcado se rechaza; uno con
acentos en NFD sale normalizado a NFC. Además: longitudes del esquema, caracteres invisibles y
validadores enchufados de otras unidades.
"""

from __future__ import annotations

import re
import types
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.ledger import free_text
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextRejected,
    FreeTextRejection,
    apply_base_policy,
)

FIELD = FreeTextField(record_type="zone_created", path="/name", min_length=1, max_length=120)


def _reason(text: str, field: FreeTextField = FIELD) -> str:
    with pytest.raises(FreeTextRejected) as raised:
        apply_base_policy(text, field)
    assert raised.value.field is field
    return raised.value.reason


# --- criterio de aceptación 3 --------------------------------------------------------------


def test_nfd_accents_come_out_in_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "Zona de envasado — línea Ñandú")
    assert decomposed != unicodedata.normalize("NFC", decomposed)
    result = apply_base_policy(decomposed, FIELD)
    assert result == "Zona de envasado — línea Ñandú"
    assert unicodedata.is_normalized("NFC", result)


@pytest.mark.parametrize(
    "text",
    [
        "Zona\x00norte",
        "Zona\x07norte",
        "Zona\tnorte",
        "Zona\nnorte",
        "Zona\rnorte",
        "Zona\x1b[31mnorte",  # secuencia ANSI
        "Zona\x7fnorte",
        "Zona\x85norte",  # NEL (C1)
        "Zona\u2028norte",  # separador de línea
        "Zona\u2029norte",  # separador de párrafo
    ],
)
def test_control_characters_are_rejected(text: str) -> None:
    assert _reason(text) == FreeTextRejection.CONTROL_CHARACTER


@pytest.mark.parametrize(
    "text",
    [
        "<b>Zona</b>",
        "Zona <script>alert(1)</script>",
        "Zona</p>",
        "Zona <!-- nota -->",
        "<?xml version='1.0'?>",
        "<img src=x onerror=y>",
        "Zona &lt;b&gt;",
        "Zona &#60;b",
        "Zona &#x3C;b",
        "Zona &amp; almacén",
        "Zona \uff1cb\uff1e",  # «<b>» de ancho completo: NFKC lo convierte en marcado
    ],
)
def test_markup_and_markup_escapes_are_rejected(text: str) -> None:
    assert _reason(text) == FreeTextRejection.MARKUP


@pytest.mark.parametrize(
    "text",
    [
        "a < b",
        "3 > 2 y 2 < 3",
        "<3",
        "Zona A & B",
        "R&D",
        "Área 51 — acceso restringido",
        "Planta «Norte» (línea 2)",
    ],
)
def test_ordinary_punctuation_is_accepted(text: str) -> None:
    assert apply_base_policy(text, FIELD) == text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Zona\u200bnorte", FreeTextRejection.INVISIBLE_CHARACTER),  # anchura cero
        ("Zona\u202enorte", FreeTextRejection.INVISIBLE_CHARACTER),  # inversión de dirección
        ("Zona\ufeffnorte", FreeTextRejection.INVISIBLE_CHARACTER),
        ("Zona\ue000norte", FreeTextRejection.DISALLOWED_CHARACTER),  # uso privado
        ("Zona\ufffenorte", FreeTextRejection.DISALLOWED_CHARACTER),  # no carácter
        ("Zona\ud800norte", FreeTextRejection.DISALLOWED_CHARACTER),  # sustituto suelto
    ],
)
def test_invisible_and_disallowed_characters_are_rejected(text: str, reason: str) -> None:
    assert _reason(text) == reason


def test_length_limits_are_those_of_the_schema_and_measured_after_nfc() -> None:
    field = FreeTextField(record_type="t", path="/reason", min_length=10, max_length=12)
    assert _reason("corto", field) == FreeTextRejection.TOO_SHORT
    assert apply_base_policy("a" * 10, field) == "a" * 10
    assert apply_base_policy("a" * 12, field) == "a" * 12
    assert _reason("a" * 13, field) == FreeTextRejection.TOO_LONG
    # 12 letras con tilde en NFD son 24 puntos de código, pero 12 caracteres tras NFC.
    nfd = unicodedata.normalize("NFD", "á" * 12)
    assert len(nfd) == 24
    assert apply_base_policy(nfd, field) == "á" * 12
    assert _reason("", FIELD) == FreeTextRejection.TOO_SHORT


def test_giant_text_is_rejected_before_normalizing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(form: str, text: str) -> str:
        raise AssertionError("no debe normalizar un texto gigante")

    stub = types.SimpleNamespace(normalize=fail, category=unicodedata.category)
    monkeypatch.setattr(free_text, "unicodedata", stub)
    assert _reason("a" * 5_000_000) == FreeTextRejection.TOO_LONG


def test_non_string_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        apply_base_policy(123, FIELD)  # type: ignore[arg-type]


def test_field_limits_must_be_coherent() -> None:
    with pytest.raises(ValueError, match="incoherentes"):
        FreeTextField(record_type="t", path="/x", min_length=5, max_length=4)


# --- registro de validadores (U-04 enchufa el suyo) ------------------------------------------


def test_plugged_validators_run_after_the_base_policy_on_nfc_text() -> None:
    seen: list[str] = []

    def vocabulary(text: str, field: FreeTextField) -> None:
        seen.append(text)
        if "prohibida" in text:
            raise FreeTextRejected("blocked_vocabulary", field, "vocabulario bloqueado")

    registry = FreeTextPolicyRegistry()
    registry.register("vocabulary", vocabulary)
    assert registry.validator_names == ("vocabulary",)
    decomposed = unicodedata.normalize("NFD", "línea")
    assert registry.apply(decomposed, FIELD) == "línea"
    assert seen == ["línea"]
    with pytest.raises(FreeTextRejected) as raised:
        registry.apply("palabra prohibida", FIELD)
    assert raised.value.reason == "blocked_vocabulary"
    # La política base aplica antes: el validador no llega a ver el marcado.
    with pytest.raises(FreeTextRejected) as raised:
        registry.apply("<b>x</b>", FIELD)
    assert raised.value.reason == FreeTextRejection.MARKUP
    assert seen == ["línea", "palabra prohibida"]


def test_base_policy_applies_without_any_validator() -> None:
    registry = FreeTextPolicyRegistry()
    with pytest.raises(FreeTextRejected):
        registry.apply("<b>x</b>", FIELD)


def test_validator_registry_is_closed_after_sealing_and_rejects_duplicates() -> None:
    registry = FreeTextPolicyRegistry()
    registry.register("vocabulary", lambda text, field: None)
    with pytest.raises(ValueError, match="ya está registrado"):
        registry.register("vocabulary", lambda text, field: None)
    registry.seal()
    with pytest.raises(RuntimeError, match="sellada"):
        registry.register("other", lambda text, field: None)


# --- propiedades ---------------------------------------------------------------------------


@given(st.text(max_size=150))
def test_accepted_text_is_nfc_idempotent_and_clean(text: str) -> None:
    try:
        result = apply_base_policy(text, FIELD)
    except FreeTextRejected:
        return
    assert unicodedata.is_normalized("NFC", result)
    assert apply_base_policy(result, FIELD) == result
    assert 1 <= len(result) <= FIELD.max_length
    assert all(unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Co", "Cn"} for c in result)
    assert re.search(r"<[A-Za-z/!?]", unicodedata.normalize("NFKC", result)) is None


@given(
    prefix=st.text(max_size=20),
    control=st.characters(categories=["Cc"]),
    suffix=st.text(max_size=20),
)
def test_any_control_character_is_rejected(prefix: str, control: str, suffix: str) -> None:
    with pytest.raises(FreeTextRejected):
        apply_base_policy(prefix + control + suffix, FIELD)
