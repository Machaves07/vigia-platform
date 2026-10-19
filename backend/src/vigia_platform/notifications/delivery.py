"""Entrega de una notificación (TASK-324; LC-LAZ-31; BR-LAZ-125, 127, 129 y 130; PAT-LAZ-RES-04;
A-69).

Todo en la transacción que recibe, por destinatario:

1. el aviso en la aplicación (``notifications.notification``), **de inmediato y para toda
   urgencia**, con su entrega ``in_app`` ya ``sent``;
2. la entrega por correo, según ``EmailPlan``:

   - ``GROUPED``: ``normal`` y ``digest`` con correo en la fila. La entrega queda ``pending`` sin
     lote (``digest_batch_id`` nulo): la agrupación y el resumen semanal son de TASK-348;
   - ``QUEUED``: ``immediate`` con el correo disponible. El ``EmailMessage`` entra ``queued``
     (``kind = notification``) en la misma transacción que el aviso, así que una reentrega no
     crea un segundo correo (PR-LAZ-40); lo envía el adaptador del ``EmailSenderPort``
     (TASK-325). La entrega queda ``pending``;
   - ``DEGRADED``: ``immediate`` sin ``EmailSenderPort`` registrado, con su cortacircuito
     ``open`` o desde un contexto de concesión. La entrega queda ``degraded_app_only`` con su
     ``failure_code``: el aviso sigue en la aplicación y la degradación se declara al usuario
     (BR-LAZ-130), nada se pierde.

   ``integrity_compromised`` (``bypasses_email_circuit``) se encola **siempre**, con o sin puerto
   y con el circuito abierto (BR-LAZ-127).

**A-69**: desde un contexto de concesión, el motor nunca escribe ``email_message`` (ni lee
``user_notification_preference``, ni toca ``digest_batch``): la política RESTRICTIVE de esas
tablas no lo admitiría, y aquí ni se intenta.

**Idempotencia** (BR-LAZ-129): la clave es ``notification_source_key`` (organización, evento,
destinatario), que llena el disparador del aviso en el mismo ``INSERT``. ``already_delivered`` mira
si el evento ya dejó alguna clave en la organización: la reentrega termina sin efecto. Dos entregas
**simultáneas** del mismo evento chocan en la clave (``unique_violation``): la segunda falla, se
revierte entera (también su correo) y su reintento ve la primera ya confirmada.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Final, Literal, Protocol

from sqlalchemy import exists, insert, select

from vigia_platform.loop.domain.enums import (
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationUrgency,
)
from vigia_platform.notifications.adapters.postgres.tables import (
    EMAIL_MESSAGE,
    NOTIFICATION,
    NOTIFICATION_DELIVERY,
    NOTIFICATION_SOURCE_KEY,
)
from vigia_platform.notifications.catalog import CatalogEntry
from vigia_platform.notifications.recipients import DeepLink
from vigia_platform.notifications.templates import RenderedNotification
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "CONCESSION_CONTEXT",
    "EMAIL_CIRCUIT_OPEN",
    "EMAIL_SENDER_ABSENT",
    "EmailCircuit",
    "EmailPlan",
    "EmailSenders",
    "NotificationDraft",
    "already_delivered",
    "email_plan",
    "write_notification",
]

EMAIL_SENDER_ABSENT: Final = "email_sender_absent"
"""``failure_code`` de ``degraded_app_only``: no hay ``EmailSenderPort`` registrado."""
EMAIL_CIRCUIT_OPEN: Final = "email_circuit_open"
"""``failure_code`` de ``degraded_app_only``: el cortacircuito del correo está abierto."""
CONCESSION_CONTEXT: Final = "provider_concession_context"
"""``failure_code`` de ``degraded_app_only``: el evento se procesó bajo concesión (A-69)."""
EMAIL_KIND: Final = "notification"
EMAIL_QUEUED: Final = "queued"


class EmailCircuit(Protocol):
    """Lo que el motor usa del ``EmailSenderPort`` de U-02: solo el estado de su circuito."""

    def circuit_state(self) -> Literal["closed", "open", "half_open"]: ...


class EmailSenders(Protocol):
    """``EmailSenderRegistry`` de U-02 (``identity.application.invitations``)."""

    @property
    def sender(self) -> EmailCircuit | None: ...


class EmailPlan(enum.Enum):
    NONE = "none"
    """La fila no tiene canal de correo."""
    GROUPED = "grouped"
    QUEUED = "queued"
    DEGRADED = "degraded"


def email_plan(
    entry: CatalogEntry,
    urgency: NotificationUrgency,
    senders: EmailSenders,
    *,
    under_concession: bool,
) -> tuple[EmailPlan, str | None]:
    """El plan del correo del evento y, si se degrada, su ``failure_code``.

    El circuito se mira **una vez por evento**: todos sus destinatarios reciben el mismo plan.
    """
    if not entry.emails:
        return EmailPlan.NONE, None
    if urgency is not NotificationUrgency.IMMEDIATE:
        return EmailPlan.GROUPED, None
    if under_concession:
        return EmailPlan.DEGRADED, CONCESSION_CONTEXT
    if entry.bypasses_email_circuit:
        return EmailPlan.QUEUED, None
    sender = senders.sender
    if sender is None:
        return EmailPlan.DEGRADED, EMAIL_SENDER_ABSENT
    if sender.circuit_state() == "open":
        return EmailPlan.DEGRADED, EMAIL_CIRCUIT_OPEN
    return EmailPlan.QUEUED, None


class NotificationDraft(Protocol):
    """Lo común a todos los destinatarios de un evento en una organización."""

    @property
    def entry(self) -> CatalogEntry: ...
    @property
    def source_event_id(self) -> uuid.UUID: ...
    @property
    def urgency(self) -> NotificationUrgency: ...
    @property
    def plant_id(self) -> uuid.UUID | None: ...
    @property
    def zone_id(self) -> uuid.UUID | None: ...
    @property
    def content(self) -> RenderedNotification: ...
    @property
    def deep_link(self) -> DeepLink: ...


async def already_delivered(transaction: Transaction, source_event_id: uuid.UUID) -> bool:
    """¿Dejó ya el evento algún aviso en la organización de la transacción?"""
    statement = select(
        exists().where(
            NOTIFICATION_SOURCE_KEY.c.organization_id == transaction.context.organization_id,
            NOTIFICATION_SOURCE_KEY.c.source_event_id == source_event_id,
        )
    )
    return bool((await transaction.execute(statement)).scalar_one())


async def write_notification(
    transaction: Transaction,
    draft: NotificationDraft,
    recipient_user_id: uuid.UUID,
    plan: tuple[EmailPlan, str | None],
    clock: Clock,
) -> uuid.UUID:
    """El aviso de ``recipient_user_id`` con sus entregas; devuelve su ``notification_id``."""
    organization_id = transaction.context.organization_id
    now: datetime = clock.now()
    notification_id = uuid7(clock)
    await transaction.execute(
        insert(NOTIFICATION).values(
            notification_id=notification_id,
            organization_id=organization_id,
            plant_id=draft.plant_id,
            zone_id=draft.zone_id,
            event_name=draft.entry.event_name,
            source_event_id=draft.source_event_id,
            urgency=draft.urgency.value,
            title_es=draft.content.title_es,
            body_es=draft.content.body_es,
            deep_link_ref=draft.deep_link.as_json(),
            recipient_user_id=recipient_user_id,
            created_at=now,
        )
    )
    delivery = {
        "organization_id": organization_id,
        "plant_id": draft.plant_id,
        "zone_id": draft.zone_id,
        "notification_id": notification_id,
        "notification_created_at": now,
    }
    await transaction.execute(
        insert(NOTIFICATION_DELIVERY).values(
            delivery_id=uuid7(clock),
            channel=NotificationChannel.IN_APP.value,
            status=NotificationDeliveryStatus.SENT.value,
            attempts=1,
            sent_at=now,
            **delivery,
        )
    )
    kind, failure_code = plan
    if kind is EmailPlan.NONE:
        return notification_id
    status = NotificationDeliveryStatus.PENDING
    if kind is EmailPlan.DEGRADED:
        status = NotificationDeliveryStatus.DEGRADED_APP_ONLY
    elif kind is EmailPlan.QUEUED:
        await transaction.execute(
            insert(EMAIL_MESSAGE).values(
                message_id=uuid7(clock),
                organization_id=organization_id,
                to_user_id=recipient_user_id,
                subject_es=draft.content.title_es,
                body_es=draft.content.body_es,
                kind=EMAIL_KIND,
                related_ids=[notification_id],
                queued_at=now,
                status=EMAIL_QUEUED,
            )
        )
    await transaction.execute(
        insert(NOTIFICATION_DELIVERY).values(
            delivery_id=uuid7(clock),
            channel=NotificationChannel.EMAIL.value,
            status=status.value,
            attempts=0,
            failure_code=failure_code,
            **delivery,
        )
    )
    return notification_id
