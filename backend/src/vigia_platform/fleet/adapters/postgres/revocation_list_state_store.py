"""Estado de publicación de la lista de revocación global sobre PostgreSQL (TASK-220; D-7).

``fleet.revocation_list_state`` (gob_0021 y gob_0024) es una fila global sin datos de cliente ni
seguridad a nivel de fila; la revocación sube su marca (``PostgresRevocationMarkStore``, TASK-218)
y ``regenerate_revocation_list`` la lee y la limpia aquí:

- ``try_lock_publication``: ``pg_try_advisory_xact_lock`` de la publicación en la transacción de
  control del ciclo, que la mantiene abierta hasta registrar. Excluye dos publicadores a la vez
  (el worker y ``vigia-admin``): sin ella, uno podría retirar del almacén la lista que el otro
  acaba de añadir. Si otro la tiene, el ciclo no hace nada.
- ``status``: la fila, sin bloquearla (una revocación nunca espera a la publicación).
- ``reserve_crl_number``: ``crl_number + 1`` en **su propia** transacción corta, antes de firmar:
  un número nunca se repite aunque la publicación falle después de escribir el objeto.
- ``record_publication``: tras confirmar la publicación, **comparación y escritura condicional**:
  ``published_generation`` pasa a la generación **leída al empezar** (``GREATEST``), nunca a la
  ``dirty_generation`` actual. Una revocación confirmada mientras se publicaba deja la marca puesta
  (``dirty_since`` = inicio del ciclo) para el ciclo siguiente.

**Orden de los candados** (el de ``fleet.application.common`` termina en la fila global): candado
de publicación → fila global. La reserva y el registro solo toman la fila, en sentencias cortas;
la revocación toma la fila al final de su transacción y nunca el candado de publicación: no hay
espera circular.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.fleet.domain.revocation_list import RevocationListStatus
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PUBLICATION_LOCK", "PostgresRevocationListStateStore"]

PUBLICATION_LOCK: Final = "revocation_list_publication"
"""Clave del candado consultivo de la publicación (``hashtextextended``)."""

_TRY_LOCK: Final = text(
    "SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0)) AS locked"
)
_STATUS: Final = text(
    "SELECT dirty_generation, published_generation, published_at, next_update, entries,"
    " crl_number, object_version_id FROM fleet.revocation_list_state WHERE singleton"
)
_RESERVE: Final = text(
    "UPDATE fleet.revocation_list_state SET crl_number = crl_number + 1"
    " WHERE singleton RETURNING crl_number"
)
_RECORD: Final = text(
    "UPDATE fleet.revocation_list_state SET"
    " published_generation = GREATEST(published_generation, :generation),"
    " dirty_since = CASE WHEN dirty_generation > GREATEST(published_generation, :generation)"
    " THEN CAST(:started_at AS timestamptz) END,"
    " published_at = :published_at, object_version_id = :object_version_id,"
    " next_update = :next_update, entries = :entries"
    " WHERE singleton AND :generation <= dirty_generation"
    " RETURNING dirty_generation, published_generation"
)


def _missing() -> RuntimeError:
    # La migración siembra la fila: sin ella, nunca se da nada por publicado.
    return RuntimeError("falta la fila de fleet.revocation_list_state")


@repository
class PostgresRevocationListStateStore:
    """La fila global de la lista de revocación, dentro de las transacciones del ciclo."""

    async def try_lock_publication(self, transaction: Transaction) -> bool:
        row = (await transaction.execute(_TRY_LOCK, {"lock_key": PUBLICATION_LOCK})).one()
        return bool(row.locked)

    async def status(self, transaction: Transaction) -> RevocationListStatus:
        row = (await transaction.execute(_STATUS)).first()
        if row is None:
            raise _missing()
        return RevocationListStatus(
            dirty_generation=int(row.dirty_generation),
            published_generation=int(row.published_generation),
            published_at=row.published_at,
            next_update=row.next_update,
            entries=int(row.entries),
            crl_number=int(row.crl_number),
            object_version_id=row.object_version_id,
        )

    async def reserve_crl_number(self, transaction: Transaction) -> int:
        row = (await transaction.execute(_RESERVE)).first()
        if row is None:
            raise _missing()
        return int(row.crl_number)

    async def record_publication(
        self,
        transaction: Transaction,
        *,
        generation: int,
        started_at: datetime,
        published_at: datetime,
        object_version_id: str,
        next_update: datetime,
        entries: int,
    ) -> bool:
        """Registra lo publicado; ``True`` si la marca quedó limpia (nada llegó entretanto)."""
        row = (
            await transaction.execute(
                _RECORD,
                {
                    "generation": generation,
                    "started_at": started_at,
                    "published_at": published_at,
                    "object_version_id": object_version_id,
                    "next_update": next_update,
                    "entries": entries,
                },
            )
        ).first()
        if row is None:
            raise _missing()
        return int(row.dirty_generation) == int(row.published_generation)
