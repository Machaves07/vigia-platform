"""FS-LAZ-12: 200 rebotes duros y quejas en la cola gestionada, con el tope de 200 correos por hora.

Sobre PostgreSQL 16 real (``notification_world``): el consumidor real ``email_delivery_events``
sondea una cola SQS en memoria (``FakeSqsClient``, sin red) con 100 rebotes duros y 100 quejas en
orden aleatorio de la semilla; después, las tareas reales ``email_grouping`` y ``digest_weekly`` y
el motor real con un aviso ``immediate``:

- cada rebote duro marca a su usuario (``bounce_kind = hard``, ``failed``, supresión) y queda
  declarado; cada queja silencia ``normal`` y ``digest`` (``suppressed_by_preference``) y **nunca**
  ``immediate``; la reentrega de los 200 avisos no cambia nada;
- la tasa de rebotes de 24 h por encima del 5 % alarma solo a la **segunda** evaluación
  consecutiva (``EMAIL_BOUNCE_RATE_PERCENT`` y ``evaluation_periods = 2`` de
  ``infra/stacks/loop.py``). En producción la evalúa CloudWatch con las métricas del conjunto de
  configuración de SES; ningún código de la aplicación emite ``email_messages_total``, así que la
  tasa se calcula aquí con los mismos hechos (``email_message`` enviados y rebotados);
- el tope de 200 ``EmailMessage`` de resumen por hora y organización frena el reenvío: con 250
  destinatarios limpios pendientes salen 200, una segunda ejecución en la misma hora no envía
  nada y el resto sale en la hora siguiente.

Solo datos generados (correos ``@example.test``); un solo reloj simulado.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import (
    Member,
    NotificationWorld,
    Senders,
    StubSender,
    notification_world,
)
from tests.properties.laz.test_pr_laz_58_bounces_complaints import (
    FakeSqsClient,
    _bounce_body,
    _complaint_body,
    _consumer,
    _seed_preference,
    _seed_sent_email,
)
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES, consecutive_breaches
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.notifications.digest import DigestWeeklyTask, register_digest_weekly
from vigia_platform.notifications.grouping import (
    ORGANIZATION_EMAIL_CAP_PER_HOUR,
    EmailGroupingTask,
    register_email_grouping,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry

pytestmark = pytest.mark.integration

NOTICES: Final = 200
CAP: Final = 200
BOUNCE_ALARM_RATIO: Final = 0.05
"""``EMAIL_BOUNCE_RATE_PERCENT`` (5 %) de ``infra/stacks/loop.py``, mayor que."""
EVALUATION_PERIODS: Final = 2
DAY: Final = timedelta(days=1)


def _bounce_rate(world: NotificationWorld, organization_id: uuid.UUID) -> float:
    """Rebotes duros entre los correos enviados en las últimas 24 h (``Bounce / Send``)."""
    since = world.clock.now() - DAY
    (row,) = world.fetch(
        "SELECT count(*) AS sent, count(*) FILTER (WHERE bounce_kind = 'hard') AS bounced"
        " FROM notifications.email_message"
        " WHERE organization_id = $1 AND sent_at > $2",
        organization_id,
        since,
    )
    return int(row["bounced"]) / int(row["sent"]) if row["sent"] else 0.0


def _poll(world: NotificationWorld, sqs: FakeSqsClient) -> None:
    consumer = _consumer(world, sqs)
    pulse = world.event(world.provider_id, "email_delivery_events_poll", {})
    world.run(consumer(pulse, None))  # type: ignore[arg-type]


def _state(world: NotificationWorld, organization_id: uuid.UUID) -> list[tuple[Any, ...]]:
    emails = world.fetch(
        "SELECT to_user_id, status, bounce_kind, complaint_at IS NOT NULL AS complained"
        " FROM notifications.email_message WHERE organization_id = $1"
        " AND kind = 'platform_invitation' ORDER BY to_user_id",
        organization_id,
    )
    preferences = world.fetch(
        "SELECT user_id, email_suppressed_hard_bounce, email_suppressed_complaint,"
        " email_suppressed_at FROM notifications.user_notification_preference"
        " WHERE organization_id = $1 ORDER BY user_id",
        organization_id,
    )
    return [tuple(row.values()) for row in emails] + [tuple(row.values()) for row in preferences]


def _delivery_status(
    world: NotificationWorld, organization_id: uuid.UUID, urgency: str
) -> dict[uuid.UUID, tuple[str, bool]]:
    rows = world.fetch(
        "SELECT n.recipient_user_id, d.status, d.digest_batch_id IS NOT NULL AS batched"
        " FROM notifications.notification_delivery AS d"
        " JOIN notifications.notification AS n ON n.notification_id = d.notification_id"
        " AND n.created_at = d.notification_created_at"
        " WHERE d.organization_id = $1 AND d.channel = 'email' AND n.urgency = $2",
        organization_id,
        urgency,
    )
    return {row["recipient_user_id"]: (row["status"], row["batched"]) for row in rows}


def _digest_emails(world: NotificationWorld, organization_id: uuid.UUID) -> int:
    (row,) = world.fetch(
        "SELECT count(*) AS n FROM notifications.email_message"
        " WHERE organization_id = $1 AND kind = 'digest'",
        organization_id,
    )
    return int(row["n"])


def _run_task(
    world: NotificationWorld, organization_id: uuid.UUID, register: Any, task: Any
) -> None:
    registration = register(PeriodicTaskRegistry(), task)
    context = world.contexts.context_for_organization(registration, organization_id)

    async def run() -> None:
        async with world.database.transaction(context) as transaction:
            await registration.handler(transaction)

    world.run(run())


@pytest.mark.parametrize("nightly", PROFILES)
def test_burst_of_bounces_and_complaints_under_the_hourly_cap(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    assert ORGANIZATION_EMAIL_CAP_PER_HOUR == CAP
    with (
        scenario(
            "FS-LAZ-12",
            title="Ráfaga de rebotes y quejas",
            injection="100 rebotes duros y 100 quejas en la cola gestionada; tope 200/h activo",
            expected="Cada rebote declarado; quejas silencian normal y digest, nunca immediate; "
            "alarma del 5 % a la segunda evaluación; el tope frena el reenvío",
        ) as run,
        notification_world(postgres_endpoint, "fs_laz_12") as world,
    ):
        organization, plant, zone = world.new_organization()
        sqs = FakeSqsClient()
        bounced: list[tuple[Member, str]] = []
        complained: list[tuple[Member, str]] = []
        for index in range(NOTICES):
            member = world.add_member(
                organization, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization
            )
            provider_message_id = f"ses-{uuid.uuid4().hex}"
            world.run(_seed_preference(world, organization, member.user_id))
            world.run(_seed_sent_email(world, organization, member.user_id, provider_message_id))
            (bounced if index % 2 == 0 else complained).append((member, provider_message_id))
        bodies = [_bounce_body(pid, "Permanent") for _, pid in bounced]
        bodies += [_complaint_body(pid) for _, pid in complained]
        run.random.shuffle(bodies)

        # Tres evaluaciones de la tasa: antes, a mitad y al final de la ráfaga.
        ratios = [_bounce_rate(world, organization)]
        for half in (bodies[: NOTICES // 2], bodies[NOTICES // 2 :]):
            for body in half:
                sqs.enqueue(body)
            _poll(world, sqs)
            world.clock.advance(300)
            ratios.append(_bounce_rate(world, organization))
        assert sqs.queue == [] and len(sqs.deleted) == NOTICES
        alarm_at = consecutive_breaches(ratios, BOUNCE_ALARM_RATIO, EVALUATION_PERIODS)
        assert ratios[0] == 0.0 and ratios[1] > BOUNCE_ALARM_RATIO
        assert alarm_at == 2, ratios  # la primera evaluación por encima del 5 % no alarma
        # Una sola evaluación por encima, seguida de una por debajo, nunca alarma.
        assert consecutive_breaches([ratios[1], 0.0], BOUNCE_ALARM_RATIO, EVALUATION_PERIODS) == -1

        preferences = {
            row["user_id"]: row
            for row in world.fetch(
                "SELECT * FROM notifications.user_notification_preference"
                " WHERE organization_id = $1",
                organization,
            )
        }
        emails = {
            row["to_user_id"]: row
            for row in world.fetch(
                "SELECT * FROM notifications.email_message WHERE organization_id = $1",
                organization,
            )
        }
        for member, _ in bounced:
            email, preference = emails[member.user_id], preferences[member.user_id]
            assert (email["status"], email["bounce_kind"]) == ("failed", "hard")
            assert preference["email_suppressed_hard_bounce"] is True
            assert preference["email_suppressed_at"] is not None
        for member, _ in complained:
            email, preference = emails[member.user_id], preferences[member.user_id]
            assert email["complaint_at"] is not None and email["status"] == "sent"
            assert preference["email_suppressed_complaint"] is True
            assert preference["email_suppressed_hard_bounce"] is False
        # Reentregar los 200 avisos (fuera de orden) no cambia ninguna marca.
        before = _state(world, organization)
        for body in reversed(bodies):
            sqs.enqueue(body)
        _poll(world, sqs)
        assert _state(world, organization) == before

        # El reenvío: entregas normal pendientes para los 200 suprimidos y 250 limpios.
        clean = [
            world.add_member(
                organization, Role.COORDINATOR_SST, ScopeLevel.ORGANIZATION, organization
            )
            for _ in range(CAP + (100 if nightly else 50))
        ]
        suppressed = [member for member, _ in bounced + complained]
        for member in suppressed + clean:
            world.seed_delivery(
                organization, member.user_id, urgency="normal", plant_id=plant, zone_id=zone
            )
        world.clock.advance(5 * 3600)  # más que ``email_grouping_hours`` (4 h por omisión)
        grouping = EmailGroupingTask(
            configuration=PostgresLoopConfigurationRepository(world.database),
            senders=Senders(StubSender()),
            clock=world.clock,
        )
        _run_task(world, organization, register_email_grouping, grouping)
        normal = _delivery_status(world, organization, "normal")
        assert all(normal[m.user_id] == ("suppressed_by_preference", False) for m in suppressed)
        assert _digest_emails(world, organization) == CAP
        waiting = [m for m in clean if not normal[m.user_id][1]]
        assert len(waiting) == len(clean) - CAP and all(
            normal[m.user_id] == ("pending", False) for m in waiting
        )
        # Una segunda ejecución en la misma hora no envía nada: el tope frena el bucle.
        world.clock.advance(60)
        _run_task(world, organization, register_email_grouping, grouping)
        assert _digest_emails(world, organization) == CAP
        # Pasada la hora, sale el resto (nada se pierde) y nunca a un suprimido.
        world.clock.advance(3600)
        _run_task(world, organization, register_email_grouping, grouping)
        assert _digest_emails(world, organization) == len(clean)
        sent_to = {
            row["to_user_id"]
            for row in world.fetch(
                "SELECT to_user_id FROM notifications.email_message"
                " WHERE organization_id = $1 AND kind = 'digest'",
                organization,
            )
        }
        assert sent_to == {m.user_id for m in clean}

        # ``digest`` también queda silenciado para quien rebotó o se quejó.
        for member in suppressed:
            world.seed_delivery(
                organization, member.user_id, urgency="digest", plant_id=plant, zone_id=zone
            )
        world.clock.advance(8 * 86400)
        _run_task(
            world,
            organization,
            register_digest_weekly,
            DigestWeeklyTask(senders=Senders(StubSender()), clock=world.clock),
        )
        digest = _delivery_status(world, organization, "digest")
        assert all(digest[m.user_id] == ("suppressed_by_preference", False) for m in suppressed)

        # ``immediate`` nunca se silencia: la alerta de integridad llega a quien se quejó.
        integrity = world.event(
            organization,
            "integrity_compromised",
            {
                "chain_kind": "ledger",
                "first_failed_sequence": 1,
                "verification_mode": "full",
                "detected_at": world.clock.now().isoformat(),
            },
        )
        world.deliver(world.engine(senders=Senders(StubSender())), integrity)
        immediate_to = {row["to_user_id"] for row in world.emails(integrity.event_id)}
        assert {m.user_id for m, _ in complained} <= immediate_to
        run.observe(
            notices=NOTICES,
            hard_bounces=len(bounced),
            complaints=len(complained),
            bounce_ratios=ratios,
            bounce_alarm_evaluation=alarm_at,
            clean_recipients=len(clean),
            digest_emails_first_hour=CAP,
            digest_emails_total=len(clean),
            suppressed_normal=len(suppressed),
            suppressed_digest=len(suppressed),
            immediate_to_complained=len({m.user_id for m, _ in complained} & immediate_to),
        )
