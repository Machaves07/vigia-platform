"""Lista de notificaciones del propio usuario en su forma mínima (TASK-324; SUP-U05-33; LC-LAZ-36;
PAT-LAZ-SEG-03; NFR-LAZ-09).

``NotificationReader.list_own``: los avisos cuyo ``recipient_user_id`` es el actor del contexto
(verificación de propiedad del objeto en la aplicación, PAT-LAZ-SEG-03), dentro de su
organización (la RLS lo asegura además), del más reciente al más antiguo, **paginados por clave**
``(created_at, notification_id)`` sobre el índice ``notification_recipient_list``. Cada aviso
declara el estado de su entrega por correo (``degraded_app_only`` se ve, BR-LAZ-130).

La clave es ``notifications.read`` (la tienen los siete roles): basta con que alguna asignación
del contexto la tenga (``narrowed``); la propiedad la da el filtro por el actor. TASK-348 completa
la ruta (filtros de estado y periodo, archivado a los 60 días con su filtro, marcar leída).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.sql import Executable

from vigia_platform.identity.authz.authorize import ResourceNotFound, narrowed
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.domain.enums import NotificationChannel
from vigia_platform.notifications.adapters.postgres.tables import (
    NOTIFICATION,
    NOTIFICATION_DELIVERY,
)
from vigia_platform.shared.context import ScopeContext, repository

__all__ = [
    "MAX_PAGE",
    "NotificationCursor",
    "NotificationItem",
    "NotificationPage",
    "NotificationReader",
]

MAX_PAGE: Final = 200
"""``limit`` ≤ 200 (LC §6.9)."""


class _Database(Protocol):
    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Any]: ...


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
    """``listar_notificaciones`` (SUP-U05-33), forma mínima."""

    def __init__(self, *, database: _Database, provider_organization_id: uuid.UUID) -> None:
        self._database = database
        self._provider_organization_id = provider_organization_id

    def __repr__(self) -> str:
        return "NotificationReader()"

    async def list_own(
        self,
        context: ScopeContext,
        *,
        after: NotificationCursor | None = None,
        limit: int = MAX_PAGE,
    ) -> NotificationPage:
        """Una página de los avisos del actor, del más reciente al más antiguo."""
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError("limit debe estar entre 1 y 200")
        if (
            narrowed(
                context,
                PermissionKey.NOTIFICATIONS_READ,
                provider_organization_id=self._provider_organization_id,
            )
            is None
        ):
            raise ResourceNotFound()
        n = NOTIFICATION
        conditions = [
            n.c.organization_id == context.organization_id,
            n.c.recipient_user_id == context.actor.id,
            n.c.archived_at.is_(None),
        ]
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
        email_status=row.email_status,
    )
