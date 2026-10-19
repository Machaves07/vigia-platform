"""Servicios de las rutas de ``notifications`` (``app.state``) (TASK-324; A-52).

``NotificationsHttp`` lo deja la unidad ``notifications`` en ``app.state`` (``API_STATE`` de
``notifications.registration``), construido con la infraestructura común (``UnitServices``). Las
rutas lo toman en cada petición: sin él, ``internal_error`` (fallo cerrado, nunca deja pasar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request

from vigia_platform.notifications.reader import NotificationReader
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["NOTIFICATIONS_STATE_KEY", "NotificationsHttp", "installed", "notifications_http"]

NOTIFICATIONS_STATE_KEY: Final = "vigia_notifications_http"

_log = get_logger("notifications.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class NotificationsHttp:
    """Los servicios que usan las rutas de ``notifications``."""

    reader: NotificationReader | None = None
    """``GET /notifications`` (TASK-324, forma mínima; TASK-348 la completa)."""


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta de las notificaciones no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def notifications_http(request: Request) -> NotificationsHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, NOTIFICATIONS_STATE_KEY, None)
    if not isinstance(services, NotificationsHttp):
        _log.error("las rutas de las notificaciones no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services
