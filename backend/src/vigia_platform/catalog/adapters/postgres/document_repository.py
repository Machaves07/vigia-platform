"""``catalog.document_upload_grant`` sobre PostgreSQL (LC-GOB-05; tabla de ``gob_0017``).

Toda sentencia va con el ``ScopeContext`` de la operación: la seguridad a nivel de fila filtra
por organización y por concesión de proveedor (``provider_concession_scope``), y además cada
consulta lleva **explícitos** ``organization_id`` y ``plant_id`` (la planta de la operación): una
concesión de otra planta no se ve aunque la política de fila la dejara pasar.

``mark_used`` es la única transición que escribe esta tarea: un ``UPDATE`` **condicional**
(``status = 'issued'``) que solo devuelve las filas que cambió. Dos registros simultáneos que
citan el mismo documento se serializan en el bloqueo de la fila; el segundo vuelve a evaluar la
condición, ya no la cumple y no cambia nada. La base custodia además la transición
(``catalog.guard_update``: solo ``issued → used`` e ``issued → expired``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.documents import (
    DocumentContentType,
    DocumentUploadGrant,
)
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresDocumentGrants"]

_ACTIVE_PLANT: Final = text(
    "SELECT p.plant_id FROM identity.plant AS p"
    " WHERE p.organization_id = :organization_id AND p.plant_id = :plant_id"
    " AND p.status = 'active'"
)

_INSERT: Final = text(
    "INSERT INTO catalog.document_upload_grant (document_id, organization_id, plant_id, kind,"
    " content_type, storage_key, sha256, size_bytes, issued_at, expires_at, status)"
    " VALUES (:document_id, :organization_id, :plant_id, :kind, :content_type, :storage_key,"
    " :sha256, :size_bytes, :issued_at, :expires_at, :status)"
)

_BY_IDS: Final = text(
    "SELECT g.document_id, g.organization_id, g.plant_id, g.kind, g.content_type,"
    " g.storage_key, g.sha256, g.size_bytes, g.issued_at, g.expires_at, g.status"
    " FROM catalog.document_upload_grant AS g"
    " WHERE g.organization_id = :organization_id AND g.plant_id = :plant_id"
    " AND g.document_id = ANY(CAST(:document_ids AS uuid[]))"
)

_MARK_USED: Final = text(
    "UPDATE catalog.document_upload_grant SET status = 'used'"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND document_id = ANY(CAST(:document_ids AS uuid[])) AND status = 'issued'"
    " RETURNING document_id"
)


def _plain(value: uuid.UUID) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(int=value.int)


def _grant(row: Row[Any]) -> DocumentUploadGrant:
    issued_at: datetime = row.issued_at
    expires_at: datetime = row.expires_at
    return DocumentUploadGrant(
        document_id=_plain(row.document_id),
        organization_id=_plain(row.organization_id),
        plant_id=_plain(row.plant_id),
        kind=DocumentKind(row.kind),
        content_type=DocumentContentType(row.content_type),
        storage_key=str(row.storage_key),
        sha256=str(row.sha256),
        size_bytes=int(row.size_bytes),
        issued_at=issued_at,
        expires_at=expires_at,
        status=UploadGrantStatus(row.status),
    )


@repository
class PostgresDocumentGrants:
    """Concesiones de subida de documentos de una planta."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def active_plant(self, context: ScopeContext, plant_id: uuid.UUID) -> bool:
        """¿Existe la planta, activa, en la organización y al alcance del contexto?"""
        rows = await self._database.read(
            context,
            _ACTIVE_PLANT,
            {"organization_id": context.organization_id, "plant_id": plant_id},
        )
        return bool(rows)

    async def insert(self, transaction: Transaction, grant: DocumentUploadGrant) -> None:
        """Alta de la concesión en ``issued`` dentro de ``transaction``."""
        await transaction.execute(
            _INSERT,
            {
                "document_id": grant.document_id,
                "organization_id": grant.organization_id,
                "plant_id": grant.plant_id,
                "kind": grant.kind.value,
                "content_type": grant.content_type.value,
                "storage_key": grant.storage_key,
                "sha256": grant.sha256,
                "size_bytes": grant.size_bytes,
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "status": grant.status.value,
            },
        )

    async def by_ids(
        self, context: ScopeContext, plant_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, DocumentUploadGrant]:
        """Las concesiones de ``document_ids`` de esa planta y organización (las demás, no)."""
        if not document_ids:
            return {}
        rows = await self._database.read(
            context,
            _BY_IDS,
            {
                "organization_id": context.organization_id,
                "plant_id": plant_id,
                "document_ids": list(document_ids),
            },
        )
        grants = (_grant(row) for row in rows)
        return {grant.document_id: grant for grant in grants}

    async def mark_used(
        self, transaction: Transaction, plant_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """``issued → used`` de las que siguen en ``issued``; devuelve las que cambiaron."""
        if not document_ids:
            return frozenset()
        result = await transaction.execute(
            _MARK_USED,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "document_ids": list(document_ids),
            },
        )
        return frozenset(_plain(row.document_id) for row in result)
