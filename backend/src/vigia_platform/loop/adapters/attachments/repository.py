"""Lecturas de la proyección para los adjuntos de cierre, siempre bajo ``ScopeContext``.

Solo dos consultas por identificador, filtradas por la organización del contexto además de la
seguridad a nivel de fila: el lugar (planta, zona, sujeto) de una acción correctiva y la ficha de
un adjunto ya aceptado. **Ninguna lista claves ni prefijos.** La planta y la zona devueltas son
las que decide la autorización por zona del servicio.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from vigia_platform.loop.adapters.postgres.tables import CLOSURE_ATTACHMENT, CORRECTIVE_ACTION
from vigia_platform.shared.context import ScopeContext

__all__ = ["ActionPlace", "AttachmentDatabase", "PostgresAttachmentRepository", "StoredAttachment"]


class AttachmentDatabase(Protocol):
    async def read(
        self, context: ScopeContext, statement: Any, parameters: Mapping[str, Any] | None = None
    ) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ActionPlace:
    """Dónde vive una acción correctiva: la clave de sus adjuntos se construye desde aquí."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    """La ficha de un adjunto aceptado que hace falta para leerlo."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    storage_key: str
    sha256: str
    size_bytes: int


def _plain(value: object) -> uuid.UUID:
    """``uuid.UUID`` exacto (asyncpg devuelve una subclase propia)."""
    return uuid.UUID(str(value))


@dataclass(frozen=True, slots=True)
class PostgresAttachmentRepository:
    database: AttachmentDatabase

    async def action_place(self, context: ScopeContext, action_id: uuid.UUID) -> ActionPlace | None:
        statement = select(
            CORRECTIVE_ACTION.c.organization_id,
            CORRECTIVE_ACTION.c.plant_id,
            CORRECTIVE_ACTION.c.zone_id,
            CORRECTIVE_ACTION.c.subject_id,
        ).where(
            CORRECTIVE_ACTION.c.action_id == action_id,
            CORRECTIVE_ACTION.c.organization_id == context.organization_id,
        )
        rows = await self.database.read(context, statement)
        if not rows:
            return None
        row = rows[0]._mapping
        return ActionPlace(
            _plain(row["organization_id"]),
            _plain(row["plant_id"]),
            _plain(row["zone_id"]),
            _plain(row["subject_id"]),
        )

    async def stored_attachment(
        self, context: ScopeContext, attachment_id: uuid.UUID
    ) -> StoredAttachment | None:
        statement = select(
            CLOSURE_ATTACHMENT.c.organization_id,
            CLOSURE_ATTACHMENT.c.plant_id,
            CLOSURE_ATTACHMENT.c.zone_id,
            CLOSURE_ATTACHMENT.c.storage_key,
            CLOSURE_ATTACHMENT.c.sha256,
            CLOSURE_ATTACHMENT.c.size_bytes,
        ).where(
            CLOSURE_ATTACHMENT.c.attachment_id == attachment_id,
            CLOSURE_ATTACHMENT.c.organization_id == context.organization_id,
        )
        rows = await self.database.read(context, statement)
        if not rows:
            return None
        row = rows[0]._mapping
        return StoredAttachment(
            _plain(row["organization_id"]),
            _plain(row["plant_id"]),
            _plain(row["zone_id"]),
            str(row["storage_key"]),
            str(row["sha256"]),
            int(row["size_bytes"]),
        )
