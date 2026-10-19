"""``FakeEmailSender`` (TASK-325; LC-LAZ-33; PAT-LAZ-RES-04; FS-LAZ-02, FS-LAZ-12).

Sin PostgreSQL ni red: solo el reloj inyectado y el estado en memoria.

- **FS-LAZ-02**: caído 5 min (``fail_for``), el circuito abre, todo responde ``"unavailable"`` de
  inmediato y, cumplido el intervalo de sonda (60 s), se recupera solo.
- **PAT-LAZ-ESC-02**: el límite exacto de 3 reenvíos por hora y usuario y 50 por hora y
  organización (FS-LAZ-12: una ráfaga por encima del tope la frena, sin ninguna llamada real).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from vigia_contracts.clock import SimulatedClock

from tests.factories import make_context
from vigia_platform.identity.application.invitations import InvitationEmail
from vigia_platform.notifications.adapters.ses.fake import (
    RESEND_LIMIT_PER_ORGANIZATION,
    RESEND_LIMIT_PER_USER,
    FakeEmailSender,
)
from vigia_platform.shared.context import ScopeContext

START = datetime(2026, 1, 1, tzinfo=UTC)
_CONTEXT: ScopeContext = make_context()
"""``send`` nunca inspecciona el contenido del contexto (no toca la base), pero la guarda
``@repository`` (BR-NUC-02) exige uno válido antes de llegar al cuerpo."""


def _message(organization_id: uuid.UUID, to_user_id: uuid.UUID) -> InvitationEmail:
    return InvitationEmail(
        organization_id=organization_id,
        to_user_id=to_user_id,
        to_email="persona@example.test",
        subject_es="Invitación a Vigía",
        body_es="Cuerpo sintético",
        related_ids=(uuid.uuid4(),),
    )


def test_fs_laz_02_a_five_minute_outage_opens_the_circuit_and_recovers() -> None:
    clock = SimulatedClock(START)
    sender = FakeEmailSender(clock=clock)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()

    async def send_once() -> str:
        return await sender.send(_CONTEXT, _message(organization_id, user_id))

    assert asyncio.run(send_once()) == "queued"
    assert sender.circuit_state() == "closed"

    sender.fail_for(timedelta(minutes=5))
    assert asyncio.run(send_once()) == "unavailable"
    assert sender.circuit_state() == "open"
    # Mientras el circuito está abierto, cada intento siguiente también falla de inmediato.
    assert asyncio.run(send_once()) == "unavailable"
    assert sender.circuit_state() == "open"

    clock.advance(4 * 60)  # 4 min: la caída sigue activa, el intervalo de sonda (60 s) ya pasó.
    # La sonda reclamada cae todavía dentro de la ventana de caída: vuelve a abrir.
    assert asyncio.run(send_once()) == "unavailable"
    assert sender.circuit_state() == "open"

    clock.advance(2 * 60)  # Total 6 min: la caída simulada ya terminó (5 min).
    assert asyncio.run(send_once()) == "queued"
    assert sender.circuit_state() == "closed"


def test_the_organization_resend_limit_is_exact() -> None:
    clock = SimulatedClock(START)
    sender = FakeEmailSender(clock=clock)
    organization_id = uuid.uuid4()

    async def send_for(user_id: uuid.UUID) -> str:
        return await sender.send(_CONTEXT, _message(organization_id, user_id))

    accepted = sum(1 for _ in range(55) if asyncio.run(send_for(uuid.uuid4())) == "queued")
    assert accepted == RESEND_LIMIT_PER_ORGANIZATION


def test_the_user_resend_limit_is_exact() -> None:
    clock = SimulatedClock(START)
    sender = FakeEmailSender(clock=clock)
    organization_id, user_id = uuid.uuid4(), uuid.uuid4()

    async def send_once() -> str:
        return await sender.send(_CONTEXT, _message(organization_id, user_id))

    results = [asyncio.run(send_once()) for _ in range(RESEND_LIMIT_PER_USER + 2)]
    assert results == ["queued"] * RESEND_LIMIT_PER_USER + ["unavailable", "unavailable"]

    clock.advance(3601)  # Pasada la hora (límite inclusivo), la ventana libera cupo.
    assert asyncio.run(send_once()) == "queued"
