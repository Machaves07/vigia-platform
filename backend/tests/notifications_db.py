"""Filas sintéticas del esquema ``notifications`` para las pruebas (TASK-339).

``ROW_BUILDERS`` da, por tabla, la sentencia y los parámetros de una fila nueva y válida de una
planta (``NotificationScope``): ``seed_notifications`` siembra con ellos al menos una fila de cada
una de las siete tablas en cada planta de los dos clientes de ``tests/identity_db.py`` (el par de
organizaciones de U-02), y las pruebas de aislamiento los reutilizan para intentar escribir desde
un contexto de proveedor. La entrega apunta al aviso y al lote del ``NotificationScope``, que
``seed_plant`` crea primero. La preferencia es una por usuario: la siembra la primera planta. Las
tablas de clave las llena el disparador de su tabla; sus constructores escriben la clave
directamente (solo el superusuario puede: ``vigia_app`` no tiene ``INSERT``).

Solo datos generados (NFR-CTR-43). Las marcas salen de ``BASE_TIME`` o del instante que se pase,
nunca de la hora real.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from tests.identity_db import BASE_TIME, IdentitySeed, Tenant, set_scope

ENTITY_TABLES: Final = (
    "notification",
    "notification_delivery",
    "digest_batch",
    "user_notification_preference",
    "email_message",
)
"""Las cinco entidades de ``domain-entities.md`` §6.2 (``laz_0030``)."""

KEY_TABLES: Final = ("notification_source_key", "email_provider_message_key")
"""Las claves de idempotencia sin particionar."""

NOTIFICATIONS_TABLES: Final = (*ENTITY_TABLES, *KEY_TABLES)
"""Las siete tablas de ``notifications``."""

ORGANIZATION_ONLY_TABLES: Final = (
    "digest_batch",
    "user_notification_preference",
    "email_message",
    "email_provider_message_key",
)
"""Las de solo organización (A-69): ninguna fila bajo una concesión de proveedor."""

PLANT_TABLES: Final = ("notification", "notification_delivery", "notification_source_key")
"""Las que llevan ``plant_id`` (opcional)."""

SINGLE_ROW_TABLES: Final = frozenset({"user_notification_preference"})
"""Tablas cuya fila nueva choca por clave con la sembrada (una por usuario)."""


@dataclass(frozen=True)
class NotificationScope:
    """Planta de un cliente con las filas de las que dependen las demás."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    user_id: uuid.UUID
    notification_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    notification_created_at: dt.datetime = BASE_TIME
    digest_batch_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)


Statement = tuple[str, list[Any]]
Builder = Callable[[NotificationScope], Statement]


def insert(table: str, values: dict[str, Any]) -> Statement:
    """``INSERT INTO notifications.<table>`` con los valores en orden."""
    columns = ", ".join(values)
    placeholders = ", ".join(f"${index}" for index in range(1, len(values) + 1))
    sql = f"INSERT INTO notifications.{table} ({columns}) VALUES ({placeholders})"  # noqa: S608
    return sql, list(values.values())


def notification(
    scope: NotificationScope,
    at: dt.datetime = BASE_TIME,
    *,
    notification_id: uuid.UUID | None = None,
    source_event_id: uuid.UUID | None = None,
    recipient_user_id: uuid.UUID | None = None,
    with_plant: bool = True,
) -> Statement:
    """Un aviso de la zona de ``scope`` (o de la organización, sin planta ni zona)."""
    return insert(
        "notification",
        {
            "notification_id": notification_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id if with_plant else None,
            "zone_id": scope.zone_id if with_plant else None,
            "event_name": "finding_received",
            "source_event_id": source_event_id or uuid.uuid4(),
            "urgency": "normal",
            "title_es": "Hallazgo nuevo en la zona sintética",
            "body_es": "Hay un hallazgo nuevo por clasificar en la zona sintética.",
            "deep_link_ref": json.dumps({"kind": "finding", "id": str(uuid.uuid4())}),
            "recipient_user_id": recipient_user_id or scope.user_id,
            "created_at": at,
        },
    )


def notification_source_key(scope: NotificationScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "notification_source_key",
        {
            "organization_id": scope.organization_id,
            "source_event_id": uuid.uuid4(),
            "recipient_user_id": scope.user_id,
            "plant_id": scope.plant_id,
            "notification_id": uuid.uuid4(),
            "created_at": at,
        },
    )


def digest_batch(
    scope: NotificationScope,
    at: dt.datetime = BASE_TIME,
    *,
    digest_batch_id: uuid.UUID | None = None,
) -> Statement:
    return insert(
        "digest_batch",
        {
            "digest_batch_id": digest_batch_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "recipient_user_id": scope.user_id,
            "kind": "email_grouped",
            "window_from": at,
            "window_to": at + dt.timedelta(hours=4),
            "notification_ids": [scope.notification_id],
            "item_count": 1,
            "status": "pending",
        },
    )


def notification_delivery(
    scope: NotificationScope, at: dt.datetime = BASE_TIME, *, in_batch: bool = True
) -> Statement:
    """Una entrega por correo del aviso de ``scope``, en el lote de ``scope`` (o sin lote)."""
    return insert(
        "notification_delivery",
        {
            "delivery_id": uuid.uuid4(),
            "organization_id": scope.organization_id,
            "plant_id": scope.plant_id,
            "zone_id": scope.zone_id,
            "notification_id": scope.notification_id,
            "notification_created_at": scope.notification_created_at,
            "channel": "email",
            "status": "pending",
            "scheduled_for": at + dt.timedelta(hours=4),
            "digest_batch_id": scope.digest_batch_id if in_batch else None,
        },
    )


def user_notification_preference(
    scope: NotificationScope, at: dt.datetime = BASE_TIME
) -> Statement:
    return insert(
        "user_notification_preference",
        {
            "user_id": scope.user_id,
            "organization_id": scope.organization_id,
            "weekly_digest_weekday": 1,
            "updated_at": at,
        },
    )


def email_message(
    scope: NotificationScope,
    at: dt.datetime = BASE_TIME,
    *,
    message_id: uuid.UUID | None = None,
    provider_message_id: str | None = None,
) -> Statement:
    return insert(
        "email_message",
        {
            "message_id": message_id or uuid.uuid4(),
            "organization_id": scope.organization_id,
            "to_user_id": scope.user_id,
            "subject_es": "Hallazgo nuevo en la zona sintética",
            "body_es": "Entra a la aplicación para ver el hallazgo de la zona sintética.",
            "kind": "notification",
            "related_ids": [scope.notification_id],
            "queued_at": at,
            "status": "queued",
            "provider_message_id": provider_message_id,
        },
    )


def email_provider_message_key(scope: NotificationScope, at: dt.datetime = BASE_TIME) -> Statement:
    return insert(
        "email_provider_message_key",
        {
            "provider_message_id": f"proveedor-{secrets.token_hex(8)}",
            "organization_id": scope.organization_id,
            "message_id": uuid.uuid4(),
            "queued_at": at,
        },
    )


ROW_BUILDERS: Final[dict[str, Builder]] = {
    "notification": notification,
    "notification_delivery": notification_delivery,
    "digest_batch": digest_batch,
    "user_notification_preference": user_notification_preference,
    "email_message": email_message,
    "notification_source_key": notification_source_key,
    "email_provider_message_key": email_provider_message_key,
}
"""Una fila nueva por tabla, que solo escribe en esa tabla (el aviso y el correo con
``provider_message_id`` reclaman además su clave). La de ``SINGLE_ROW_TABLES`` choca por clave
con la sembrada."""

assert tuple(ROW_BUILDERS) == NOTIFICATIONS_TABLES


def plant_scopes(tenant: Tenant) -> tuple[NotificationScope, ...]:
    return tuple(
        NotificationScope(tenant.organization_id, plant.plant_id, plant.zone_id, tenant.user_id)
        for plant in tenant.plants
    )


async def seed_plant(connection: Any, scope: NotificationScope, *, first: bool) -> None:
    """Las filas de las que dependen las demás y una fila de cada tabla (como superusuario).

    ``first``: la primera planta de la organización, que siembra la preferencia del usuario y un
    aviso de la organización (sin planta ni zona).
    """
    dependencies: list[Statement] = [
        notification(scope, scope.notification_created_at, notification_id=scope.notification_id),
        digest_batch(scope, digest_batch_id=scope.digest_batch_id),
        email_message(scope, provider_message_id=f"proveedor-{secrets.token_hex(8)}"),
    ]
    if first:
        dependencies.append(notification(scope, with_plant=False))
    for sql, args in dependencies:
        await connection.execute(sql, *args)
    for table in NOTIFICATIONS_TABLES:
        if table == "user_notification_preference" and not first:  # una por usuario
            continue
        sql, args = ROW_BUILDERS[table](scope)
        await connection.execute(sql, *args)


@dataclass(frozen=True)
class NotificationsSeed:
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[NotificationScope, ...]]
    """Plantas sembradas de cada organización cliente."""

    def plant(self, organization_id: uuid.UUID, index: int = 0) -> NotificationScope:
        return self.scopes[organization_id][index]


async def seed_notifications(connection: Any, seed: IdentitySeed) -> NotificationsSeed:
    """Filas de cada tabla de ``notifications`` en cada planta de A y de B (superusuario)."""
    scopes: dict[uuid.UUID, tuple[NotificationScope, ...]] = {}
    async with connection.transaction():
        for tenant in (seed.a, seed.b):
            # Los disparadores de las claves corren como su dueño, con la RLS forzada: la clave
            # solo entra con la organización del contexto, también si siembra el superusuario.
            await set_scope(connection, tenant.organization_id)
            scopes[tenant.organization_id] = plant_scopes(tenant)
            for index, scope in enumerate(scopes[tenant.organization_id]):
                await seed_plant(connection, scope, first=index == 0)
    return NotificationsSeed(seed, scopes)
