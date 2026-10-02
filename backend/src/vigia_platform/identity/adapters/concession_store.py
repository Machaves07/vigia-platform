"""``ConcessionStore`` de ``identity.concessions`` sobre PostgreSQL (LC-NUC-06; ``nuc_0009``).

- Desde la **proveedora** (su contexto de sesión, sin concesión): ``client_terms``,
  ``provider_concession`` y ``provider_concessions`` llaman a las funciones de búsqueda
  ``identity.concession_terms``, ``identity.provider_concession_of`` e
  ``identity.provider_concessions_of`` (``nuc_0012``); la RLS no deja leer nada más del cliente.
- En la transacción del escritor del expediente (``projection``): ``insert``, ``revoke`` y
  ``expire``. Las reglas entre filas de la base (cliente activo de tipo ``client``, proveedora de
  tipo ``provider``, planta del cliente, tope ``concession_max_days``, cierre una sola vez) llegan
  como ``check_violation`` con nombre y se traducen: un destino que no existe es
  ``ResourceNotFound``; una duración fuera del tope, ``ConcessionRejected``. ``UPDATE`` sin
  ``RETURNING``: con un contexto del proveedor la fila cerrada ya no es visible.
- En el contexto del **cliente**: ``concession``, ``list_concessions`` y ``provider_queries``;
  las dos últimas escriben en su misma transacción la entrada ``ledger_read`` (BR-NUC-41, 59, 62:
  la lista de concesiones es la proyección de los registros ``provider_concession_*`` y las
  consultas son registros ``provider_query`` del expediente).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.identity.application.concessions import (
    ClientTerms,
    Concession,
    ConcessionRejected,
    ConcessionRejectionCode,
    ConcessionStatus,
    ProviderQueryCursor,
    ProviderQueryPage,
    ProviderQueryView,
    RevokedBySide,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    CHECK_VIOLATION,
    LedgerDatabase,
    violated_constraint,
)
from vigia_platform.shared.context import ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["CONCESSION_RESOURCE_KIND", "PostgresConcessionStore"]

CONCESSION_RESOURCE_KIND: Final = "provider_concession"

_TARGET_MISSING: Final = frozenset(
    {
        "provider_concession_client_kind",
        "provider_concession_provider_kind",
        "provider_concession_client_active",
        "provider_concession_scope_plant",
        "provider_concession_never_the_provider",
        "provider_concession_organization_scope",
    }
)
"""Restricciones de ``nuc_0004`` y ``nuc_0009`` que dicen que el destino no existe."""

_DURATION: Final = frozenset({"provider_concession_max_days", "provider_concession_duration"})
_CLOSING: Final = frozenset(
    {"provider_concession_revoked_before_expiry", "provider_concession_expired_after_expiry"}
)

_TERMS: Final = text(
    "SELECT concession_max_days, concession_default_days"
    " FROM identity.concession_terms(CAST(:client AS uuid))"
)
_PROVIDER_SIDE: Final = text(
    "SELECT concession_id, organization_id, provider_user_id, scope_level, scope_id, granted_at,"
    " expires_at, status, revoked_at FROM identity.provider_concession_of(CAST(:id AS uuid))"
)
_PROVIDER_LIST: Final = text(
    "SELECT concession_id, organization_id, provider_user_id, scope_level, scope_id, granted_at,"
    " expires_at, status, revoked_at, revoked_by_side"
    " FROM identity.provider_concessions_of(CAST(:grantee AS uuid))"
)
_ONE: Final = text(
    "SELECT concession_id, organization_id, provider_user_id, provider_organization_id,"
    " scope_level, scope_id, reason, granted_at, expires_at, status, revoked_at, revoked_by,"
    " revoked_by_side FROM identity.provider_concession WHERE concession_id = :id"
)
_INSERT: Final = text(
    "INSERT INTO identity.provider_concession (concession_id, organization_id, provider_user_id,"
    " provider_organization_id, scope_level, scope_id, reason, granted_at, expires_at)"
    " VALUES (:concession_id, :organization_id, :provider_user_id, :provider_organization_id,"
    " :scope_level, :scope_id, :reason, :granted_at, :expires_at)"
)
_REVOKE: Final = text(
    "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = :revoked_at,"
    " revoked_by = :revoked_by, revoked_by_side = :side"
    " WHERE concession_id = :id AND status = 'active' AND revoked_at IS NULL"
    " AND expires_at > :revoked_at"
)
_EXPIRE: Final = text(
    "UPDATE identity.provider_concession SET status = 'expired'"
    " WHERE concession_id = :id AND status = 'active' AND revoked_at IS NULL"
    " AND expires_at <= :now AND expires_at <= pg_catalog.now()"
)
_DUE: Final = text(
    "SELECT concession_id, organization_id, provider_user_id, provider_organization_id,"
    " scope_level, scope_id, reason, granted_at, expires_at, status, revoked_at, revoked_by,"
    " revoked_by_side FROM identity.provider_concession"
    " WHERE status = 'active' AND revoked_at IS NULL AND expires_at <= :now"
    " ORDER BY expires_at, concession_id LIMIT 500"
)
_LIST: Final = text(
    "SELECT concession_id, organization_id, provider_user_id, provider_organization_id,"
    " scope_level, scope_id, reason, granted_at, expires_at, status, revoked_at, revoked_by,"
    " revoked_by_side FROM identity.provider_concession"
    " WHERE CAST(:plant_id AS uuid) IS NULL OR scope_level = 'organization'"
    " OR scope_id = CAST(:plant_id AS uuid)"
    " ORDER BY granted_at DESC, concession_id DESC"
)
_QUERIES: Final = text(
    "SELECT record_id, received_at, plant_id, actor_id, content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND record_type = 'provider_query'"
    " AND actor_concession_id = :concession_id"
    " AND (CAST(:after_received_at AS timestamptz) IS NULL"
    " OR (received_at, record_id)"
    " > (CAST(:after_received_at AS timestamptz), CAST(:after_record_id AS uuid)))"
    " ORDER BY received_at, record_id LIMIT :limit"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _concession(row: Row[Any], *, provider_organization_id: uuid.UUID | None = None) -> Concession:
    mapping = row._mapping
    return Concession(
        concession_id=_uuid(mapping["concession_id"]),
        organization_id=_uuid(mapping["organization_id"]),
        provider_user_id=_uuid(mapping["provider_user_id"]),
        provider_organization_id=(
            _uuid(mapping["provider_organization_id"])
            if provider_organization_id is None
            else provider_organization_id
        ),
        scope_level=ScopeLevel(mapping["scope_level"]),
        scope_id=_uuid(mapping["scope_id"]),
        reason=mapping.get("reason"),
        granted_at=mapping["granted_at"],
        expires_at=mapping["expires_at"],
        status=ConcessionStatus(mapping["status"]),
        revoked_at=mapping["revoked_at"],
        revoked_by=_optional_uuid(mapping.get("revoked_by")),
        revoked_by_side=(
            None
            if mapping.get("revoked_by_side") is None
            else RevokedBySide(mapping["revoked_by_side"])
        ),
    )


def _translate(error: sa_exc.IntegrityError) -> Exception | None:
    """El rechazo de la base como error del servicio, o ``None`` si no es una regla conocida."""
    constraint = violated_constraint(error, CHECK_VIOLATION)
    if constraint in _TARGET_MISSING:
        return ResourceNotFound()
    if constraint in _DURATION:
        return ConcessionRejected(ConcessionRejectionCode.DURATION_OUT_OF_RANGE)
    if constraint in _CLOSING:
        return ConcessionRejected(ConcessionRejectionCode.CONCESSION_CLOSED)
    return None


@repository
class PostgresConcessionStore:
    """``ConcessionStore`` sobre ``shared.db`` y el escritor de auditoría."""

    def __init__(self, *, database: LedgerDatabase, audit: AuditWriter) -> None:
        self._database = database
        self._audit = audit

    async def client_terms(
        self, context: ScopeContext, client_organization_id: uuid.UUID
    ) -> ClientTerms | None:
        rows = await self._database.read(context, _TERMS, {"client": str(client_organization_id)})
        if not rows:
            return None
        (row,) = rows
        return ClientTerms(
            max_days=int(row.concession_max_days), default_days=int(row.concession_default_days)
        )

    async def concession(
        self, context: ScopeContext, concession_id: uuid.UUID
    ) -> Concession | None:
        rows = await self._database.read(context, _ONE, {"id": concession_id})
        return _concession(rows[0]) if rows else None

    async def provider_concession(
        self, context: ScopeContext, concession_id: uuid.UUID
    ) -> Concession | None:
        rows = await self._database.read(context, _PROVIDER_SIDE, {"id": str(concession_id)})
        if not rows:
            return None
        return _concession(rows[0], provider_organization_id=context.organization_id)

    async def provider_concessions(
        self, context: ScopeContext, grantee: uuid.UUID
    ) -> tuple[Concession, ...]:
        rows = await self._database.read(context, _PROVIDER_LIST, {"grantee": str(grantee)})
        return tuple(
            _concession(row, provider_organization_id=context.organization_id) for row in rows
        )

    async def insert(self, transaction: Transaction, concession: Concession) -> None:
        try:
            await transaction.execute(
                _INSERT,
                {
                    "concession_id": concession.concession_id,
                    "organization_id": concession.organization_id,
                    "provider_user_id": concession.provider_user_id,
                    "provider_organization_id": concession.provider_organization_id,
                    "scope_level": concession.scope_level.value,
                    "scope_id": concession.scope_id,
                    "reason": concession.reason,
                    "granted_at": concession.granted_at,
                    "expires_at": concession.expires_at,
                },
            )
        except sa_exc.IntegrityError as error:
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from None

    async def revoke(
        self,
        transaction: Transaction,
        concession: Concession,
        *,
        revoked_at: datetime,
        revoked_by: uuid.UUID,
        side: RevokedBySide,
    ) -> None:
        await self._close(
            transaction,
            _REVOKE,
            {
                "id": concession.concession_id,
                "revoked_at": revoked_at,
                "revoked_by": revoked_by,
                "side": RevokedBySide(side).value,
            },
        )

    async def expire(self, transaction: Transaction, concession: Concession, now: datetime) -> None:
        await self._close(transaction, _EXPIRE, {"id": concession.concession_id, "now": now})

    @staticmethod
    async def _close(transaction: Transaction, statement: Any, parameters: dict[str, Any]) -> None:
        try:
            result = await transaction.execute(statement, parameters)
        except sa_exc.IntegrityError as error:
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from None
        if getattr(result, "rowcount", 0) != 1:
            # Otro proceso la cerró antes, o aún no vence según la base: nada que cerrar.
            raise ConcessionRejected(ConcessionRejectionCode.CONCESSION_CLOSED)

    async def due_for_expiry(
        self, transaction: Transaction, now: datetime
    ) -> tuple[Concession, ...]:
        rows = (await transaction.execute(_DUE, {"now": now})).all()
        return tuple(_concession(row) for row in rows)

    async def list_concessions(
        self, context: ScopeContext, plant_id: uuid.UUID | None
    ) -> tuple[Concession, ...]:
        async with self._database.transaction(context) as transaction:
            rows = (
                await transaction.execute(
                    _LIST, {"plant_id": None if plant_id is None else str(plant_id)}
                )
            ).all()
            await self._audit.append(
                context,
                AuditOperation.LEDGER_READ,
                plant_id=plant_id,
                filters={
                    "view": "provider_concessions",
                    "plant_id": None if plant_id is None else str(plant_id),
                },
                result_count=len(rows),
                transaction=transaction,
            )
        return tuple(_concession(row) for row in rows)

    async def provider_queries(
        self,
        context: ScopeContext,
        concession: Concession,
        after: ProviderQueryCursor | None,
        limit: int,
    ) -> ProviderQueryPage:
        async with self._database.transaction(context) as transaction:
            rows = list(
                (
                    await transaction.execute(
                        _QUERIES,
                        {
                            "organization_id": context.organization_id,
                            "concession_id": concession.concession_id,
                            "after_received_at": None if after is None else after.received_at,
                            "after_record_id": None if after is None else str(after.record_id),
                            "limit": limit + 1,
                        },
                    )
                ).all()
            )
            more = len(rows) > limit
            rows = rows[:limit]
            await self._audit.append(
                context,
                AuditOperation.LEDGER_READ,
                plant_id=concession.plant_id,
                resource=ResourceRef(CONCESSION_RESOURCE_KIND, concession.concession_id),
                filters={
                    "view": "provider_queries",
                    "concession_id": str(concession.concession_id),
                    "after": None if after is None else format_timestamp(after.received_at),
                    "limit": limit,
                },
                result_count=len(rows),
                transaction=transaction,
            )
        items = tuple(_query(row, concession) for row in rows)
        return ProviderQueryPage(
            items=items, next_cursor=items[-1].cursor if more and items else None
        )


def _query(row: Row[Any], concession: Concession) -> ProviderQueryView:
    document = json.loads(bytes(row.content))
    return ProviderQueryView(
        record_id=_uuid(row.record_id),
        received_at=row.received_at,
        plant_id=_optional_uuid(row.plant_id),
        concession_id=concession.concession_id,
        provider_user_id=_uuid(row.actor_id),
        operation=str(document["operation"]),
        method=str(document["method"]),
        resource=str(document["resource"]),
        occurred_at=str(document["occurred_at"]),
        reason=concession.reason,
    )
