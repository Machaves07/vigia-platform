"""Plegado de validación de U-04 y piezas del validador ``blocked_vocabulary`` (TASK-303; A-45).

- ``casefold``: «Straße» y «STRASSE» pliegan igual; la i con punto y la s larga también;
- confusables de UTS #39: las letras cirílicas y griegas que se ven como latinas pasan a las
  latinas; lo ASCII no se pliega (la tabla lleva «m» → «rn» y «0» → «O»); la tabla empaquetada
  declara su versión y su licencia Unicode;
- **A-45**: un texto bloqueado con homoglifos cirílicos, espacios no ASCII, dígitos y letras de
  ancho completo se rechaza con el **mismo término** que su forma ASCII; con invisibles lo rechaza
  la política base de U-02 (``invisible_character``), antes de llegar al validador;
- las rutas exentas de §2.12 (``/added[*]/value`` de ``vocabulary_list_changed``) no se validan;
- el ``detail_code`` del validador es ``LoopDetailCode.BLOCKED_VOCABULARY_TERM`` y el error de la
  API es ``invalid_request`` con él;
- ninguna sugerencia de la lista inicial contiene un término bloqueado; los ``term_id`` son
  estables y únicos; ``LEMMATIZER_VERSION`` cabe en ``blocked_vocabulary_list``;
- el módulo crítico se importa sin FastAPI ni SQLAlchemy (LC-LAZ-16).

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import uuid

import pytest
from hypothesis import given

from tests.properties.laz.strategies.texts import UnicodeVariant, unicode_texts
from tests.vocabulary_support import FIELD, initial_policy
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextRejected,
    canonical_form,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.folding import (
    CONFUSABLES_PATH,
    UNICODE_SECURITY_VERSION,
    confusable_map,
    validation_form,
)
from vigia_platform.loop.validators.initial_list import INITIAL_TERMS
from vigia_platform.loop.validators.vocabulary import (
    DETAIL_CODE,
    BlockedVocabularyRejected,
    BlockedVocabularyValidator,
    initial_vocabulary,
)
from vigia_platform.shared.api.errors import ApiErrorCode, from_ledger_rejection

POLICY = initial_policy()
VALIDATOR = BlockedVocabularyValidator(lambda _organization: initial_vocabulary())


def fold(text: str) -> str:
    return validation_form(FreeTextCandidate(text, canonical_form(text)))


def test_strasse_with_eszett_and_strasse_fold_the_same() -> None:
    eszett = "Stra" + chr(0x00DF) + "e"  # «Straße»
    assert fold(eszett) == fold("STRASSE") == fold("strasse") == "strasse"
    assert canonical_form(eszett) != canonical_form("STRASSE")  # U-02 sola no basta (A-45)


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(char) - 0x21 + 0xFF01) for char in text)


_CYRILLIC_A, _CYRILLIC_E, _CYRILLIC_O, _CYRILLIC_DZE = map(chr, (0x0430, 0x0435, 0x043E, 0x0455))
_GREEK_OMICRON, _NBSP, _IDEOGRAPHIC_SPACE = map(chr, (0x03BF, 0x00A0, 0x3000))

EXAMPLES = (
    (chr(0x0130) + "stanbul", "istanbul"),
    (chr(0x017F) + "abotaje", "sabotaje"),
    ("Stra" + chr(0x00DF) + "e", "strasse"),
    ("s" + _CYRILLIC_A + "b" + _CYRILLIC_O + "t" + _CYRILLIC_A + "je", "sabotaje"),
    (
        _CYRILLIC_DZE + _CYRILLIC_A + "b" + _GREEK_OMICRON + "t" + _CYRILLIC_A + "j" + _CYRILLIC_E,
        "sabotaje",
    ),
    (_fullwidth("SABOTAJE"), "sabotaje"),
    (_fullwidth("100") + _NBSP + "%" + _IDEOGRAPHIC_SPACE + "seguro", "100 % seguro"),
    ("modo 0 y 1, rn", "modo 0 y 1, rn"),
)


@pytest.mark.parametrize(("text", "folded"), EXAMPLES)
def test_validation_form_examples(text: str, folded: str) -> None:
    assert fold(text) == folded
    assert fold(fold(text)) == fold(text)


def test_ascii_is_never_folded_by_the_confusables_table() -> None:
    table = confusable_map()
    assert all(code >= 0x80 for code in table)
    assert all(value.isascii() and value == value.lower() for value in table.values())
    assert table[0x0430] == "a" and table[0x043E] == "o" and table[0x03BF] == "o"


def test_the_confusables_table_is_packaged_with_its_version_and_license() -> None:
    header = CONFUSABLES_PATH.read_text(encoding="utf-8").splitlines()[:12]
    assert "# Unicode Security Mechanisms for UTS #39" in header
    assert f"# Version: {UNICODE_SECURITY_VERSION}" in header
    assert UNICODE_SECURITY_VERSION.count(".") == 2
    license_text = (CONFUSABLES_PATH.parent / "LICENSE-Unicode.txt").read_text(encoding="utf-8")
    assert license_text.splitlines()[0] == "UNICODE LICENSE V3"


@given(variant=unicode_texts(kinds=("homoglyph", "space", "fullwidth", "mixed")))
def test_a_unicode_variant_is_rejected_with_the_same_term_as_its_ascii_form(
    variant: UnicodeVariant,
) -> None:
    def term(text: str) -> uuid.UUID:
        with pytest.raises(BlockedVocabularyRejected) as raised:
            POLICY.apply(text, FIELD, organization_id=uuid.uuid4())
        return raised.value.verdict.term_id

    assert term(variant.text) == term(variant.blocked.text)


@given(variant=unicode_texts(kinds=("invisible",)))
def test_a_variant_with_invisibles_is_rejected_by_the_base_policy(variant: UnicodeVariant) -> None:
    with pytest.raises(FreeTextRejected) as raised:
        POLICY.apply(variant.text, FIELD, organization_id=uuid.uuid4())
    assert raised.value.reason == "invisible_character"


def test_exempt_paths_are_not_validated() -> None:
    term = FreeTextField("vocabulary_list_changed", "/added[*]/value", 1, 256)
    reason = FreeTextField("vocabulary_list_changed", "/reason", 1, 500)
    VALIDATOR(FreeTextCandidate("sabotaje", "sabotaje"), term)
    removed = dataclasses.replace(term, path="/removed[*]/value")
    VALIDATOR(FreeTextCandidate("sabotaje", "sabotaje"), removed)
    other_type = dataclasses.replace(term, record_type="classification")
    with pytest.raises(BlockedVocabularyRejected):
        VALIDATOR(FreeTextCandidate("sabotaje", "sabotaje"), other_type)
    with pytest.raises(BlockedVocabularyRejected):
        VALIDATOR(FreeTextCandidate("sabotaje", "sabotaje"), reason)


def test_the_detail_code_is_the_one_of_the_loop_and_reaches_the_api() -> None:
    assert LoopDetailCode.BLOCKED_VOCABULARY_TERM.value == DETAIL_CODE
    rejection = LedgerRejection.of(
        LedgerRejectionCode.FREE_TEXT_REJECTED, "/note", detail_code=DETAIL_CODE
    )
    error = from_ledger_rejection(rejection)
    assert (error.code, error.detail_code) == (ApiErrorCode.INVALID_REQUEST, DETAIL_CODE)
    base = from_ledger_rejection(LedgerRejection.of(LedgerRejectionCode.FREE_TEXT_REJECTED))
    assert base.detail_code is None


def test_no_suggestion_of_the_initial_list_is_blocked() -> None:
    for term in INITIAL_TERMS:
        assert POLICY.apply(term.suggestion_es, FIELD) == term.suggestion_es
        assert 1 <= len(term.suggestion_es) <= 200


def test_initial_term_ids_are_stable_and_unique() -> None:
    ids = [term.term_id for term in INITIAL_TERMS]
    assert len(set(ids)) == len(ids)
    assert all(term_id.version == 5 for term_id in ids)
    # Fijado: la siembra de la versión 1 en la base (TASK-350) usa el mismo identificador.
    assert (INITIAL_TERMS[0].value, ids[0]) == (
        "vacio",
        uuid.UUID("14cc5d83-1ab1-5d43-a220-603f34ef784c"),
    )


def test_the_lemmatizer_version_fits_the_list_column() -> None:
    version = lexicon.LEMMATIZER_VERSION
    assert 1 <= len(version) <= 64
    name, number, language, digest = version.split(" ")
    assert (name, language) == ("simplemma", "es") and number.count(".") == 2
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 16


def test_lemmas_are_stored_already_lemmatized() -> None:
    assert lexicon.lemmas_of("vacio") == {"vacio"}
    assert lexicon.lemmas_of("despejado") == {"despejar"}
    assert lexicon.lemmas_of("disminuira") == {"disminuir"}
    assert {"garantizamos", "garantizado", "garantice"} <= lexicon.forms_of("garantizar")


def test_the_critical_modules_import_without_fastapi_or_sqlalchemy() -> None:
    code = (
        "import sys\n"
        "import vigia_platform.loop.validators\n"
        "import vigia_platform.loop.validators.cache\n"
        "bad = sorted(m for m in ('fastapi', 'sqlalchemy') if m in sys.modules)\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == ""
