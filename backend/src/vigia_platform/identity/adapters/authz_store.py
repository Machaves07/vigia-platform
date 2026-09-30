"""Adaptadores de ``identity.authz`` sobre PostgreSQL (LC-NUC-04; PAT-NUC-REN-03, SEG-01).

- ``PostgresContextStore``: las lecturas de los constructores de contexto, **una sentencia** cada
  una, sin caché.

  * ``session_row``: ``UPDATE ... RETURNING`` de la sesión (estado, vencimientos, segundo factor
    verificado, usuario y organización activos; prolonga ``idle_expires_at``) con, en la misma
    sentencia, las asignaciones vigentes del usuario, la versión del aviso aceptada y, si la
    petición selecciona una concesión, ``identity.session_concession`` (``nuc_0008``), que solo
    devuelve la concesión vigente de ese usuario vista desde la proveedora.
  * ``operator_row``: el operador activo de la proveedora con sus asignaciones vigentes.

- ``PostgresAuthorizationAudit``: ``authorization_denied`` (``outcome = denied``) en la cadena de
  auditoría del contexto, con la clave pedida y el recurso; y ``context_absent_attempt`` en la de
  la proveedora con ``security_alert`` en la bandeja, en la misma transacción (BR-NUC-02).
- ``LedgerProviderQueryLedger``: ``provider_query`` por el único camino de escritura del
  expediente (``EscritorExpediente``); un rechazo sale como ``ProviderQueryRejected``.

Ninguna entrada lleva el identificador de sesión, su hash ni texto libre: identificadores, la clave
de permiso y el nombre de la operación del código.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Resource
from vigia_platform.identity.authz.context import (
    ConcessionRow,
    OperatorRow,
    SessionRow,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    RecordScope,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    AllowedScope,
    Role,
    ScopeContext,
    ScopeLevel,
    repository,
)
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "SESSION_CONTEXT_STATEMENT",
    "LedgerProviderQueryLedger",
    "PostgresAuthorizationAudit",
    "PostgresContextStore",
    "ProviderQueryRejected",
]

SESSION_CONTEXT_STATEMENT: Final = text(
    "WITH touched AS ("
    " UPDATE identity.session AS s SET last_seen_at = GREATEST(s.last_seen_at, :now),"
    " idle_expires_at = GREATEST(s.last_seen_at, :now) + interval '30 minutes'"
    " FROM identity.user_account AS u, identity.organization AS o"
    " WHERE s.session_id_hash = :session_id_hash"
    " AND u.user_id = s.user_id AND o.organization_id = s.organization_id"
    " AND s.status = 'active' AND s.idle_expires_at > :now AND s.absolute_expires_at > :now"
    " AND s.second_factor_verified AND u.status = 'active' AND o.status = 'active'"
    " RETURNING s.user_id, s.organization_id, o.kind AS organization_kind, u.display_name,"
    " u.privacy_notice_version_accepted)"
    " SELECT t.user_id, t.organization_id, t.organization_kind, t.display_name,"
    " t.privacy_notice_version_accepted,"
    " ARRAY(SELECT r.role || ' ' || r.scope_level || ' ' || CAST(r.scope_id AS text)"
    " FROM identity.role_assignment AS r"
    " WHERE r.user_id = t.user_id AND r.removed_at IS NULL ORDER BY r.assignment_id)"
    " AS assignments, c.concession_id, c.organization_id AS concession_organization_id,"
    " c.scope_level AS concession_scope_level, c.scope_id AS concession_scope_id,"
    " c.expires_at AS concession_expires_at"
    " FROM touched AS t LEFT JOIN LATERAL identity.session_concession("
    "CAST(:concession_id AS uuid), t.user_id, CAST(:now AS timestamptz)) AS c ON true"
)
"""La sentencia única de ``context_from_session`` (PAT-NUC-REN-03)."""

_OPERATOR: Final = text(
    "SELECT u.user_id, u.display_name,"
    " ARRAY(SELECT r.role || ' ' || r.scope_level || ' ' || CAST(r.scope_id AS text)"
    " FROM identity.role_assignment AS r"
    " WHERE r.user_id = u.user_id AND r.removed_at IS NULL ORDER BY r.assignment_id)"
    " AS assignments FROM identity.user_account AS u"
    " JOIN identity.organization AS o ON o.organization_id = u.organization_id"
    " WHERE u.user_id = :user_id AND u.status = 'active'"
    " AND o.kind = 'provider' AND o.status = 'active'"
)

_CONTEXT_ABSENT_ALERT: Final = "context_absent_attempt"


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el contexto exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _assignments(values: Sequence[str] | None) -> tuple[AllowedScope, ...]:
    result: list[AllowedScope] = []
    for value in values or ():
        role, level, scope_id = value.split(" ")
        result.append(AllowedScope(ScopeLevel(level), uuid.UUID(scope_id), Role(role)))
    return tuple(result)


@repository
class PostgresContextStore:
    """``ContextStore`` de ``identity.authz.context`` sobre ``shared.db``."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def session_row(
        self,
        lookup: ScopeContext,
        session_id_hash: str,
        now: datetime,
        concession_id: uuid.UUID | None,
    ) -> SessionRow | None:
        async with self._database.transaction(lookup) as transaction:
            row = (
                await transaction.execute(
                    SESSION_CONTEXT_STATEMENT,
                    {
                        "session_id_hash": session_id_hash,
                        "now": now,
                        "concession_id": None if concession_id is None else str(concession_id),
                    },
                )
            ).one_or_none()
        if row is None:
            return None
        concession = None
        if row.concession_id is not None:
            concession = ConcessionRow(
                concession_id=_uuid(row.concession_id),
                organization_id=_uuid(row.concession_organization_id),
                scope_level=ScopeLevel(row.concession_scope_level),
                scope_id=_uuid(row.concession_scope_id),
                expires_at=row.concession_expires_at,
            )
        kind: Literal["client", "provider"] = (
            "provider" if row.organization_kind == "provider" else "client"
        )
        return SessionRow(
            user_id=_uuid(row.user_id),
            organization_id=_uuid(row.organization_id),
            organization_kind=kind,
            display_name=row.display_name,
            privacy_notice_version_accepted=row.privacy_notice_version_accepted,
            assignments=_assignments(row.assignments),
            concession=concession,
        )

    async def operator_row(self, lookup: ScopeContext, user_id: uuid.UUID) -> OperatorRow | None:
        rows = await self._database.read(lookup, _OPERATOR, {"user_id": user_id})
        if not rows:
            return None
        (row,) = rows
        return OperatorRow(
            user_id=_uuid(row.user_id),
            display_name=row.display_name,
            assignments=_assignments(row.assignments),
        )


@repository
class PostgresAuthorizationAudit:
    """``AuthorizationAudit`` y ``SecurityAudit`` sobre el escritor de auditoría y la bandeja."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        audit: AuditWriter,
        outbox: OutboxPort,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._metrics = metrics or get_metrics()

    async def authorization_denied(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> None:
        in_organization = resource.organization_id == context.organization_id
        await self._audit.append(
            context,
            AuditOperation.AUTHORIZATION_DENIED,
            outcome=AuditOutcome.DENIED,
            plant_id=resource.plant_id if in_organization else None,
            zone_id=resource.zone_id if in_organization else None,
            resource=ResourceRef(resource.kind, resource.id),
            filters={"permission_key": PermissionKey(key).value},
        )

    async def context_absent_attempt(self, provider_context: ScopeContext, operation: str) -> None:
        async with self._database.transaction(provider_context) as transaction:
            await self._audit.append_without_organization(
                provider_context,
                AuditOperation.CONTEXT_ABSENT_ATTEMPT,
                outcome=AuditOutcome.DENIED,
                filters={"operation": operation},
                transaction=transaction,
            )
            await self._outbox.publish(
                transaction,
                NewEvent(
                    event_name="security_alert",
                    payload={
                        "alert_kind": _CONTEXT_ABSENT_ALERT,
                        "occurred_at": format_timestamp(self._clock.now()),
                    },
                ),
            )
        self._metrics.security_alert_total.add(
            1,
            {
                "alert_type": "security_alert",
                "organization_id": str(provider_context.organization_id),
            },
        )


class ProviderQueryRejected(Exception):
    """El expediente rechazó el ``provider_query``: la petición no puede darse por buena."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"provider_query rechazado: {rejection.code.value}")
        self.rejection = rejection


@repository
class LedgerProviderQueryLedger:
    """``ProviderQueryLedger`` sobre ``EscritorExpediente``."""

    def __init__(self, writer: EscritorExpediente) -> None:
        self._writer = writer

    async def write_provider_query(
        self, context: ScopeContext, content: Mapping[str, str], plant_id: uuid.UUID | None
    ) -> None:
        document: dict[str, Any] = dict(content)
        result = await self._writer.write(
            context, "provider_query", document, scope=RecordScope(plant_id=plant_id)
        )
        if isinstance(result, LedgerRejection):
            raise ProviderQueryRejected(result)
