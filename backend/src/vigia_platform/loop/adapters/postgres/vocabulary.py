"""Lecturas de la lista de vocabulario por organización sobre PostgreSQL (LC-LAZ-17; TASK-303).

``PostgresVocabularyStore`` es el ``VocabularyStore`` de la caché por proceso
(``loop.validators.cache``). No abre conexiones: lee con el lector de filas de la escritura
(``FreeTextPreparation.rows``), con su contexto, así que la seguridad a nivel de fila de
``loop.blocked_vocabulary_list`` y ``loop.blocked_term`` (solo organización, A-69) decide lo que
ve: bajo una concesión de proveedor no hay fila y se valida con la lista inicial.

- ``head``: la comprobación de versión, **una sola fila** por el índice único
  ``blocked_vocabulary_list_one_per_organization``;
- ``load``: los términos vigentes en esa versión (``added_in_version ≤ v`` y sin retirar, o
  retirados después de ``v``), en orden de ``term_id``.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import text

from vigia_platform.ledger.free_text import RowReader
from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm, BlockedTermOrigin
from vigia_platform.loop.validators.cache import ListHead
from vigia_platform.loop.validators.vocabulary import VocabularyList, VocabularyTerm

__all__ = ["PostgresVocabularyStore"]

_HEAD: Final = text(
    "SELECT list_id, version, lemmatizer_version FROM loop.blocked_vocabulary_list"
    " WHERE organization_id = :organization_id"
)
_TERMS: Final = text(
    "SELECT term_id, category, form, value, suggestion_es, origin FROM loop.blocked_term"
    " WHERE organization_id = :organization_id AND list_id = :list_id"
    " AND added_in_version <= :version"
    " AND (removed_in_version IS NULL OR removed_in_version > :version)"
    " ORDER BY term_id"
)


class PostgresVocabularyStore:
    """``VocabularyStore`` de PostgreSQL."""

    async def head(self, organization_id: uuid.UUID, rows: RowReader) -> ListHead | None:
        found = await rows(_HEAD, {"organization_id": organization_id})
        if not found:
            return None
        row = found[0]
        return ListHead(
            list_id=row.list_id, version=int(row.version), lemmatizer_version=row.lemmatizer_version
        )

    async def load(
        self, organization_id: uuid.UUID, head: ListHead, rows: RowReader
    ) -> VocabularyList:
        found = await rows(
            _TERMS,
            {"organization_id": organization_id, "list_id": head.list_id, "version": head.version},
        )
        return VocabularyList(
            version=head.version,
            lemmatizer_version=head.lemmatizer_version,
            terms=tuple(
                VocabularyTerm(
                    term_id=row.term_id,
                    category=BlockedTermCategory(row.category),
                    form=BlockedTermForm(row.form),
                    value=row.value,
                    suggestion_es=row.suggestion_es,
                    origin=BlockedTermOrigin(row.origin),
                )
                for row in found
            ),
        )
