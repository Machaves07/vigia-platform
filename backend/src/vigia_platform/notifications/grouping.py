"""Tarea periódica ``email_grouping`` (TASK-348; LC-LAZ-35; PAT-LAZ-RES-05; BR-LAZ-125, 126, 130;
PR-LAZ-42, 48).

Cada hora, por organización: para cada destinatario con entregas de correo ``normal`` pendientes
de agrupar (``NotificationDelivery.channel = email``, ``status = pending``,
``digest_batch_id IS NULL``, ``Notification.urgency = normal``), si ya pasó
``email_grouping_hours`` desde el cierre de su último lote, un **solo** ``DigestBatch``
(``kind = email_grouped``) con todo lo acumulado y **un solo** ``EmailMessage``
(``kind = digest``). Tras una caída de cualquier duración, la siguiente ejecución encuentra que
ya toca (la ventana lleva más de ``email_grouping_hours`` abierta) y agrupa todo de una vez: nunca
una ráfaga, porque la ventana solo se cierra una vez por lote.

**Silenciado** (BR-LAZ-126): si ``email_enabled_normal`` es falso o el correo del usuario está
suprimido (rebote duro o queja, TASK-325), las entregas pendientes se marcan
``suppressed_by_preference`` de inmediato, sin lote ni ventana: el aviso en la aplicación no se
toca (nunca se silencia, BR-LAZ-125).

**Correo caído** (BR-LAZ-130): sin ``EmailSenderPort`` registrado o con su circuito abierto, el
lote se escribe igual (para cerrar la ventana y no repetir el intento cada hora) pero sin
``EmailMessage``; las entregas quedan ``degraded_app_only`` con su ``failure_code``. El aviso
sigue visible en la aplicación: nada se pierde.

**Tope de 200 correos por hora y organización** ``[estimación propia]`` (NFR-LAZ-13): bajo el
mismo candado consultivo de organización que ``adapters.ses.sender._admit`` (orden único:
organización), se cuentan los ``EmailMessage`` de ``kind = digest`` de la última hora **antes**
de insertar el ``DigestBatch`` y reclamar las entregas, no después: sin presupuesto, nada se
escribe (ni lote ni reclamo) y el destinatario se deja tal cual para la siguiente ejecución (su
ventana no se cierra, así que no se pierde ni se duplica nada). Comprobarlo después de reclamar
dejaba las entregas con ``digest_batch_id`` puesto pero el lote ``queued`` para siempre, fuera del
alcance de ``candidates_statement`` (que solo ve ``digest_batch_id IS NULL``): un hallazgo real de
la ronda de revisión, no solo de las pruebas.

**Garantía de «una sola vez» entre ejecuciones concurrentes** (PR-LAZ-48): el ``DigestBatch`` se
inserta sin condición (puede haber, transitoriamente, más de uno para la misma ventana si dos
ejecuciones corren a la vez), pero **reclamar** las entregas es lo que de verdad decide quién
gana: ``UPDATE notification_delivery ... WHERE digest_batch_id IS NULL`` toma el bloqueo de fila de
cada entrega; la segunda ejecución, al evaluar la misma condición tras que la primera confirme, no
encuentra ninguna fila libre y su lote se queda vacío, así que se borra sin enviar nada. Sin la
condición ``digest_batch_id IS NULL`` en ese ``UPDATE`` (la clave de la garantía), las dos
ejecuciones reclamarían las mismas entregas y **enviarían el correo dos veces**: así lo demuestra
la mutación de ``test_laz_grouping_catch_up.py``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.engine import Row

from vigia_platform.loop.config.parameters import INTEGER_PARAMETERS
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import (
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationUrgency,
)
from vigia_platform.notifications.adapters.postgres.tables import (
    DIGEST_BATCH,
    EMAIL_MESSAGE,
    NOTIFICATION,
    NOTIFICATION_DELIVERY,
)
from vigia_platform.notifications.delivery import (
    EMAIL_CIRCUIT_OPEN,
    EMAIL_SENDER_ABSENT,
    EmailSenders,
)
from vigia_platform.notifications.metrics import record_email
from vigia_platform.notifications.preferences import preference_for
from vigia_platform.notifications.reader import archive_read_notifications
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

__all__ = [
    "DIGEST_EMAIL_KIND",
    "EMAIL_GROUPED_KIND",
    "EMAIL_GROUPING",
    "EMAIL_GROUPING_SCHEDULE",
    "ORGANIZATION_EMAIL_CAP_PER_HOUR",
    "Candidate",
    "DigestGrouperBase",
    "EmailGroupingTask",
    "candidates_statement",
    "compose",
    "last_window_to",
    "register_email_grouping",
]

EMAIL_GROUPING: Final = "email_grouping"
EMAIL_GROUPING_SCHEDULE: Final = Schedule.every(3600)
"""Sondea cada hora; solo agrupa cuando la ventana del destinatario ya cumplió sus horas
(LC §6.9)."""

EMAIL_GROUPED_KIND: Final = "email_grouped"
DIGEST_EMAIL_KIND: Final = "digest"
_STATUS_QUEUED: Final = "queued"
_STATUS_SENT: Final = "sent"
_STATUS_DEGRADED: Final = "degraded"
_STATUS_SUPERSEDED: Final = "superseded"
"""Una ejecución concurrente reclamó las entregas primero: este lote nunca envía nada."""
ORGANIZATION_EMAIL_CAP_PER_HOUR: Final = 200
"""Tope de ``EmailMessage`` de ``kind = digest`` por organización y hora (NFR-LAZ-13)."""
_RATE_WINDOW_SECONDS: Final = 3600
_MAX_BODY_CHARS: Final = 7900
_MAX_SUBJECT_CHARS: Final = 120

_LOCK_ORGANIZATION: Final = text("SELECT pg_advisory_xact_lock(hashtext(:organization)::bigint)")
"""Mismo candado y mismo orden (solo organización) que ``adapters.ses.sender._LOCK_ORGANIZATION``:
ambos cuentan ``email_message`` de la última hora antes de admitir uno más."""


@dataclass(frozen=True, slots=True)
class Candidate:
    notification_id: uuid.UUID
    created_at: datetime
    plant_id: uuid.UUID | None
    title_es: str
    body_es: str


def candidates_statement(organization_id: uuid.UUID, urgency: NotificationUrgency) -> Any:
    d = NOTIFICATION_DELIVERY
    n = NOTIFICATION
    return (
        select(
            n.c.recipient_user_id,
            n.c.plant_id,
            d.c.notification_id,
            n.c.created_at,
            n.c.title_es,
            n.c.body_es,
        )
        .select_from(
            d.join(
                n,
                and_(
                    d.c.organization_id == n.c.organization_id,
                    d.c.notification_id == n.c.notification_id,
                    d.c.notification_created_at == n.c.created_at,
                ),
            )
        )
        .where(
            d.c.organization_id == organization_id,
            d.c.channel == NotificationChannel.EMAIL.value,
            d.c.status == NotificationDeliveryStatus.PENDING.value,
            d.c.digest_batch_id.is_(None),
            n.c.urgency == urgency.value,
        )
        .order_by(n.c.recipient_user_id, n.c.created_at, d.c.notification_id)
    )


def last_window_to(transaction: Transaction, recipient_user_id: uuid.UUID, kind: str) -> Any:
    b = DIGEST_BATCH
    return select(func.max(b.c.window_to)).where(
        b.c.organization_id == transaction.context.organization_id,
        b.c.recipient_user_id == recipient_user_id,
        b.c.kind == kind,
    )


def compose(subject_prefix: str, candidates: list[Candidate]) -> tuple[str, str]:
    subject = f"{subject_prefix} ({len(candidates)})"[:_MAX_SUBJECT_CHARS]
    lines: list[str] = []
    for candidate in candidates:
        line = f"- {candidate.title_es}: {candidate.body_es}"
        lines.append(line)
    body = "\n".join(lines)
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS].rstrip() + f"\n… y más ({len(candidates)} en total)"
    return subject, body


class DigestGrouperBase:
    """Lo común a ``email_grouping`` y ``digest_weekly`` (TASK-348): cerrar una ventana de
    ``DigestBatch`` y enviar su ``EmailMessage``, con el tope de 200 por hora y organización y la
    degradación declarada si el correo está caído. Cada tarea solo decide, por destinatario, si ya
    toca cerrar la ventana y qué candidatos entran."""

    def __init__(self, *, senders: EmailSenders, clock: Clock) -> None:
        self._senders = senders
        self._clock = clock

    async def _close_window(
        self,
        transaction: Transaction,
        *,
        recipient_user_id: uuid.UUID,
        kind: str,
        subject_prefix: str,
        window_from: datetime,
        window_to: datetime,
        candidates: list[Candidate],
    ) -> None:
        organization_id = transaction.context.organization_id
        sender = self._senders.sender
        failure_code: str | None = None
        if sender is None:
            failure_code = EMAIL_SENDER_ABSENT
        elif sender.circuit_state() == "open":
            failure_code = EMAIL_CIRCUIT_OPEN
        # Tope de 200/hora/organización (NFR-LAZ-13): se comprueba **antes** de insertar el lote
        # y reclamar las entregas (como ``adapters.ses.sender._admit``): sin presupuesto, nada se
        # escribe y el destinatario se deja tal cual para la siguiente ejecución (su ventana no
        # se cierra, así que no se pierde ni se duplica nada, BR-LAZ-125/P5). Sin esto, el lote
        # quedaba ``queued`` para siempre con las entregas ya reclamadas (``digest_batch_id``
        # puesto) y ``candidates_statement`` dejaba de encontrarlas. No aplica con el correo
        # caído: ahí no se envía nada, así que el tope no entra en juego.
        if failure_code is None and not await self._has_budget(transaction, organization_id):
            return
        notification_ids = sorted({c.notification_id for c in candidates})
        digest_batch_id = uuid7(self._clock)
        await transaction.execute(
            DIGEST_BATCH.insert().values(
                digest_batch_id=digest_batch_id,
                organization_id=organization_id,
                recipient_user_id=recipient_user_id,
                kind=kind,
                window_from=window_from,
                window_to=window_to,
                notification_ids=notification_ids,
                item_count=len(notification_ids),
                sent_at=None,
                status=_STATUS_QUEUED,
            )
        )
        claimed = (
            await transaction.execute(
                update(NOTIFICATION_DELIVERY)
                .where(
                    NOTIFICATION_DELIVERY.c.organization_id == organization_id,
                    NOTIFICATION_DELIVERY.c.notification_id.in_(notification_ids),
                    NOTIFICATION_DELIVERY.c.channel == NotificationChannel.EMAIL.value,
                    NOTIFICATION_DELIVERY.c.digest_batch_id.is_(None),
                )
                .values(digest_batch_id=digest_batch_id)
                .returning(NOTIFICATION_DELIVERY.c.notification_id)
            )
        ).all()
        claimed_ids = sorted({uuid.UUID(str(row.notification_id)) for row in claimed})
        if not claimed_ids:
            # Otra ejecución concurrente ya reclamó estas entregas primero (PR-LAZ-48): este
            # lote no tiene nada que enviar. `vigia_app` no puede borrar ni corregir
            # `notification_ids` (solo `sent_at` y `status` son columnas actualizables), así que
            # el lote queda como huella de la carrera, nunca enviado.
            await transaction.execute(
                update(DIGEST_BATCH)
                .where(DIGEST_BATCH.c.digest_batch_id == digest_batch_id)
                .values(status=_STATUS_SUPERSEDED)
            )
            return
        claimed_set = set(claimed_ids)
        sent_candidates = [c for c in candidates if c.notification_id in claimed_set]
        await self._send(
            transaction,
            digest_batch_id=digest_batch_id,
            recipient_user_id=recipient_user_id,
            subject_prefix=subject_prefix,
            candidates=sent_candidates,
            failure_code=failure_code,
        )

    async def _has_budget(self, transaction: Transaction, organization_id: uuid.UUID) -> bool:
        """Tope de 200/hora/organización (NFR-LAZ-13), bajo el mismo candado consultivo que
        ``adapters.ses.sender._admit`` (orden único: organización); lo toma el llamador antes de
        escribir nada, para que la decisión sea atómica con la escritura que la sigue."""
        await transaction.execute(_LOCK_ORGANIZATION, {"organization": str(organization_id)})
        window_start = self._clock.now() - timedelta(seconds=_RATE_WINDOW_SECONDS)
        count_statement = (
            select(func.count())
            .select_from(EMAIL_MESSAGE)
            .where(
                EMAIL_MESSAGE.c.organization_id == organization_id,
                EMAIL_MESSAGE.c.kind == DIGEST_EMAIL_KIND,
                EMAIL_MESSAGE.c.queued_at >= window_start,
            )
        )
        sent_this_hour: int = (await transaction.execute(count_statement)).scalar_one()
        return sent_this_hour < ORGANIZATION_EMAIL_CAP_PER_HOUR

    async def _send(
        self,
        transaction: Transaction,
        *,
        digest_batch_id: uuid.UUID,
        recipient_user_id: uuid.UUID,
        subject_prefix: str,
        candidates: list[Candidate],
        failure_code: str | None,
    ) -> None:
        organization_id = transaction.context.organization_id
        now = self._clock.now()
        notification_ids = [c.notification_id for c in candidates]
        if failure_code is not None:
            await transaction.execute(
                update(NOTIFICATION_DELIVERY)
                .where(
                    NOTIFICATION_DELIVERY.c.organization_id == organization_id,
                    NOTIFICATION_DELIVERY.c.digest_batch_id == digest_batch_id,
                )
                .values(
                    status=NotificationDeliveryStatus.DEGRADED_APP_ONLY.value,
                    failure_code=failure_code,
                )
            )
            await transaction.execute(
                update(DIGEST_BATCH)
                .where(DIGEST_BATCH.c.digest_batch_id == digest_batch_id)
                .values(status=_STATUS_DEGRADED)
            )
            record_email("degraded_app_only", organization_id, len(candidates))
            return
        # El presupuesto ya se comprobó en ``_close_window``, antes de insertar el lote y
        # reclamar las entregas: aquí solo queda escribir el envío.
        subject, body = compose(subject_prefix, candidates)
        await transaction.execute(
            EMAIL_MESSAGE.insert().values(
                message_id=uuid7(self._clock),
                organization_id=organization_id,
                to_user_id=recipient_user_id,
                subject_es=subject,
                body_es=body,
                kind=DIGEST_EMAIL_KIND,
                related_ids=notification_ids,
                queued_at=now,
                status=_STATUS_QUEUED,
            )
        )
        await transaction.execute(
            update(DIGEST_BATCH)
            .where(DIGEST_BATCH.c.digest_batch_id == digest_batch_id)
            .values(sent_at=now, status=_STATUS_SENT)
        )
        record_email("digested", organization_id)


@repository
class EmailGroupingTask(DigestGrouperBase):
    """``email_grouping``: agrupa el correo ``normal`` pendiente (TASK-348)."""

    def __init__(
        self,
        *,
        configuration: PostgresLoopConfigurationRepository,
        senders: EmailSenders,
        clock: Clock,
    ) -> None:
        super().__init__(senders=senders, clock=clock)
        self._configuration = configuration

    def __repr__(self) -> str:
        return "EmailGroupingTask()"

    async def run(self, transaction: Transaction) -> None:
        organization_id = transaction.context.organization_id
        rows = (
            await transaction.execute(
                candidates_statement(organization_id, NotificationUrgency.NORMAL)
            )
        ).all()
        by_recipient: dict[uuid.UUID, list[Row[Any]]] = defaultdict(list)
        for row in rows:
            by_recipient[uuid.UUID(str(row.recipient_user_id))].append(row)
        for recipient_user_id in sorted(by_recipient, key=str):
            await self._process_recipient(
                transaction, recipient_user_id, by_recipient[recipient_user_id]
            )
        await archive_read_notifications(transaction, self._clock)

    async def _process_recipient(
        self, transaction: Transaction, recipient_user_id: uuid.UUID, rows: list[Row[Any]]
    ) -> None:
        preference = await preference_for(transaction, recipient_user_id, self._clock)
        candidate_ids = [uuid.UUID(str(row.notification_id)) for row in rows]
        if not preference.email_enabled_normal or preference.email_suppressed:
            await transaction.execute(
                update(NOTIFICATION_DELIVERY)
                .where(
                    NOTIFICATION_DELIVERY.c.organization_id == transaction.context.organization_id,
                    NOTIFICATION_DELIVERY.c.notification_id.in_(candidate_ids),
                    NOTIFICATION_DELIVERY.c.channel == NotificationChannel.EMAIL.value,
                    NOTIFICATION_DELIVERY.c.digest_batch_id.is_(None),
                )
                .values(status=NotificationDeliveryStatus.SUPPRESSED_BY_PREFERENCE.value)
            )
            return
        hours = await self._grouping_hours(transaction, rows)
        earliest_pending = min(row.created_at for row in rows)
        window_from = (
            await transaction.execute(
                last_window_to(transaction, recipient_user_id, EMAIL_GROUPED_KIND)
            )
        ).scalar_one_or_none() or earliest_pending
        now = self._clock.now()
        if Schedule.every(hours * 3600).next_after(window_from) > now:
            return  # Todavía no toca cerrar la ventana de este destinatario.
        candidates = [
            Candidate(
                notification_id=uuid.UUID(str(row.notification_id)),
                created_at=row.created_at,
                plant_id=None if row.plant_id is None else uuid.UUID(str(row.plant_id)),
                title_es=row.title_es,
                body_es=row.body_es,
            )
            for row in rows
        ]
        await self._close_window(
            transaction,
            recipient_user_id=recipient_user_id,
            kind=EMAIL_GROUPED_KIND,
            subject_prefix="Resumen de notificaciones",
            window_from=window_from,
            window_to=now,
            candidates=candidates,
        )

    async def _grouping_hours(self, transaction: Transaction, rows: list[Row[Any]]) -> int:
        plant_ids = {row.plant_id for row in rows}
        default = INTEGER_PARAMETERS["email_grouping_hours"].default
        values: list[int] = []
        for plant_id in plant_ids:
            if plant_id is None:
                values.append(default)
                continue
            settings, _version = await self._configuration.settings_in(
                transaction, uuid.UUID(str(plant_id))
            )
            values.append(settings["email_grouping_hours"])
        return min(values) if values else default


def register_email_grouping(
    registry: PeriodicTaskRegistry, task: EmailGroupingTask
) -> PeriodicTask:
    """Registra ``email_grouping`` (horaria, por organización, TASK-348)."""

    async def handler(transaction: Transaction) -> None:
        await task.run(transaction)

    return registry.register(
        EMAIL_GROUPING,
        EMAIL_GROUPING_SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )
