"""Apoyo de las pruebas del validador ``blocked_vocabulary`` (TASK-303).

- ``initial_policy``: una ``FreeTextPolicyRegistry`` con el validador sobre la lista inicial, sin
  caché ni base (para las propiedades puras);
- ``MemoryStore``: un ``VocabularyStore`` en memoria que cuenta sus lecturas;
- ``seed_list``: siembra como superusuario la lista de una organización (fila de
  ``loop.blocked_vocabulary_list`` y sus ``loop.blocked_term``) en la base migrada;
- ``vocabulary_writer``: el ``EscritorExpediente`` real de ``writer_environment`` con la política
  que registra U-04 (``register_blocked_vocabulary``: validador, caché y preparador sobre
  PostgreSQL).

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from tests.identity_db import BASE_TIME, MigratedDatabase
from tests.writer_support import WriterEnvironment
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, RowReader
from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm, BlockedTermOrigin
from vigia_platform.loop.registration import register_blocked_vocabulary
from vigia_platform.loop.validators.cache import ListHead
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    VocabularyList,
    VocabularyTerm,
    initial_vocabulary,
)

__all__ = [
    "FIELD",
    "MemoryStore",
    "SeededTerm",
    "customer_term",
    "initial_policy",
    "seed_list",
    "vocabulary_writer",
]

FIELD = FreeTextField("order_probe", "/note", 1, 2000)
"""Un campo de texto libre de U-03 (los validadores de U-04 también corren sobre los suyos)."""


def initial_policy() -> FreeTextPolicyRegistry:
    """La política base más ``blocked_vocabulary`` con la lista inicial, sellada."""
    registry = FreeTextPolicyRegistry()
    registry.register(
        BLOCKED_VOCABULARY, BlockedVocabularyValidator(lambda _organization: initial_vocabulary())
    )
    registry.seal()
    return registry


def customer_term(
    value: str,
    *,
    form: BlockedTermForm = BlockedTermForm.LEMMA,
    category: BlockedTermCategory = BlockedTermCategory.ABSENCE_CLAIM,
) -> VocabularyTerm:
    return VocabularyTerm(
        term_id=uuid.uuid4(),
        category=category,
        form=form,
        value=value,
        suggestion_es="Alternativa sintética del cliente",
        origin=BlockedTermOrigin.CUSTOMER_ADDED,
    )


@dataclass
class MemoryStore:
    """``VocabularyStore`` en memoria: una lista por organización y conteo de lecturas."""

    lists: dict[uuid.UUID, VocabularyList] = field(default_factory=dict)
    heads: int = 0
    loads: int = 0

    async def head(self, organization_id: uuid.UUID, rows: RowReader) -> ListHead | None:
        self.heads += 1
        found = self.lists.get(organization_id)
        if found is None:
            return None
        return ListHead(list_id=organization_id, version=found.version, lemmatizer_version=None)

    async def load(
        self, organization_id: uuid.UUID, head: ListHead, rows: RowReader
    ) -> VocabularyList:
        self.loads += 1
        return self.lists[organization_id]


@dataclass(frozen=True)
class SeededTerm:
    """Un término sembrado en ``loop.blocked_term``."""

    value: str
    form: BlockedTermForm = BlockedTermForm.LEMMA
    added_in_version: int = 1
    removed_in_version: int | None = None
    term_id: uuid.UUID = field(default_factory=uuid.uuid4)


async def seed_list(
    migrated: MigratedDatabase,
    organization_id: uuid.UUID,
    version: int,
    terms: Sequence[SeededTerm],
    *,
    lemmatizer_version: str | None = None,
) -> uuid.UUID:
    """La lista de la organización en ``version`` con ``terms``; devuelve su ``list_id``."""
    list_id = uuid.uuid4()
    connection = await migrated.connect()
    try:
        await connection.execute(
            "INSERT INTO loop.blocked_vocabulary_list (list_id, organization_id, version,"
            " effective_from, created_by, record_id, lemmatizer_version)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            list_id,
            organization_id,
            version,
            BASE_TIME,
            json.dumps({"user_id": str(uuid.uuid4()), "display_name": "Administración sintética"}),
            uuid.uuid4(),
            lemmatizer_version,
        )
        for term in terms:
            await connection.execute(
                "INSERT INTO loop.blocked_term (term_id, list_id, organization_id, category,"
                " form, value, suggestion_es, origin, added_in_version, removed_in_version,"
                " added_reason) VALUES ($1, $2, $3, 'absence_claim', $4, $5,"
                " 'Alternativa sintética del cliente', 'customer_added', $6, $7,"
                " 'Motivo sintético del término')",
                term.term_id,
                list_id,
                organization_id,
                term.form.value,
                term.value,
                term.added_in_version,
                term.removed_in_version,
            )
    finally:
        await connection.close()
    return list_id


def vocabulary_writer(environment: WriterEnvironment) -> EscritorExpediente:
    """El escritor del entorno con la política de texto libre que registra U-04."""
    registry = FreeTextPolicyRegistry()
    register_blocked_vocabulary(registry)
    registry.seal()
    return EscritorExpediente(
        database=environment.database,
        registry=environment.registry,
        free_text=registry,
        evidence=EvidenceVerifier(environment.storage, environment.clock),
        outbox=environment.outbox,
        clock=environment.clock,
    )
