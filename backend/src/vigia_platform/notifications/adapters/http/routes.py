"""Notificaciones por HTTP (TASK-324 y 348; LC-LAZ §6.9; SUP-U05-33 a 35).

- ``GET /notifications`` (``notifications.read``, **propias**): los avisos del actor, del más
  reciente al más antiguo, por clave (``after`` opaco y ``limit`` de 1 a 200), con filtro de
  ``status`` (``unread``/``read``), de periodo (``since``/``until`` sobre ``created_at``) y
  ``include_archived`` para ver también lo archivado a los 60 días. Cada aviso trae su
  ``deep_link_ref`` estructurado (nunca un enlace público) y ``email_status``, el estado de su
  entrega por correo (``degraded_app_only`` declara que el correo no salió, BR-LAZ-130).
- ``POST /notifications/{notification_id}/read`` (204, idempotente): marca leído un aviso propio;
  sin fila propia con ese identificador, ``not_found`` (PR-LAZ-61).
- ``PUT /notifications/preferences`` (SUP-U05-35): las preferencias del propio actor
  (``email_enabled_normal``, ``email_enabled_digest``, ``weekly_digest_weekday``); **no existe
  campo** para silenciar ``immediate`` (BR-LAZ-126, imposibilidad estructural).

``Cache-Control: no-store``. Las marcas, ISO 8601 UTC con milisegundos y ``Z``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.notifications.adapters.http.services import (
    NotificationsHttp,
    installed,
    notifications_http,
)
from vigia_platform.notifications.preferences import (
    NotificationPreference,
    NotificationPreferenceUpdate,
)
from vigia_platform.notifications.reader import (
    MAX_PAGE,
    NotificationCursor,
    NotificationItem,
    NotificationStatusFilter,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "NotificationOut",
    "NotificationPageOut",
    "NotificationPreferenceOut",
    "NotificationPreferenceUpdateIn",
    "decode_cursor",
    "encode_cursor",
    "notifications_router",
]

_READ: Final = PermissionKey.NOTIFICATIONS_READ.value
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")

Services = Annotated[NotificationsHttp, Depends(notifications_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeepLinkOut(_Strict):
    kind: str
    id: uuid.UUID


class NotificationOut(_Strict):
    notification_id: uuid.UUID
    event_name: str
    urgency: str
    title_es: str
    body_es: str
    deep_link_ref: DeepLinkOut
    created_at: str
    read_at: str | None
    archived_at: str | None
    email_status: str | None
    """Estado de la entrega por correo (``pending``, ``degraded_app_only``…) o nada."""


class NotificationPageOut(_Strict):
    items: tuple[NotificationOut, ...]
    next_after: str | None


class NotificationPreferenceUpdateIn(_Strict):
    email_enabled_normal: bool
    email_enabled_digest: bool
    weekly_digest_weekday: Annotated[int, Field(ge=1, le=7)]


class NotificationPreferenceOut(_Strict):
    email_enabled_normal: bool
    email_enabled_digest: bool
    weekly_digest_weekday: int
    updated_at: str
    email_suppressed_hard_bounce: bool
    email_suppressed_complaint: bool


def encode_cursor(cursor: NotificationCursor) -> str:
    """Cursor opaco: la marca con microsegundos y el ``notification_id`` del último aviso."""
    raw = f"{cursor.created_at.astimezone(UTC).isoformat()}|{cursor.notification_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> NotificationCursor:
    """El cursor de ``encode_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, _, identifier = raw.partition("|")
        created_at = datetime.fromisoformat(moment)
        notification_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if created_at.utcoffset() is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return NotificationCursor(created_at, notification_id)


def _view(item: NotificationItem) -> NotificationOut:
    return NotificationOut(
        notification_id=item.notification_id,
        event_name=item.event_name,
        urgency=item.urgency,
        title_es=item.title_es,
        body_es=item.body_es,
        deep_link_ref=DeepLinkOut(kind=item.deep_link_kind, id=item.deep_link_id),
        created_at=format_timestamp(item.created_at),
        read_at=None if item.read_at is None else format_timestamp(item.read_at),
        archived_at=None if item.archived_at is None else format_timestamp(item.archived_at),
        email_status=item.email_status,
    )


def _preference_view(preference: NotificationPreference) -> NotificationPreferenceOut:
    return NotificationPreferenceOut(
        email_enabled_normal=preference.email_enabled_normal,
        email_enabled_digest=preference.email_enabled_digest,
        weekly_digest_weekday=preference.weekly_digest_weekday,
        updated_at=format_timestamp(preference.updated_at),
        email_suppressed_hard_bounce=preference.email_suppressed_hard_bounce,
        email_suppressed_complaint=preference.email_suppressed_complaint,
    )


def notifications_router() -> APIRouter:
    router = APIRouter(tags=["notificaciones"])

    @router.get(
        "/notifications",
        dependencies=[
            requires(_READ),
            Depends(exact_query("after", "limit", "status", "since", "until", "include_archived")),
        ],
        summary="Las notificaciones propias, de la más reciente a la más antigua, por clave",
    )
    async def list_notifications(
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
        status_filter: Annotated[NotificationStatusFilter | None, Query(alias="status")] = None,
        since: Annotated[AwareDatetime | None, Query()] = None,
        until: Annotated[AwareDatetime | None, Query()] = None,
        include_archived: Annotated[bool, Query()] = False,
    ) -> NotificationPageOut:
        no_store(response)
        cursor = None if after is None else decode_cursor(after)
        page = await installed(services.reader).list_own(
            request_context(request),
            after=cursor,
            limit=limit,
            status=status_filter,
            since=since,
            until=until,
            include_archived=include_archived,
        )
        return NotificationPageOut(
            items=tuple(_view(item) for item in page.items),
            next_after=None if page.next_cursor is None else encode_cursor(page.next_cursor),
        )

    @router.post(
        "/notifications/{notification_id}/read",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[requires(_READ)],
        summary="Marca leído un aviso propio (idempotente)",
    )
    async def mark_notification_read(
        request: Request, response: Response, services: Services, notification_id: uuid.UUID
    ) -> None:
        no_store(response)
        await installed(services.reader).mark_read(request_context(request), notification_id)

    @router.put(
        "/notifications/preferences",
        dependencies=[requires(_READ)],
        summary="Las preferencias de correo del propio actor (sin campo para silenciar immediate)",
    )
    async def update_preferences(
        request: Request,
        response: Response,
        services: Services,
        body: NotificationPreferenceUpdateIn,
    ) -> NotificationPreferenceOut:
        no_store(response)
        preference = await installed(services.preferences).update_own(
            request_context(request),
            NotificationPreferenceUpdate(
                email_enabled_normal=body.email_enabled_normal,
                email_enabled_digest=body.email_enabled_digest,
                weekly_digest_weekday=body.weekly_digest_weekday,
            ),
        )
        return _preference_view(preference)

    @router.get(
        "/notifications/preferences",
        dependencies=[requires(_READ)],
        summary="Las preferencias de correo vigentes del propio actor",
    )
    async def read_preferences(
        request: Request, response: Response, services: Services
    ) -> NotificationPreferenceOut:
        no_store(response)
        preference = await installed(services.preferences).read_own(request_context(request))
        return _preference_view(preference)

    return router
