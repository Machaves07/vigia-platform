"""PR-LAZ-22: el vocabulario bloqueado se rechaza nombrando el término y nunca se persiste.

TASK-303 (BR-LAZ-79 a 81; LC-LAZ-16). Sobre la lista inicial de BLM §14.1 a §14.4:

- **todo texto generado con una flexión de un lema bloqueado** (las formas del diccionario español
  de simplemma cuyo lema es el del término, con sus tildes y mayúsculas al azar) **o con una
  instancia de un patrón** se rechaza con ``BlockedVocabularyRejected``: código
  ``blocked_vocabulary_term``, el ``term_id`` del término, el tramo que coincidió, la sugerencia y
  ``list_version = 1``; el ``detail_code`` es ``loop_blocked_vocabulary_term``;
- además, de forma exhaustiva, cada flexión de cada lema y cada instancia de cada patrón;
- **todo texto generado del vocabulario permitido se acepta** y vuelve en NFC;
- **nada rechazado se persiste** (integración, ``EscritorExpediente`` real como ``vigia_app``):
  un registro de U-03 con un término bloqueado en su texto libre responde ``free_text_rejected``
  con ``detail_code = loop_blocked_vocabulary_term`` en ``/note`` y no deja **ninguna** fila
  (registro, clave, evidencia, etiqueta, evento, auditoría ni cabeza de cadena); hacia la API es
  ``invalid_request`` con ese ``detail_code``. El mismo registro con un texto permitido se escribe.

Solo datos generados.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterator

import pytest
from hypothesis import given

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.properties.laz.strategies.texts import (
    PATTERN_INSTANCES,
    BlockedText,
    allowed_texts,
    blocked_vocabulary_texts,
    inflections,
)
from tests.vocabulary_support import FIELD, initial_policy, vocabulary_writer
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    organization_counts,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.ledger.free_text import FreeTextCandidate, canonical_form
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import BlockedTermForm
from vigia_platform.loop.validators import lexicon
from vigia_platform.loop.validators.folding import validation_form
from vigia_platform.loop.validators.initial_list import (
    INITIAL_LIST_VERSION,
    INITIAL_TERMS,
    InitialTerm,
)
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY_TERM,
    BlockedVocabularyRejected,
)
from vigia_platform.shared.api.errors import ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.context import ActorUnit

POLICY = initial_policy()


def _rejection(text: str) -> BlockedVocabularyRejected:
    with pytest.raises(BlockedVocabularyRejected) as raised:
        POLICY.apply(text, FIELD, organization_id=uuid.uuid4())
    return raised.value


@given(blocked=blocked_vocabulary_texts())
def test_every_inflection_or_pattern_instance_is_rejected_naming_the_term(
    blocked: BlockedText,
) -> None:
    rejection = _rejection(blocked.text)
    verdict = rejection.verdict
    assert verdict.code == BLOCKED_VOCABULARY_TERM
    assert verdict.term_id in blocked.term_ids
    assert verdict.list_version == INITIAL_LIST_VERSION
    form = validation_form(FreeTextCandidate(blocked.text, canonical_form(blocked.text)))
    start, end = verdict.span
    assert verdict.matched_value == form[start:end] != ""
    assert verdict.suggestion_es
    assert rejection.reason == BLOCKED_VOCABULARY_TERM
    assert rejection.detail_code == LoopDetailCode.BLOCKED_VOCABULARY_TERM.value


@pytest.mark.parametrize(
    "term", INITIAL_TERMS, ids=[f"{term.form.value}:{term.value[:40]}" for term in INITIAL_TERMS]
)
def test_each_inflection_and_each_pattern_instance_of_the_initial_list_falls(
    term: InitialTerm,
) -> None:
    pool = (
        inflections()[term.term_id]
        if term.form is BlockedTermForm.LEMMA
        else PATTERN_INSTANCES[term.value]
    )
    assert pool
    for instance in pool:
        verdict = _rejection(f"Revisión de la zona norte {instance} durante el turno").verdict
        if term.form is BlockedTermForm.PATTERN:
            assert verdict.term_id == term.term_id, instance
        else:
            assert verdict.term_id in _owners(instance), instance


def _owners(form: str) -> set[uuid.UUID]:
    folded = lexicon.fold_word(form)
    return {
        term_id
        for term_id, forms in inflections().items()
        if folded in {lexicon.fold_word(candidate) for candidate in forms}
    }


@given(text=allowed_texts())
def test_every_allowed_text_is_accepted(text: str) -> None:
    assert POLICY.apply(text, FIELD, organization_id=uuid.uuid4()) == unicodedata.normalize(
        "NFC", text
    )


# --- Integración: nada rechazado se persiste ------------------------------------------------------


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "laz_vocabulary") as migrated,
        writer_environment(migrated) as environment,
    ):
        yield environment


@pytest.mark.integration
@given(blocked=blocked_vocabulary_texts())
def test_a_rejected_text_leaves_no_row(
    environment: WriterEnvironment, blocked: BlockedText
) -> None:
    writer = vocabulary_writer(environment)
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03)
    loop, migrated = environment.loop, environment.migrated
    before = loop.run(organization_counts(migrated, place.organization_id))
    document = order_document(place, clips=0)
    document["note"] = blocked.text[:200]
    result = loop.run(writer.write(context, ORDER_TYPE, document))
    assert result == LedgerRejection.of(
        LedgerRejectionCode.FREE_TEXT_REJECTED,
        "/note",
        detail_code=LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    )
    error = from_ledger_rejection(result)
    assert (error.code, error.detail_code) == (
        ApiErrorCode.INVALID_REQUEST,
        LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    )
    after = loop.run(organization_counts(migrated, place.organization_id))
    assert after == before
    assert after["ledger.ledger_record"] == 0


@pytest.mark.integration
def test_the_same_record_with_an_allowed_text_is_written(environment: WriterEnvironment) -> None:
    writer = vocabulary_writer(environment)
    place = Place.new()
    context = unit_context(place.organization_id, ActorUnit.U03)
    document = order_document(place, clips=0)
    document["note"] = "Revisión de la guarda norte durante el turno"
    receipt = environment.loop.run(writer.write(context, ORDER_TYPE, document))
    assert isinstance(receipt, Receipt)
    counts = environment.loop.run(organization_counts(environment.migrated, place.organization_id))
    assert counts["ledger.ledger_record"] == 1
