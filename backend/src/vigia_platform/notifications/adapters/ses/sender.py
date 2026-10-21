"""``SesEmailSender``: ``EmailSenderPort`` sobre Amazon SES (TASK-325; LC-LAZ-33; PAT-LAZ-RES-04,
PAT-LAZ-ESC-02; BR-LAZ-133).

Implementa el puerto que ``identity.application.invitations`` ya define (``send``,
``circuit_state``) para las invitaciones de usuario de U-02 (``InvitationEmail`` con
``kind = platform_invitation``): si el puerto no está registrado, U-02 degrada a mostrar el
enlace una sola vez (BR-NUC-32); esta tarea no cambia esa ruta.

**Admisión** (``_admit``), en la transacción que la concede (PAT-LAZ-ESC-02): bajo dos candados
consultivos de la transacción, **organización primero y usuario después** (orden único,
documentado aquí y en el PR), cuenta los ``EmailMessage`` del mismo ``kind`` (``platform_
invitation``) de la última hora sobre ``EmailMessage.queued_at`` y rechaza con ``"unavailable"``
si el límite exacto de 3 por hora y usuario o 50 por hora y organización ya se alcanzó (reenvíos y
reintentos incluidos; el tope es solo de este tipo de correo, no del total de correo de la
organización o el usuario, rework de revisión); si admite, escribe el ``EmailMessage`` con
``status = "queued"`` en la misma transacción.

**Envío**, fuera de la transacción de admisión (para no mantener una conexión abierta durante la
llamada de red): cliente ``sesv2`` de ``loop.http.client_factory`` (LC-LAZ-39), en un hilo
(LC-NUC-30), con 10 s de presupuesto total. Con éxito, marca ``sent`` y guarda
``provider_message_id`` (dispara ``claim_email_provider_message_key``, la clave de idempotencia
que ``notifications.adapters.ses.delivery_events`` usa); con fallo, marca ``failed``.

**Cortacircuito** (PAT-LAZ-RES-04): ``ConsumerBreaker`` de U-02 tal cual, con su estado en
memoria del proceso (``_EmailCircuit``): este adaptador no es un consumidor de la bandeja de
salida, así que no hay fila de ``shared.consumer`` que compartir entre procesos; cada proceso
abre y cierra su propio circuito. Con el circuito abierto, ``send`` responde ``"unavailable"`` de
inmediato y sin llamada externa.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Protocol

from botocore.config import Config  # type: ignore[import-untyped]
from sqlalchemy import Select, func, insert, select, text, update

from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.loop.http.client_factory import HttpClientFactory
from vigia_platform.notifications.adapters.postgres.tables import EMAIL_MESSAGE
from vigia_platform.notifications.adapters.ses.circuit import EmailCircuitBreaker
from vigia_platform.notifications.metrics import record_email
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "CODE_PATH",
    "RESEND_LIMIT_PER_ORGANIZATION",
    "RESEND_LIMIT_PER_USER",
    "RESEND_WINDOW",
    "SEND_TIMEOUT_SECONDS",
    "EmailDatabase",
    "SesEmailSender",
]

_log = get_logger("notifications.adapters.ses")

CODE_PATH: Final = "vigia_platform.notifications.adapters.ses"
SEND_TIMEOUT_SECONDS: Final = 10
RESEND_WINDOW: Final = timedelta(hours=1)
RESEND_LIMIT_PER_USER: Final = 3
RESEND_LIMIT_PER_ORGANIZATION: Final = 50
_STATUS_QUEUED: Final = "queued"
_STATUS_SENT: Final = "sent"
_STATUS_FAILED: Final = "failed"
_FAILURE_SEND_FAILED: Final = "email_send_failed"
_FAILURE_RATE_LIMITED_USER: Final = "email_resend_limit_user"
_FAILURE_RATE_LIMITED_ORGANIZATION: Final = "email_resend_limit_organization"


class EmailDatabase(Protocol):
    """Lo que el adaptador usa de ``shared.db.Database``."""

    def transaction(self, context: ScopeContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class _Admitted:
    message_id: uuid.UUID
    queued_at: datetime


_LOCK_ORGANIZATION: Final = text("SELECT pg_advisory_xact_lock(hashtext(:organization)::bigint)")
_LOCK_USER: Final = text(
    "SELECT pg_advisory_xact_lock(hashtext(:organization)::int, hashtext(:recipient)::int)"
)
"""Orden de candados: organización (una clave) y después usuario (dos claves, espacio distinto
del de una clave): nunca al revés, también en cualquier otro envío que tome ambos."""


def _count_since(
    organization_id: uuid.UUID,
    window_start: datetime,
    *,
    kind: str,
    to_user_id: uuid.UUID | None,
) -> Select[tuple[int]]:
    """Cuenta solo ``EmailMessage`` del mismo ``kind`` que lo que se admite: el límite de
    PAT-LAZ-ESC-02 es de reenvíos y reintentos de **ese** correo, no del total de correo de la
    organización o el usuario (un aviso ``notification``/``digest`` del motor no debe consumir el
    presupuesto de reenvío de una invitación, ni al revés; rework de revisión)."""
    statement = (
        select(func.count())
        .select_from(EMAIL_MESSAGE)
        .where(
            EMAIL_MESSAGE.c.organization_id == organization_id,
            EMAIL_MESSAGE.c.queued_at >= window_start,
            EMAIL_MESSAGE.c.kind == kind,
        )
    )
    if to_user_id is not None:
        statement = statement.where(EMAIL_MESSAGE.c.to_user_id == to_user_id)
    return statement


@repository
class SesEmailSender:
    """``EmailSenderPort`` sobre Amazon SES (LC-LAZ-33)."""

    def __init__(
        self,
        *,
        database: EmailDatabase,
        http_clients: HttpClientFactory,
        region_name: str,
        sender_email: str,
        configuration_set_name: str,
        clock: Clock,
    ) -> None:
        self._database = database
        self._clock = clock
        self._circuit = EmailCircuitBreaker(clock)
        self._sender_email = sender_email
        self._configuration_set_name = configuration_set_name
        self._client = http_clients.email_client(
            CODE_PATH,
            region_name=region_name,
            config=Config(connect_timeout=3, read_timeout=8, retries={"max_attempts": 1}),
        )

    def __repr__(self) -> str:
        return "SesEmailSender()"

    def circuit_state(self) -> Literal["closed", "open", "half_open"]:
        return self._circuit.circuit_state()

    async def send(
        self, context: ScopeContext, message: InvitationEmail
    ) -> Literal["queued", "unavailable"]:
        """Admite (con sus límites exactos), envía por SES y marca el resultado.

        El circuito se comprueba **después** de admitir (BR-LAZ-133 no dice qué pesa más entre un
        límite agotado y el circuito abierto; aquí el límite, que protege reputación, se evalúa
        siempre, y solo entonces se intenta la red si el circuito lo permite).
        """
        admitted = await self._admit(context, message)
        if admitted is None:
            return "unavailable"
        if not await self._circuit.guard():
            return "unavailable"
        try:
            provider_message_id = await asyncio.wait_for(
                asyncio.to_thread(self._send_via_ses, message), timeout=SEND_TIMEOUT_SECONDS
            )
        except Exception:
            _log.warning("el envío por SES falló", code=_FAILURE_SEND_FAILED)
            await self._circuit.on_dependency_failure()
            await self._mark_failed(context, admitted)
            return "unavailable"
        await self._circuit.on_success()
        await self._mark_sent(context, admitted, provider_message_id)
        return "queued"

    async def _admit(self, context: ScopeContext, message: InvitationEmail) -> _Admitted | None:
        now = self._clock.now()
        window_start = now - RESEND_WINDOW
        organization_id = message.organization_id
        to_user_id = message.to_user_id
        async with self._database.transaction(context) as transaction:
            await transaction.execute(_LOCK_ORGANIZATION, {"organization": str(organization_id)})
            await transaction.execute(
                _LOCK_USER, {"organization": str(organization_id), "recipient": str(to_user_id)}
            )
            organization_statement = _count_since(
                organization_id, window_start, kind=message.kind, to_user_id=None
            )
            organization_count = (await transaction.execute(organization_statement)).scalar_one()
            if organization_count >= RESEND_LIMIT_PER_ORGANIZATION:
                _log.warning(
                    "límite de reenvíos por organización alcanzado",
                    code=_FAILURE_RATE_LIMITED_ORGANIZATION,
                )
                return None
            user_count = (
                await transaction.execute(
                    _count_since(
                        organization_id, window_start, kind=message.kind, to_user_id=to_user_id
                    )
                )
            ).scalar_one()
            if user_count >= RESEND_LIMIT_PER_USER:
                _log.warning(
                    "límite de reenvíos por usuario alcanzado", code=_FAILURE_RATE_LIMITED_USER
                )
                return None
            message_id = uuid7(self._clock)
            await transaction.execute(
                insert(EMAIL_MESSAGE).values(
                    message_id=message_id,
                    organization_id=organization_id,
                    to_user_id=to_user_id,
                    subject_es=message.subject_es,
                    body_es=message.body_es,
                    kind=message.kind,
                    related_ids=list(message.related_ids),
                    queued_at=now,
                    status=_STATUS_QUEUED,
                )
            )
        return _Admitted(message_id, now)

    def _send_via_ses(self, message: InvitationEmail) -> str:
        response = self._client.send_email(
            FromEmailAddress=self._sender_email,
            Destination={"ToAddresses": [message.to_email]},
            Content={
                "Simple": {
                    "Subject": {"Data": message.subject_es, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": message.body_es, "Charset": "UTF-8"}},
                }
            },
            ConfigurationSetName=self._configuration_set_name,
        )
        return str(response["MessageId"])

    async def _mark_sent(
        self, context: ScopeContext, admitted: _Admitted, provider_message_id: str
    ) -> None:
        now = self._clock.now()
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                update(EMAIL_MESSAGE)
                .where(
                    EMAIL_MESSAGE.c.message_id == admitted.message_id,
                    EMAIL_MESSAGE.c.queued_at == admitted.queued_at,
                )
                .values(sent_at=now, status=_STATUS_SENT, provider_message_id=provider_message_id)
            )
        record_email("sent", context.organization_id)

    async def _mark_failed(self, context: ScopeContext, admitted: _Admitted) -> None:
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                update(EMAIL_MESSAGE)
                .where(
                    EMAIL_MESSAGE.c.message_id == admitted.message_id,
                    EMAIL_MESSAGE.c.queued_at == admitted.queued_at,
                )
                .values(status=_STATUS_FAILED, failure_code=_FAILURE_SEND_FAILED)
            )
