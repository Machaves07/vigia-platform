"""La lista de vocabulario de una organización sobre PostgreSQL (DE §2.12; LC-LAZ-17; TASK-350).

``loop.blocked_vocabulary_list`` (una fila por organización: la versión vigente, el registro que
la produjo, la tabla de flexiones con la que se plegaron los lemas y el vocabulario declarado) y
``loop.blocked_term`` (los términos que añadió el cliente, con la versión en que entraron y, si se
retiraron, en la que salieron). Es la proyección de los registros ``vocabulary_list_changed``:
solo ``INSERT`` y el ``UPDATE`` de las columnas que ``laz_0029`` y ``laz_0039`` conceden a
``vigia_app``; nunca ``DELETE`` (retirar un término no borra nada, BR-LAZ-85). La lista inicial
del producto (BLM §14, ``origin = derived``) no se guarda aquí: la compila siempre el validador
(BR-LAZ-86).

**Exclusión** (``lock``): ``pg_advisory_xact_lock`` sobre ``loop_vocabulary|<organización>``
antes de leer la versión vigente. Dos cambios simultáneos se ordenan: el segundo lee la versión
que dejó el primero y escribe la siguiente. El respaldo es el ``UPDATE`` condicional
(``WHERE version = :expected``) y la unicidad de la fila por organización: dos cambios con la misma
versión nunca se confirman.

Toda sentencia nombra la organización del contexto (defensa en profundidad sobre la RLS, que en
estas dos tablas es de solo organización, A-69).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.domain.enums import (
    BlockedTermCategory,
    BlockedTermForm,
    BlockedTermOrigin,
    DeclaredNameKind,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "DeclaredName",
    "ListRow",
    "PostgresVocabularyRepository",
    "StoredTerm",
]

_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('loop_vocabulary|' || :organization_id, 0))"
)
_HEAD: Final = text(
    "SELECT list_id, version, effective_from, created_by, record_id, lemmatizer_version,"
    " declared_names FROM loop.blocked_vocabulary_list WHERE organization_id = :organization_id"
)
_CURRENT_TERMS: Final = text(
    "SELECT term_id, category, form, value, suggestion_es, origin, added_in_version"
    " FROM loop.blocked_term"
    " WHERE organization_id = :organization_id AND list_id = :list_id"
    " AND removed_in_version IS NULL ORDER BY added_in_version, term_id"
)
_TERMS_AT_VERSION: Final = text(
    "SELECT term_id, category, form, value, suggestion_es, origin, added_in_version"
    " FROM loop.blocked_term"
    " WHERE organization_id = :organization_id AND list_id = :list_id"
    " AND added_in_version <= :version"
    " AND (removed_in_version IS NULL OR removed_in_version > :version)"
    " ORDER BY added_in_version, term_id"
)
"""Los términos vigentes en la versión leída (como ``PostgresVocabularyStore``): una lectura sin
transacción que coincide con un cambio no mezcla la cabeza de una versión con los términos de la
siguiente."""
_INSERT_LIST: Final = text(
    "INSERT INTO loop.blocked_vocabulary_list (list_id, organization_id, version, effective_from,"
    " created_by, record_id, lemmatizer_version, declared_names)"
    " VALUES (:list_id, :organization_id, :version, :effective_from, CAST(:created_by AS jsonb),"
    " :record_id, :lemmatizer_version, CAST(:declared_names AS jsonb))"
)
_UPDATE_LIST: Final = text(
    "UPDATE loop.blocked_vocabulary_list SET version = :version,"
    " effective_from = :effective_from, created_by = CAST(:created_by AS jsonb),"
    " record_id = :record_id, lemmatizer_version = :lemmatizer_version,"
    " declared_names = CAST(:declared_names AS jsonb)"
    " WHERE organization_id = :organization_id AND list_id = :list_id"
    " AND version = :expected_version"
)
_INSERT_TERM: Final = text(
    "INSERT INTO loop.blocked_term (term_id, list_id, organization_id, category, form, value,"
    " suggestion_es, origin, added_in_version, removed_in_version, added_reason)"
    " VALUES (:term_id, :list_id, :organization_id, :category, :form, :value, :suggestion_es,"
    " :origin, :added_in_version, NULL, :added_reason)"
)
_REMOVE_TERMS: Final = text(
    "UPDATE loop.blocked_term SET removed_in_version = :version"
    " WHERE organization_id = :organization_id AND list_id = :list_id"
    " AND term_id = ANY(:term_ids) AND removed_in_version IS NULL"
    " AND origin = 'customer_added'"
).bindparams(bindparam("term_ids", type_=ARRAY(UUID(as_uuid=True))))


@dataclass(frozen=True, slots=True)
class StoredTerm:
    """Un término vigente que añadió el cliente (``loop.blocked_term``)."""

    term_id: uuid.UUID
    category: BlockedTermCategory
    form: BlockedTermForm
    value: str
    suggestion_es: str
    origin: BlockedTermOrigin
    added_in_version: int


@dataclass(frozen=True, slots=True)
class DeclaredName:
    """Un nombre del vocabulario declarado de la organización (``declared_names``)."""

    name_id: uuid.UUID
    kind: DeclaredNameKind
    value: str
    added_in_version: int

    def document(self) -> dict[str, Any]:
        return {
            "name_id": str(self.name_id),
            "kind": self.kind.value,
            "value": self.value,
            "added_in_version": self.added_in_version,
        }


@dataclass(frozen=True, slots=True)
class ListRow:
    """La fila de ``loop.blocked_vocabulary_list`` de una organización."""

    list_id: uuid.UUID
    organization_id: uuid.UUID
    version: int
    effective_from: datetime
    created_by: Mapping[str, Any]
    record_id: uuid.UUID
    lemmatizer_version: str | None
    declared_names: tuple[DeclaredName, ...]


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _declared(document: object) -> tuple[DeclaredName, ...]:
    items = _json(document) or []
    return tuple(
        DeclaredName(
            name_id=uuid.UUID(str(item["name_id"])),
            kind=DeclaredNameKind(item["kind"]),
            value=str(item["value"]),
            added_in_version=int(item["added_in_version"]),
        )
        for item in items
    )


def _list_row(row: Row[Any], organization_id: uuid.UUID) -> ListRow:
    return ListRow(
        list_id=uuid.UUID(str(row.list_id)),
        organization_id=organization_id,
        version=int(row.version),
        effective_from=row.effective_from,
        created_by=dict(_json(row.created_by)),
        record_id=uuid.UUID(str(row.record_id)),
        lemmatizer_version=row.lemmatizer_version,
        declared_names=_declared(row.declared_names),
    )


def _term(row: Row[Any]) -> StoredTerm:
    return StoredTerm(
        term_id=uuid.UUID(str(row.term_id)),
        category=BlockedTermCategory(row.category),
        form=BlockedTermForm(row.form),
        value=str(row.value),
        suggestion_es=str(row.suggestion_es),
        origin=BlockedTermOrigin(row.origin),
        added_in_version=int(row.added_in_version),
    )


def _list_parameters(row: ListRow) -> dict[str, Any]:
    return {
        "list_id": row.list_id,
        "organization_id": row.organization_id,
        "version": row.version,
        "effective_from": row.effective_from,
        "created_by": json.dumps(dict(row.created_by)),
        "record_id": row.record_id,
        "lemmatizer_version": row.lemmatizer_version,
        "declared_names": json.dumps([name.document() for name in row.declared_names]),
    }


def _same_organization(transaction: Transaction, organization_id: uuid.UUID) -> None:
    if organization_id != transaction.context.organization_id:
        raise ValueError("la lista es de otra organización que la transacción")


@repository
class PostgresVocabularyRepository:
    """La fila de la lista de cada organización y sus términos."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def read(self, context: ScopeContext) -> tuple[ListRow | None, tuple[StoredTerm, ...]]:
        """La fila y los términos vigentes, fuera de una transacción de escritura."""
        organization = context.organization_id
        heads = await self._database.read(context, _HEAD, {"organization_id": organization})
        if not heads:
            return None, ()
        head = _list_row(heads[0], organization)
        rows = await self._database.read(
            context,
            _TERMS_AT_VERSION,
            {"organization_id": organization, "list_id": head.list_id, "version": head.version},
        )
        return head, tuple(_term(row) for row in rows)

    async def lock(self, transaction: Transaction) -> None:
        """Exclusión entre cambios de la lista de la organización hasta el fin de la transacción."""
        await transaction.execute(
            _LOCK, {"organization_id": str(transaction.context.organization_id)}
        )

    async def current(
        self, transaction: Transaction
    ) -> tuple[ListRow | None, tuple[StoredTerm, ...]]:
        """La fila y los términos vigentes dentro de la transacción."""
        organization = transaction.context.organization_id
        result = await transaction.execute(_HEAD, {"organization_id": organization})
        found = result.first()
        if found is None:
            return None, ()
        head = _list_row(found, organization)
        rows = await transaction.execute(
            _CURRENT_TERMS, {"organization_id": organization, "list_id": head.list_id}
        )
        return head, tuple(_term(row) for row in rows.all())

    async def insert_list(self, transaction: Transaction, row: ListRow) -> None:
        """Crea la fila (el primer cambio); una organización que ya la tiene sale como
        ``IntegrityError``."""
        _same_organization(transaction, row.organization_id)
        await transaction.execute(_INSERT_LIST, _list_parameters(row))

    async def update_list(
        self, transaction: Transaction, row: ListRow, *, expected_version: int
    ) -> bool:
        """Pasa la fila a ``row`` si sigue en ``expected_version``; ``False`` si no."""
        _same_organization(transaction, row.organization_id)
        result = await transaction.execute(
            _UPDATE_LIST, {**_list_parameters(row), "expected_version": expected_version}
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def insert_terms(
        self,
        transaction: Transaction,
        list_id: uuid.UUID,
        terms: Sequence[StoredTerm],
        *,
        reason: str,
    ) -> None:
        """Los términos que añade el cliente en su versión de entrada."""
        organization = transaction.context.organization_id
        for term in terms:
            await transaction.execute(
                _INSERT_TERM,
                {
                    "term_id": term.term_id,
                    "list_id": list_id,
                    "organization_id": organization,
                    "category": term.category.value,
                    "form": term.form.value,
                    "value": term.value,
                    "suggestion_es": term.suggestion_es,
                    "origin": term.origin.value,
                    "added_in_version": term.added_in_version,
                    "added_reason": reason,
                },
            )

    async def remove_terms(
        self,
        transaction: Transaction,
        list_id: uuid.UUID,
        term_ids: Sequence[uuid.UUID],
        *,
        version: int,
    ) -> int:
        """Marca retirados en ``version`` los términos vigentes del cliente; cuántos."""
        if not term_ids:
            return 0
        result = await transaction.execute(
            _REMOVE_TERMS,
            {
                "organization_id": transaction.context.organization_id,
                "list_id": list_id,
                "term_ids": list(term_ids),
                "version": version,
            },
        )
        return int(getattr(result, "rowcount", 0) or 0)
