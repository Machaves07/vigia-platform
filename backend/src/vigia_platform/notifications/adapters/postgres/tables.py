"""Tablas del esquema ``notifications`` en SQLAlchemy Core (TASK-339, LC-LAZ-02).

Reflejan la migración ``laz_0030_notifications_schema``, que es la fuente de verdad: las
restricciones (listas cerradas, longitudes, claves foráneas hacia ``identity``), la seguridad a
nivel de fila, las particiones mensuales, los índices, los disparadores de las claves de
idempotencia y los permisos viven allí y no se repiten aquí. Estas definiciones solo dan nombre y
tipo a las columnas para los repositorios de U-04;
``tests/integration/test_laz_notifications_schema.py`` comprueba que coinciden con la base.

``USER_NOTIFICATION_PREFERENCE`` no tiene ninguna columna para silenciar ``immediate``
(BR-LAZ-126). ``NOTIFICATION_SOURCE_KEY`` y ``EMAIL_PROVIDER_MESSAGE_KEY`` solo se leen: las llenan
los disparadores de la base.

Sin ORM y sin repositorios: toda consulta se abre con un ``ScopeContext`` (BR-NUC-02) desde la
tarea que la necesite. Las marcas de tiempo no tienen valor por defecto: las pone la aplicación
con su ``Clock``.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import Boolean, Column, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.types import TIMESTAMP

__all__ = [
    "DIGEST_BATCH",
    "EMAIL_MESSAGE",
    "EMAIL_PROVIDER_MESSAGE_KEY",
    "METADATA",
    "NOTIFICATION",
    "NOTIFICATION_DELIVERY",
    "NOTIFICATION_SOURCE_KEY",
    "SCHEMA",
    "USER_NOTIFICATION_PREFERENCE",
]

SCHEMA: Final = "notifications"
METADATA: Final = MetaData(schema=SCHEMA)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, UUID(as_uuid=True), nullable=nullable, primary_key=primary_key)


def _instant(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, TIMESTAMP(timezone=True), nullable=nullable, primary_key=primary_key)


def _text(name: str, *, nullable: bool = False, primary_key: bool = False) -> Column[Any]:
    return Column(name, Text, nullable=nullable, primary_key=primary_key)


def _boolean(name: str) -> Column[Any]:
    return Column(name, Boolean, nullable=False)


def _uuids(name: str) -> Column[Any]:
    return Column(name, ARRAY(UUID(as_uuid=True)), nullable=False)


NOTIFICATION: Final = Table(
    "notification",
    METADATA,
    _uuid("notification_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id", nullable=True),
    _uuid("zone_id", nullable=True),
    _text("event_name"),
    _uuid("source_event_id"),
    _text("urgency"),
    _text("title_es"),
    _text("body_es"),
    Column("deep_link_ref", JSONB, nullable=False),
    _uuid("recipient_user_id"),
    _instant("created_at", primary_key=True),
    _instant("read_at", nullable=True),
    _instant("archived_at", nullable=True),
)
"""``Notification`` (§6.2), particionada por ``created_at``."""

NOTIFICATION_SOURCE_KEY: Final = Table(
    "notification_source_key",
    METADATA,
    _uuid("organization_id", primary_key=True),
    _uuid("source_event_id", primary_key=True),
    _uuid("recipient_user_id", primary_key=True),
    _uuid("plant_id", nullable=True),
    _uuid("notification_id"),
    _instant("created_at"),
)
"""La clave de idempotencia de ``Notification`` (BR-LAZ-129)."""

DIGEST_BATCH: Final = Table(
    "digest_batch",
    METADATA,
    _uuid("digest_batch_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("recipient_user_id"),
    _text("kind"),
    _instant("window_from"),
    _instant("window_to"),
    _uuids("notification_ids"),
    Column("item_count", Integer, nullable=False),
    _instant("sent_at", nullable=True),
    _text("status"),
)
"""``DigestBatch`` (§6.2)."""

NOTIFICATION_DELIVERY: Final = Table(
    "notification_delivery",
    METADATA,
    _uuid("delivery_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("plant_id", nullable=True),
    _uuid("zone_id", nullable=True),
    _uuid("notification_id"),
    _instant("notification_created_at", primary_key=True),
    _text("channel"),
    _text("status"),
    Column("attempts", Integer, nullable=False),
    _instant("scheduled_for", nullable=True),
    _instant("sent_at", nullable=True),
    _text("failure_code", nullable=True),
    _uuid("digest_batch_id", nullable=True),
    _text("provider_message_id", nullable=True),
)
"""``NotificationDelivery`` (§6.2), particionada por la marca de su aviso."""

USER_NOTIFICATION_PREFERENCE: Final = Table(
    "user_notification_preference",
    METADATA,
    _uuid("user_id", primary_key=True),
    _uuid("organization_id"),
    _boolean("email_enabled_normal"),
    _boolean("email_enabled_digest"),
    Column("weekly_digest_weekday", Integer, nullable=False),
    _instant("updated_at"),
    _boolean("email_suppressed_hard_bounce"),
    _boolean("email_suppressed_complaint"),
    _instant("email_suppressed_at", nullable=True),
)
"""``UserNotificationPreference`` (§6.2 y su nota de 2026-09-21): sin campo para ``immediate``."""

EMAIL_MESSAGE: Final = Table(
    "email_message",
    METADATA,
    _uuid("message_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("to_user_id"),
    _text("subject_es"),
    _text("body_es", nullable=True),
    _text("kind"),
    _uuids("related_ids"),
    _instant("queued_at", primary_key=True),
    _instant("sent_at", nullable=True),
    _text("status"),
    _text("failure_code", nullable=True),
    _text("provider_message_id", nullable=True),
    _text("bounce_kind", nullable=True),
    _instant("complaint_at", nullable=True),
)
"""``EmailMessage`` (§6.2), particionada por ``queued_at``; ``body_es`` caduca a los 90 días."""

EMAIL_PROVIDER_MESSAGE_KEY: Final = Table(
    "email_provider_message_key",
    METADATA,
    _text("provider_message_id", primary_key=True),
    _uuid("organization_id"),
    _uuid("message_id"),
    _instant("queued_at"),
)
"""La clave de idempotencia de ``EmailMessage`` (PR-LAZ-58)."""
