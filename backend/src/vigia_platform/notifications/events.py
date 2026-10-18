"""Eventos de las notificaciones en la bandeja (domain-entities.md de U-04, §6 y §11.3).

``notifications`` **no publica** ninguno de los 25 eventos de U-04: los consume (el motor de
notificaciones, TASK-324) y resuelve sus destinatarios. Los avisos de entrega del correo entran
por cola gestionada, no por la bandeja (nota de §6.2, respuesta 3). Este módulo existe para que el
registro de la unidad sea simétrico con ``loop`` y ``accreditation``: un evento que esta unidad
llegue a publicar se añade aquí y en ``U04_EVENT_TYPES``.
"""

from __future__ import annotations

from typing import Final

from vigia_platform.shared.outbox.registries import EventType, EventTypeRegistry

__all__ = ["NOTIFICATIONS_EVENT_TYPES", "register_notifications_event_types"]

NOTIFICATIONS_EVENT_TYPES: Final[tuple[EventType, ...]] = ()
"""Ninguno: ``notifications`` solo consume."""


def register_notifications_event_types(registry: EventTypeRegistry) -> None:
    """Registra los eventos de las notificaciones (hoy ninguno)."""
    for event_type in NOTIFICATIONS_EVENT_TYPES:
        registry.register(event_type)
