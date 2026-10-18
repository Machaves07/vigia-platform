"""``detail_code`` de las notificaciones (domain-entities.md de U-04 §1.2 y §10; nº 33).

Uno de los 44 códigos de negocio de U-04: ``notification_preference_not_owned`` (un usuario
intenta cambiar las preferencias de otro), con el prefijo ``notifications_``. Su estado HTTP está
en el mapa único de U-04 (``loop.detail_codes.HTTP_STATUS_BY_DETAIL_CODE``, A-68) y su mensaje en
``labels.loop.es.json`` (``notifications_detail_code``).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

__all__ = ["NOTIFICATIONS_DETAIL_CODE_LABEL_BINDINGS", "NotificationsDetailCode"]


class NotificationsDetailCode(enum.StrEnum):
    """Lista cerrada de ``detail_code`` del módulo ``notifications``."""

    NOTIFICATION_PREFERENCE_NOT_OWNED = "notifications_notification_preference_not_owned"


NOTIFICATIONS_DETAIL_CODE_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "notifications_detail_code": NotificationsDetailCode,
}
"""Mensaje en español de cada código: el arranque falla si falta uno (NFR-LAZ-51)."""
