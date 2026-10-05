"""Marca única de la lista de revocación global (TASK-218; gob_0021; D-7, PAT-GOB-RES-02).

``fleet.revocation_list_state`` es una fila global sin datos de cliente. La revocación de un nodo
(y la re-alta, que revoca la credencial anterior) llama a ``mark_dirty`` **en su propia
transacción**: ``dirty_generation`` sube en uno y ``dirty_since`` conserva el primer instante sin
publicar. La fila se bloquea hasta confirmar, así que las revocaciones se ordenan entre sí y cada
una deja su generación. ``regenerate_revocation_list`` (TASK-220) lee y limpia la marca por
generación con el contexto de operador; aquí solo se marca y se lee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresRevocationMarkStore", "RevocationListState"]

_MARK: Final = text(
    "UPDATE fleet.revocation_list_state"
    " SET dirty_generation = dirty_generation + 1,"
    " dirty_since = COALESCE(dirty_since, :marked_at)"
    " WHERE singleton RETURNING dirty_generation"
)
_STATE: Final = text(
    "SELECT dirty_generation, dirty_since, published_generation, published_at,"
    " object_version_id, next_update, entries FROM fleet.revocation_list_state WHERE singleton"
)


@dataclass(frozen=True, slots=True)
class RevocationListState:
    dirty_generation: int
    dirty_since: datetime | None
    published_generation: int
    published_at: datetime | None
    object_version_id: str | None
    next_update: datetime | None
    entries: int

    @property
    def dirty(self) -> bool:
        return self.dirty_generation > self.published_generation


@repository
class PostgresRevocationMarkStore:
    """``fleet.revocation_list_state``: la marca de la revocación y su lectura."""

    async def mark_dirty(self, transaction: Transaction, marked_at: datetime) -> int:
        """Sube la generación sucia en la transacción de la revocación; devuelve la nueva."""
        row = (await transaction.execute(_MARK, {"marked_at": marked_at})).first()
        if row is None:  # la migración siembra la fila: sin ella, nunca se da por marcada
            raise RuntimeError("falta la fila de fleet.revocation_list_state")
        return int(row.dirty_generation)

    async def state(self, transaction: Transaction) -> RevocationListState:
        row = (await transaction.execute(_STATE)).first()
        if row is None:
            raise RuntimeError("falta la fila de fleet.revocation_list_state")
        return RevocationListState(
            dirty_generation=int(row.dirty_generation),
            dirty_since=row.dirty_since,
            published_generation=int(row.published_generation),
            published_at=row.published_at,
            object_version_id=row.object_version_id,
            next_update=row.next_update,
            entries=int(row.entries),
        )
