"""Consultas acotadas de acciones y adjuntos de cierre."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from vigia_platform.loop.adapters.postgres.tables import CLOSURE_ATTACHMENT, CORRECTIVE_ACTION
from vigia_platform.shared.context import ScopeContext

__all__ = ["PostgresAttachmentRepository"]


class AttachmentDatabase(Protocol):
    async def read(
        self, context: ScopeContext, statement: Any, parameters: Mapping[str, Any] | None = None
    ) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class PostgresAttachmentRepository:
    """Solo lecturas RLS; no ofrece listado de claves ni prefijos."""

    database: AttachmentDatabase

    async def action_scope(
        self, context: ScopeContext, action_id: uuid.UUID
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
        statement = (
            select(
                CORRECTIVE_ACTION.c.organization_id,
                CORRECTIVE_ACTION.c.plant_id,
                CORRECTIVE_ACTION.c.zone_id,
            )
            .where(CORRECTIVE_ACTION.c.action_id == action_id)
            .where(CORRECTIVE_ACTION.c.organization_id == context.organization_id)
        )
        rows = await self.database.read(context, statement)
        if not rows:
            return None
        row = rows[0]._mapping
        return (
            uuid.UUID(str(row["organization_id"])),
            uuid.UUID(str(row["plant_id"])),
            uuid.UUID(str(row["zone_id"])),
        )

    async def attachment_key(self, context: ScopeContext, attachment_id: uuid.UUID) -> str | None:
        statement = (
            select(CLOSURE_ATTACHMENT.c.storage_key)
            .where(CLOSURE_ATTACHMENT.c.attachment_id == attachment_id)
            .where(CLOSURE_ATTACHMENT.c.organization_id == context.organization_id)
        )
        rows = await self.database.read(context, statement)
        return None if not rows else str(rows[0]._mapping["storage_key"])
