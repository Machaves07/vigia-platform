"""``email_delivery_events``: el consumidor de rebotes y quejas (TASK-325; LC-LAZ-34; PR-LAZ-58;
PAT-LAZ-RES-02; FS-LAZ-12).

El servicio de correo entrega sus avisos por una **cola gestionada** (``vigia-email-events-
<entorno>``), nunca por una ruta pública nueva (respuesta 3 B): ``notifications`` no publica
ninguno de los 25 eventos de U-04 (``notifications.events``), así que no hay ningún evento de la
bandeja al que suscribirse para sondear algo externo. Esta unidad añade el único que necesita,
puramente interno: ``email_delivery_events_poll`` (``EMAIL_DELIVERY_EVENTS_EVENT_TYPES``), un
pulso sin carga útil que una tarea periódica ``global`` publica cada 15 s
(``register_email_delivery_events_poll``) bajo el contexto del sistema de la proveedora. El
consumidor ``email_delivery_events`` (LC-LAZ-40: mamparo de concurrencia 2) está suscrito a ese
pulso: cada vez que lo recibe, sondea la cola de SQS **de verdad** (``receive_message``, cliente
heredado en un hilo, LC-NUC-30) hasta vaciarla o alcanzar su tope por ronda.

**Idempotencia** (PR-LAZ-58): por ``provider_message_id`` (el ``MessageId`` de SES). El consumidor
no conoce la organización del mensaje hasta encontrarla: igual que
``identity.invitation_organization`` para el enlace de activación,
``notifications.email_provider_message_lookup`` (``laz_0036``) es una función con seguridad
propia que solo devuelve ``organization_id``, ``message_id`` y ``queued_at`` de la clave con ese
identificador; el consumidor abre después su propia transacción con el contexto de esa
organización (como ``NotificationEngine._provider_side``) y actualiza bajo su seguridad a nivel
de fila de siempre. Un identificador que no aparece en la clave (mensaje ajeno, reentrega de un
envío que esta plataforma no hizo, o un mensaje malformado) no se puede procesar: el mensaje no se
borra y, agotado ``maxReceiveCount`` en la cola, termina en su cola muerta (infraestructura, sin
código de aplicación).

**Comportamiento declarado**: un **rebote duro** marca ``EmailMessage.status = failed`` con
``bounce_kind = hard`` y se declara (``NotificationDelivery.status = failed`` si hay una entrega
asociada, lo que hoy no ocurre para ``kind = platform_invitation``: no tiene entrega del motor de
notificaciones); un rebote **blando** solo se registra (``bounce_kind = soft``), sin suprimir
nada. ``bounce_kind`` es **monótono** (duro domina sobre blando, PR-LAZ-58; rework de revisión):
un blando que llega después de un duro ya aplicado no lo rebaja, así el resultado final no depende
del orden de los avisos. Una **queja** marca ``EmailMessage.complaint_at`` y silencia los niveles
``normal`` y ``digest`` del usuario (``UserNotificationPreference.email_suppressed_complaint``);
**nunca** ``immediate``: la entidad no tiene campo para eso (BR-LAZ-126). Las dos marcas de
supresión se escriben con ``INSERT ... ON CONFLICT`` sobre ``user_id`` (rework de revisión): la
fila de preferencia no existe hasta que el usuario la toca (LC-LAZ-35) o hasta este primer rebote
o queja, y la marca no se puede perder por falta de fila. El consumidor **no crea
notificaciones**: solo actualiza el estado de entrega y las marcas de supresión; el motor de
notificaciones sigue siendo el único que crea avisos. Cada campo se escribe con el mismo valor sin
importar el orden o la repetición de los avisos (conmutativo e idempotente, PR-LAZ-58).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from vigia_platform.notifications.metrics import record_email
from vigia_platform.shared.api.errors import ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    EventType,
    GlobalTaskScope,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

__all__ = [
    "EMAIL_DELIVERY_EVENTS",
    "EMAIL_DELIVERY_EVENTS_EVENT_TYPES",
    "EMAIL_DELIVERY_EVENTS_POLL",
    "MAX_MESSAGES_PER_ROUND",
    "POLL_SCHEDULE",
    "RECEIVE_BATCH_SIZE",
    "RECEIVE_WAIT_SECONDS",
    "EmailDeliveryEventsConsumer",
    "EmailDeliveryEventsPollTrigger",
    "SqsClient",
    "parse_notice",
    "register_email_delivery_events",
    "register_email_delivery_events_poll",
]

_log = get_logger("notifications.adapters.ses.delivery_events")

EMAIL_DELIVERY_EVENTS: Final = "email_delivery_events"
"""El nombre exacto que ``loop.worker.bulkheads.DEFAULT_CONCURRENCY`` ya reserva (LC-LAZ-40)."""
EMAIL_DELIVERY_EVENTS_POLL: Final = "email_delivery_events_poll"
POLL_SCHEDULE: Final = Schedule.every(15)
"""Cada 15 s: con la espera larga de SQS (20 s) de infraestructura, un rebote tarda poco en
procesarse sin sondear sin descanso (NFR-LAZ-39)."""
RECEIVE_BATCH_SIZE: Final = 10
"""El máximo de ``ReceiveMessage`` de SQS por llamada."""
RECEIVE_WAIT_SECONDS: Final = 5
MAX_MESSAGES_PER_ROUND: Final = 500
"""Tope de mensajes por invocación del consumidor (FS-LAZ-12: una ráfaga de 200 no se arrastra
indefinidamente entre pulsos)."""

_HARD: Final = "hard"
_SOFT: Final = "soft"
_DELIVERY_FAILED: Final = "failed"
_FAILURE_BOUNCED_HARD: Final = "email_bounced_hard"


class _PollTick(BaseModel):
    """La carga de ``email_delivery_events_poll``: ninguna (un pulso, no un hecho de negocio)."""

    model_config = ConfigDict(strict=True, extra="forbid")


EMAIL_DELIVERY_EVENTS_EVENT_TYPES: Final[tuple[EventType, ...]] = (
    EventType(
        event_name=EMAIL_DELIVERY_EVENTS_POLL,
        publisher_unit=ActorUnit.U04,
        payload_model=_PollTick,
        description_es=(
            "Pulso interno (sin carga) que dispara una ronda de sondeo de la cola de rebotes y "
            "quejas del correo"
        ),
    ),
)


class SqsClient(Protocol):
    """Lo que el consumidor usa del cliente ``sqs`` de ``boto3`` (``config=`` con tiempos de
    espera, VIG003; LC-NUC-30: siempre en un hilo, nunca en el bucle de eventos)."""

    def receive_message(self, **kwargs: Any) -> Any: ...

    def delete_message(self, **kwargs: Any) -> Any: ...


class EventContexts(Protocol):
    """Lo que el consumidor usa de ``ScopeContexts`` (U-02): el contexto del sistema de la
    organización de un mensaje encontrado, como el de un evento."""

    def context_from_event(
        self, event: Any, *, unit: ActorUnit = ActorUnit.U02
    ) -> ScopeContext: ...


class LookupDatabase(Protocol):
    """Lo que el consumidor usa de ``shared.db.Database``."""

    def transaction(self, context: ScopeContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class _Organization:
    """El «evento» con el que se pide el contexto del sistema de la organización encontrada."""

    organization_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Found:
    organization_id: uuid.UUID
    message_id: uuid.UUID
    queued_at: datetime


@dataclass(frozen=True, slots=True)
class _Notice:
    """Un aviso de SES ya interpretado: rebote (duro o blando) o queja."""

    provider_message_id: str
    bounce_kind: str | None
    """``hard``, ``soft`` o ``None`` si el aviso es una queja."""
    is_complaint: bool


_LOOKUP: Final = text(
    "SELECT organization_id, message_id, queued_at"
    " FROM notifications.email_provider_message_lookup(:provider_message_id)"
)
_UPDATE_BOUNCE: Final = text(
    "UPDATE notifications.email_message"
    " SET bounce_kind = CASE WHEN bounce_kind = 'hard' THEN 'hard' ELSE :bounce_kind END,"
    " status = CASE WHEN bounce_kind = 'hard' OR :bounce_kind = 'hard' THEN 'failed'"
    " ELSE status END"
    " WHERE message_id = :message_id AND queued_at = :queued_at"
    " RETURNING to_user_id, bounce_kind"
)
"""``bounce_kind`` es monótono (duro domina siempre sobre blando, PR-LAZ-58): el resultado no
depende del orden ni de la repetición de los avisos, también si un blando llega después de un
duro ya aplicado."""
_UPDATE_COMPLAINT: Final = text(
    "UPDATE notifications.email_message"
    " SET complaint_at = COALESCE(complaint_at, :now)"
    " WHERE message_id = :message_id AND queued_at = :queued_at"
    " RETURNING to_user_id"
)
_UPDATE_DELIVERY_BOUNCE: Final = text(
    "UPDATE notifications.notification_delivery"
    " SET status = :status, failure_code = :failure_code"
    " WHERE provider_message_id = :provider_message_id"
)
_UPSERT_SUPPRESS_HARD_BOUNCE: Final = text(
    "INSERT INTO notifications.user_notification_preference AS current_row"
    " (user_id, organization_id, email_enabled_normal, email_enabled_digest,"
    " weekly_digest_weekday, updated_at, email_suppressed_hard_bounce,"
    " email_suppressed_complaint, email_suppressed_at)"
    " VALUES (:user_id, :organization_id, true, true, 1, :now, true, false, :now)"
    " ON CONFLICT (user_id) DO UPDATE SET"
    " email_suppressed_hard_bounce = true,"
    " email_suppressed_at = COALESCE(current_row.email_suppressed_at, :now)"
)
_UPSERT_SUPPRESS_COMPLAINT: Final = text(
    "INSERT INTO notifications.user_notification_preference AS current_row"
    " (user_id, organization_id, email_enabled_normal, email_enabled_digest,"
    " weekly_digest_weekday, updated_at, email_suppressed_hard_bounce,"
    " email_suppressed_complaint, email_suppressed_at)"
    " VALUES (:user_id, :organization_id, true, true, 1, :now, false, true, :now)"
    " ON CONFLICT (user_id) DO UPDATE SET"
    " email_suppressed_complaint = true,"
    " email_suppressed_at = COALESCE(current_row.email_suppressed_at, :now)"
)
"""La fila de preferencia no existe hasta que el usuario la toca (``LC-LAZ-35``) o llega aquí: un
rebote duro o una queja del primer correo que recibe alguien que nunca abrió sus preferencias no
debe perderse por falta de fila (``ON CONFLICT`` sobre la clave primaria ``user_id``). Los valores
del ``INSERT`` para las columnas que no son la marca de supresión son los predeterminados de
``domain-entities`` §6.2; la rama ``DO UPDATE`` nunca los toca, solo la marca que corresponde."""


def parse_notice(body: str) -> _Notice | None:
    """El aviso de SES de ``body``: desenvuelve el sobre de SNS si lo lleva.

    ``None`` si no se puede interpretar (no es JSON, le falta ``mail.messageId`` o su tipo no es
    ``Bounce`` ni ``Complaint``): el mensaje queda sin procesar y, agotados los reintentos de la
    cola, termina en su cola muerta.
    """
    try:
        outer = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(outer, dict):
        return None
    envelope = outer
    if outer.get("Type") == "Notification" and isinstance(outer.get("Message"), str):
        try:
            inner = json.loads(outer["Message"])
        except (ValueError, TypeError):
            return None
        if not isinstance(inner, dict):
            return None
        envelope = inner
    mail = envelope.get("mail")
    if not isinstance(mail, dict):
        return None
    provider_message_id = mail.get("messageId")
    if not isinstance(provider_message_id, str) or not provider_message_id:
        return None
    notification_type = envelope.get("notificationType")
    if notification_type == "Bounce":
        bounce = envelope.get("bounce")
        bounce_type = bounce.get("bounceType") if isinstance(bounce, dict) else None
        kind = _HARD if bounce_type == "Permanent" else _SOFT
        return _Notice(provider_message_id, kind, is_complaint=False)
    if notification_type == "Complaint":
        return _Notice(provider_message_id, None, is_complaint=True)
    return None


class EmailDeliveryEventsPollTrigger:
    """El manejador de la tarea ``global`` ``email_delivery_events_poll``: publica el pulso
    interno que dispara una ronda del consumidor; ningún dato de cliente (TASK-220, D-7)."""

    def __init__(self, outbox: OutboxPort) -> None:
        self._outbox = outbox

    def __repr__(self) -> str:
        return "EmailDeliveryEventsPollTrigger()"

    async def __call__(self, scope: GlobalTaskScope) -> None:
        async with scope.control() as control:
            await self._outbox.publish(
                control, NewEvent(event_name=EMAIL_DELIVERY_EVENTS_POLL, payload={})
            )


class EmailDeliveryEventsConsumer:
    """El manejador de ``email_delivery_events`` (LC-LAZ-34): una ronda de sondeo por pulso."""

    def __init__(
        self,
        *,
        sqs: SqsClient,
        queue_url: str | None,
        database: LookupDatabase,
        provider_context: ScopeContext,
        contexts: EventContexts,
        clock: Clock,
    ) -> None:
        self._sqs = sqs
        self._queue_url = queue_url
        self._database = database
        self._provider_context = provider_context
        self._contexts = contexts
        self._clock = clock

    def __repr__(self) -> str:
        return "EmailDeliveryEventsConsumer()"

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        del event, transaction  # El pulso no lleva nada; cada mensaje abre su propia transacción.
        if self._queue_url is None:
            return  # Sin VIGIA_EMAIL_EVENTS_QUEUE_URL (fuera de pilot): nada que sondear.
        processed = 0
        while processed < MAX_MESSAGES_PER_ROUND:
            messages = await self._receive()
            if not messages:
                return
            for message in messages:
                await self._process_one(message)
                processed += 1
                if processed >= MAX_MESSAGES_PER_ROUND:
                    return

    async def _receive(self) -> list[dict[str, Any]]:
        try:
            response = await asyncio.to_thread(
                self._sqs.receive_message,
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=RECEIVE_BATCH_SIZE,
                WaitTimeSeconds=RECEIVE_WAIT_SECONDS,
            )
        except (BotoCoreError, ClientError) as error:
            raise ExternalDependencyDown("sqs") from error
        messages: object = response.get("Messages", [])
        return list(messages) if isinstance(messages, list) else []

    async def _process_one(self, message: dict[str, Any]) -> None:
        body: object = message.get("Body")
        receipt_handle: object = message.get("ReceiptHandle")
        if not isinstance(body, str) or not isinstance(receipt_handle, str):
            _log.warning("mensaje de la cola de correo sin cuerpo o sin recibo: se deja")
            return
        notice = parse_notice(body)
        if notice is None:
            _log.warning(
                "mensaje de la cola de correo no interpretable: se deja para su cola muerta"
            )
            return
        found = await self._lookup(notice.provider_message_id)
        if found is None:
            _log.warning("identificador del proveedor sin clave conocida: se deja")
            return
        await self._apply(found, notice)
        try:
            await asyncio.to_thread(
                self._sqs.delete_message, QueueUrl=self._queue_url, ReceiptHandle=receipt_handle
            )
        except (BotoCoreError, ClientError) as error:
            raise ExternalDependencyDown("sqs") from error

    async def _lookup(self, provider_message_id: str) -> _Found | None:
        async with self._database.transaction(self._provider_context) as transaction:
            rows = (
                await transaction.execute(_LOOKUP, {"provider_message_id": provider_message_id})
            ).all()
        if not rows:
            return None
        row = rows[0]
        return _Found(
            uuid.UUID(str(row.organization_id)), uuid.UUID(str(row.message_id)), row.queued_at
        )

    async def _apply(self, found: _Found, notice: _Notice) -> None:
        now = self._clock.now()
        context = self._contexts.context_from_event(
            _Organization(found.organization_id, uuid7(self._clock)), unit=ActorUnit.U04
        )
        async with self._database.transaction(context) as transaction:
            if notice.is_complaint:
                await self._apply_complaint(transaction, found, now)
            else:
                await self._apply_bounce(transaction, found, notice, now)
        record_email("complained" if notice.is_complaint else "bounced", found.organization_id)

    @staticmethod
    async def _apply_complaint(transaction: Any, found: _Found, now: datetime) -> None:
        parameters = {"message_id": found.message_id, "queued_at": found.queued_at, "now": now}
        result = await transaction.execute(_UPDATE_COMPLAINT, parameters)
        row = result.first()
        if row is None:
            return
        await transaction.execute(
            _UPSERT_SUPPRESS_COMPLAINT,
            {"user_id": row.to_user_id, "organization_id": found.organization_id, "now": now},
        )

    @staticmethod
    async def _apply_bounce(
        transaction: Any, found: _Found, notice: _Notice, now: datetime
    ) -> None:
        result = await transaction.execute(
            _UPDATE_BOUNCE,
            {
                "bounce_kind": notice.bounce_kind,
                "message_id": found.message_id,
                "queued_at": found.queued_at,
            },
        )
        row = result.first()
        # ``bounce_kind`` monótono: refleja si ESTE aviso o uno anterior ya fue duro, sin importar
        # el orden de llegada (PR-LAZ-58).
        if row is None or row.bounce_kind != _HARD:
            return
        to_user_id = row.to_user_id
        await transaction.execute(
            _UPDATE_DELIVERY_BOUNCE,
            {
                "status": _DELIVERY_FAILED,
                "failure_code": _FAILURE_BOUNCED_HARD,
                "provider_message_id": notice.provider_message_id,
            },
        )
        await transaction.execute(
            _UPSERT_SUPPRESS_HARD_BOUNCE,
            {"user_id": to_user_id, "organization_id": found.organization_id, "now": now},
        )


def register_email_delivery_events_poll(
    registry: PeriodicTaskRegistry, trigger: EmailDeliveryEventsPollTrigger
) -> PeriodicTask:
    """Registra la tarea ``global`` que dispara el pulso de ``email_delivery_events``."""
    return registry.register(
        EMAIL_DELIVERY_EVENTS_POLL,
        POLL_SCHEDULE,
        trigger,
        unit=ActorUnit.U04,
        iteration=TaskIteration.GLOBAL,
    )


def register_email_delivery_events(
    registry: ConsumerRegistry, consumer: EmailDeliveryEventsConsumer
) -> Consumer:
    """Registra ``email_delivery_events`` (LC-LAZ-34), con dependencia externa declarada (SQS):
    un fallo de red abre su propio circuito (PAT-LAZ-RES-04), sin tocar el de ``SesEmailSender``."""
    return registry.register(
        Consumer(
            consumer_name=EMAIL_DELIVERY_EVENTS,
            unit=ActorUnit.U04,
            subscribed_events=(EMAIL_DELIVERY_EVENTS_POLL,),
            handler=consumer,
            has_external_dependency=True,
        )
    )
