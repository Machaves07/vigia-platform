"""Tarea periódica ``digest_weekly`` (TASK-348; LC-LAZ-35; PAT-LAZ-RES-05; BR-LAZ-125, 126, 131;
PR-LAZ-42, 48).

Diaria, por organización: para cada destinatario con avisos ``digest`` pendientes de agrupar
(mismas condiciones que ``notifications.grouping``, pero sobre ``Notification.urgency = digest``),
si hoy es ``weekly_digest_weekday`` de su preferencia **o** ya pasó una semana completa desde el
cierre de su último resumen (la sonda diaria alcanza el día que tocaba después de una caída: no
hace falta que caiga exactamente en el día configurado para no perder el resumen, BR-LAZ-131), un
**solo** ``DigestBatch`` (``kind = weekly_manager``) con todo lo acumulado y **un solo**
``EmailMessage``. El resumen del gerente va como lote, no como un aviso por hallazgo (BR-LAZ-131):
esta tarea nunca escribe un ``EmailMessage`` por aviso individual.

Reutiliza de ``notifications.grouping`` (``DigestGrouperBase``) el cierre de ventana, la garantía
de «una sola vez» (reclamar las entregas con ``digest_batch_id IS NULL``), el tope de 200 correos
por hora y organización y la degradación declarada con el correo caído: solo cambian la urgencia
que agrupa, el ``kind`` del lote y cómo decide si ya toca.

**Pendiente conocido** (fuera de alcance de TASK-348, documentado en el PR): el catálogo
(``notifications.catalog``, TASK-324) no da a esta tarea ninguna fila de ``finding_escalated`` con
nivel ``manager_weekly`` para el gerente, porque sus destinatarios son solo el coordinador SST
(``RecipientRule(client=(COORDINATOR,))``, urgencia ``immediate``); el comentario de esa fila
señala que «el nivel ``manager_weekly`` entra además en el digest del gerente», pero escribirlo
exige que el catálogo o el motor (TASK-324, fuera del alcance de esta tarea) resuelvan también al
gerente como destinatario. Esta tarea agrupa fielmente todo lo que **sí** llega con
``urgency = digest`` (los eventos de administración de U-02 y U-03 que ya la tienen); no inventa
una fuente de datos que el catálogo no produce.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Final

from sqlalchemy import update
from sqlalchemy.engine import Row

from vigia_platform.loop.domain.enums import (
    NotificationChannel,
    NotificationDeliveryStatus,
    NotificationUrgency,
)
from vigia_platform.notifications.adapters.postgres.tables import NOTIFICATION_DELIVERY
from vigia_platform.notifications.delivery import EmailSenders
from vigia_platform.notifications.grouping import (
    Candidate,
    DigestGrouperBase,
    candidates_statement,
    last_window_to,
)
from vigia_platform.notifications.preferences import preference_for
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

__all__ = [
    "DIGEST_WEEKLY",
    "DIGEST_WEEKLY_SCHEDULE",
    "WEEKLY_MANAGER_KIND",
    "DigestWeeklyTask",
    "register_digest_weekly",
]

DIGEST_WEEKLY: Final = "digest_weekly"
DIGEST_WEEKLY_SCHEDULE: Final = Schedule.daily(hour=2)
"""Sondea una vez al día; solo agrupa cuando ya tocó la semana del destinatario (LC §6.9)."""

WEEKLY_MANAGER_KIND: Final = "weekly_manager"


@repository
class DigestWeeklyTask(DigestGrouperBase):
    """``digest_weekly``: el resumen semanal del gerente (TASK-348, BR-LAZ-131)."""

    def __init__(self, *, senders: EmailSenders, clock: Clock) -> None:
        super().__init__(senders=senders, clock=clock)

    def __repr__(self) -> str:
        return "DigestWeeklyTask()"

    async def run(self, transaction: Transaction) -> None:
        organization_id = transaction.context.organization_id
        rows = (
            await transaction.execute(
                candidates_statement(organization_id, NotificationUrgency.DIGEST)
            )
        ).all()
        by_recipient: dict[uuid.UUID, list[Row[Any]]] = defaultdict(list)
        for row in rows:
            by_recipient[uuid.UUID(str(row.recipient_user_id))].append(row)
        for recipient_user_id in sorted(by_recipient, key=str):
            await self._process_recipient(
                transaction, recipient_user_id, by_recipient[recipient_user_id]
            )

    async def _process_recipient(
        self, transaction: Transaction, recipient_user_id: uuid.UUID, rows: list[Row[Any]]
    ) -> None:
        preference = await preference_for(transaction, recipient_user_id, self._clock)
        candidate_ids = [uuid.UUID(str(row.notification_id)) for row in rows]
        if not preference.email_enabled_digest or preference.email_suppressed:
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
        earliest_pending = min(row.created_at for row in rows)
        window_from = (
            await transaction.execute(
                last_window_to(transaction, recipient_user_id, WEEKLY_MANAGER_KIND)
            )
        ).scalar_one_or_none() or earliest_pending
        now = self._clock.now()
        weekday = Schedule.weekly(weekday=preference.weekly_digest_weekday - 1)
        if weekday.next_after(window_from) > now:
            return  # Todavía no toca el resumen semanal de este destinatario.
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
            kind=WEEKLY_MANAGER_KIND,
            subject_prefix="Resumen semanal",
            window_from=window_from,
            window_to=now,
            candidates=candidates,
        )


def register_digest_weekly(registry: PeriodicTaskRegistry, task: DigestWeeklyTask) -> PeriodicTask:
    """Registra ``digest_weekly`` (diaria, por organización, TASK-348)."""

    async def handler(transaction: Transaction) -> None:
        await task.run(transaction)

    return registry.register(
        DIGEST_WEEKLY,
        DIGEST_WEEKLY_SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )
