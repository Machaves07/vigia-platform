"""Lista, marcar leída y archivado de las notificaciones del propio usuario (TASK-324 y 348;
SUP-U05-33 y 34; LC-LAZ-36; PAT-LAZ-SEG-03; NFR-LAZ-09).

``NotificationReader.list_own``: los avisos cuyo ``recipient_user_id`` es el actor del contexto
(verificación de propiedad del objeto en la aplicación, PAT-LAZ-SEG-03), dentro de su
organización (la RLS lo asegura además), del más reciente al más antiguo, **paginados por clave**
``(created_at, notification_id)`` sobre el índice ``notification_recipient_list``. Cada aviso
declara el estado de su entrega por correo (``degraded_app_only`` se ve, BR-LAZ-130). Filtra por
``status`` (``unread``/``read``) y por periodo (``since``/``until`` sobre ``created_at``); lo
archivado (``archived_at``) queda fuera por defecto y entra con ``include_archived``. Nada se
borra por ninguna de estas vías (NFR-LAZ-09, nota de 2026-09-20).

``mark_read``: ``UPDATE`` condicional a ``read_at IS NULL`` (idempotente: marcar dos veces no es
un error). Sin fila propia con ese ``notification_id`` en la organización, ``not_found`` (nunca
revela si existe de otro usuario, BR-NUC-09).

``archive_read_notifications``: el archivado a los 60 días (``[estimación propia]``, NFR-LAZ-09)
de lo ya leído; lo ejecuta ``notifications.grouping.EmailGroupingTask`` en cada pasada por
organización (no hay una tarea propia: una condicional más en la misma transacción horaria).

La clave es ``notifications.read`` (la tienen los siete roles): basta con que alguna asignación
del contexto la tenga (``narrowed``); la propiedad la da el filtro por el actor. **Bajo concesión**
se deniega (``Authorizer.deny``: ``authorization_denied`` en la auditoría del cliente y
``not_found``): el proveedor no entra al lazo (BR-LAZ-142) y no tiene avisos en el cliente.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import and_, or_, select, tuple_, update
from sqlalchemy.sql import Executable

from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    narrowed,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.domain.enums import NotificationChannel
from vigia_platform.notifications.adapters.postgres.tables import (
    NOTIFICATION,
    NOTIFICATION_DELIVERY,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "ARCHIVE_AFTER_READ",
    "MAX_PAGE",
    "NotificationCursor",
    "NotificationItem",
    "NotificationPage",
    "NotificationReader",
    "NotificationStatusFilter",
    "archive_read_notifications",
]

MAX_PAGE: Final = 200
"""``limit`` ≤ 200 (LC §6.9)."""

ARCHIVE_AFTER_READ: Final = timedelta(days=60)
"""Lo leído se archiva a los 60 días ``[estimación propia]`` (NFR-LAZ-09, nota de 2026-09-20)."""


class NotificationStatusFilter(enum.StrEnum):
    """``status`` de ``GET /notifications`` (SUP-U05-33)."""

    UNREAD = "unread"
    READ = "read"


class _Database(Protocol):
    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Any]: ...

    def transaction(self, context: ScopeContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class NotificationCursor:
    created_at: datetime
    notification_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class NotificationItem:
    notification_id: uuid.UUID
    event_name: str
    urgency: str
    title_es: str
    body_es: str
    deep_link_kind: str
    deep_link_id: uuid.UUID
    created_at: datetime
    read_at: datetime | None
    archived_at: datetime | None
    email_status: str | None
    """Estado de la entrega por correo, o ``None`` si el aviso no tiene correo."""

    @property
    def cursor(self) -> NotificationCursor:
        return NotificationCursor(self.created_at, self.notification_id)


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[NotificationItem, ...]
    next_cursor: NotificationCursor | None


_EMAIL: Final = NOTIFICATION_DELIVERY.alias("email_delivery")


@repository
class NotificationReader:
    """``listar_notificaciones`` y ``marcar_leída`` (SUP-U05-33 y 34)."""

    def __init__(
        self,
        *,
        database: _Database,
        authorizer: Authorizer,
        clock: Clock,
        provider_organization_id: uuid.UUID,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._clock = clock
        self._provider_organization_id = provider_organization_id

    def __repr__(self) -> str:
        return "NotificationReader()"

    async def _own_scope(self, context: ScopeContext) -> None:
        """Comprueba la clave y la concesión; ``ResourceNotFound`` si no procede
        (PAT-LAZ-SEG-03)."""
        key = PermissionKey.NOTIFICATIONS_READ
        if context.concession_id is not None:
            await self._authorizer.deny(
                context, key, Resource.organization(context.organization_id)
            )
        if narrowed(context, key, provider_organization_id=self._provider_organization_id) is None:
            raise ResourceNotFound()

    async def list_own(
        self,
        context: ScopeContext,
        *,
        after: NotificationCursor | None = None,
        limit: int = MAX_PAGE,
        status: NotificationStatusFilter | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        include_archived: bool = False,
    ) -> NotificationPage:
        """Una página de los avisos del actor, del más reciente al más antiguo."""
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError("limit debe estar entre 1 y 200")
        await self._own_scope(context)
        n = NOTIFICATION
        conditions = [
            n.c.organization_id == context.organization_id,
            n.c.recipient_user_id == context.actor.id,
        ]
        if not include_archived:
            conditions.append(n.c.archived_at.is_(None))
        if status is NotificationStatusFilter.UNREAD:
            conditions.append(n.c.read_at.is_(None))
        elif status is NotificationStatusFilter.READ:
            conditions.append(n.c.read_at.is_not(None))
        if since is not None:
            conditions.append(n.c.created_at >= since)
        if until is not None:
            conditions.append(n.c.created_at <= until)
        if after is not None:
            conditions.append(
                or_(
                    n.c.created_at < after.created_at,
                    and_(
                        n.c.created_at == after.created_at,
                        n.c.notification_id < after.notification_id,
                    ),
                )
            )
        statement = (
            select(
                n.c.notification_id,
                n.c.event_name,
                n.c.urgency,
                n.c.title_es,
                n.c.body_es,
                n.c.deep_link_ref,
                n.c.created_at,
                n.c.read_at,
                n.c.archived_at,
                _EMAIL.c.status.label("email_status"),
            )
            .select_from(
                n.outerjoin(
                    _EMAIL,
                    and_(
                        _EMAIL.c.organization_id == n.c.organization_id,
                        tuple_(_EMAIL.c.notification_id, _EMAIL.c.notification_created_at)
                        == tuple_(n.c.notification_id, n.c.created_at),
                        _EMAIL.c.channel == NotificationChannel.EMAIL.value,
                    ),
                )
            )
            .where(*conditions)
            .order_by(n.c.created_at.desc(), n.c.notification_id.desc())
            .limit(limit + 1)
        )
        rows = await self._database.read(context, statement)
        items = tuple(_item(row) for row in rows[:limit])
        more = len(rows) > limit
        return NotificationPage(items, items[-1].cursor if more and items else None)

    async def mark_read(self, context: ScopeContext, notification_id: uuid.UUID) -> None:
        """Marca leído el aviso propio; idempotente. Sin fila propia, ``not_found`` (PR-LAZ-61)."""
        await self._own_scope(context)
        n = NOTIFICATION
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                update(n)
                .where(
                    n.c.organization_id == context.organization_id,
                    n.c.notification_id == notification_id,
                    n.c.recipient_user_id == context.actor.id,
                    n.c.read_at.is_(None),
                )
                .values(read_at=self._clock.now())
            )
            exists = (
                await transaction.execute(
                    select(n.c.notification_id)
                    .where(
                        n.c.organization_id == context.organization_id,
                        n.c.notification_id == notification_id,
                        n.c.recipient_user_id == context.actor.id,
                    )
                    .limit(1)
                )
            ).first()
            if exists is None:
                raise ResourceNotFound()


async def archive_read_notifications(
    transaction: Transaction, clock: Clock, *, cutoff: timedelta = ARCHIVE_AFTER_READ
) -> None:
    """Archiva lo leído con más de ``cutoff`` (60 días por defecto): deja de verse por defecto y
    sigue consultable con ``include_archived`` (nada se borra, NFR-LAZ-09)."""
    n = NOTIFICATION
    threshold = clock.now() - cutoff
    await transaction.execute(
        update(n)
        .where(
            n.c.organization_id == transaction.context.organization_id,
            n.c.archived_at.is_(None),
            n.c.read_at.is_not(None),
            n.c.read_at <= threshold,
        )
        .values(archived_at=clock.now())
    )


def _item(row: Any) -> NotificationItem:
    link = row.deep_link_ref
    return NotificationItem(
        notification_id=uuid.UUID(str(row.notification_id)),
        event_name=row.event_name,
        urgency=row.urgency,
        title_es=row.title_es,
        body_es=row.body_es,
        deep_link_kind=str(link["kind"]),
        deep_link_id=uuid.UUID(str(link["id"])),
        created_at=row.created_at,
        read_at=row.read_at,
        archived_at=row.archived_at,
        email_status=row.email_status,
    )
