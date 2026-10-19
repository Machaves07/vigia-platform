"""``FakeEmailSender``: el ``EmailSenderPort`` de pruebas y de ``staging`` (TASK-325; LC-LAZ-33).

Valor por omisión fuera de ``pilot`` (``notifications.registration``): nunca llama a un servicio
real. Admite simular una caída de ``duration`` (``fail_for``, con el mismo reloj inyectado que el
resto de la prueba, nunca la pared) para los escenarios de resiliencia (FS-LAZ-02): mientras dura,
``send`` declara la dependencia caída y el cortacircuito se abre, con el mismo ``ConsumerBreaker``
que ``SesEmailSender`` (``EmailCircuitBreaker``). Lleva su propia cuenta de reenvíos exacta
(PAT-LAZ-ESC-02), **en memoria**, para que las pruebas de los límites no necesiten PostgreSQL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.notifications.adapters.ses.circuit import EmailCircuitBreaker
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext

__all__ = ["FakeEmailSender", "SentEmail"]

RESEND_WINDOW: Final = timedelta(hours=1)
RESEND_LIMIT_PER_USER: Final = 3
RESEND_LIMIT_PER_ORGANIZATION: Final = 50


@dataclass(frozen=True, slots=True)
class SentEmail:
    """Un envío aceptado, para que la prueba lo lea sin tocar la base."""

    organization_id: uuid.UUID
    to_user_id: uuid.UUID
    to_email: str
    subject_es: str
    kind: str
    queued_at: datetime


class FakeEmailSender:
    """Doble de prueba de ``EmailSenderPort``: nunca sale a la red."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._circuit = EmailCircuitBreaker(clock)
        self._unavailable_until: datetime | None = None
        self._sent: list[SentEmail] = []

    def __repr__(self) -> str:
        return "FakeEmailSender()"

    @property
    def sent(self) -> tuple[SentEmail, ...]:
        """Los envíos aceptados, en orden."""
        return tuple(self._sent)

    def fail_for(self, duration: timedelta) -> None:
        """Simula el servicio caído durante ``duration`` desde ahora (FS-LAZ-02)."""
        self._unavailable_until = self._clock.now() + duration

    def recover(self) -> None:
        """Quita la caída simulada antes de que expire por sí sola."""
        self._unavailable_until = None

    def circuit_state(self) -> Literal["closed", "open", "half_open"]:
        return self._circuit.circuit_state()

    def _down(self) -> bool:
        return self._unavailable_until is not None and self._clock.now() < self._unavailable_until

    def _resend_count(
        self, organization_id: uuid.UUID, window_start: datetime, *, to_user_id: uuid.UUID | None
    ) -> int:
        return sum(
            1
            for sent in self._sent
            if sent.organization_id == organization_id
            and sent.queued_at >= window_start
            and (to_user_id is None or sent.to_user_id == to_user_id)
        )

    async def send(
        self, context: ScopeContext, message: InvitationEmail
    ) -> Literal["queued", "unavailable"]:
        now = self._clock.now()
        window_start = now - RESEND_WINDOW
        organization_count = self._resend_count(
            message.organization_id, window_start, to_user_id=None
        )
        if organization_count >= RESEND_LIMIT_PER_ORGANIZATION:
            return "unavailable"
        user_count = self._resend_count(
            message.organization_id, window_start, to_user_id=message.to_user_id
        )
        if user_count >= RESEND_LIMIT_PER_USER:
            return "unavailable"
        if not await self._circuit.guard():
            return "unavailable"
        if self._down():
            await self._circuit.on_dependency_failure()
            return "unavailable"
        await self._circuit.on_success()
        self._sent.append(
            SentEmail(
                organization_id=message.organization_id,
                to_user_id=message.to_user_id,
                to_email=message.to_email,
                subject_es=message.subject_es,
                kind=message.kind,
                queued_at=now,
            )
        )
        return "queued"
