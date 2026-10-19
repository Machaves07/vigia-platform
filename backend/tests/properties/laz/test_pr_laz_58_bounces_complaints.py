"""PR-LAZ-58: el procesamiento de rebotes y quejas de ``email_delivery_events`` es conmutativo e
idempotente (TASK-325; LC-LAZ-34; BR-LAZ-126, 127, 130; FS-LAZ-12).

Contra PostgreSQL 16 real como ``vigia_app`` (``NotificationWorld``, reutilizado de TASK-324),
con un cliente de SQS doble (``FakeSqsClient``) y ``EmailDeliveryEventsConsumer`` real:

- Un rebote **duro** marca ``email_message.status = failed`` y ``bounce_kind = hard``, y suprime
  ``email_suppressed_hard_bounce`` del destinatario; uno **blando** solo deja constancia
  (``bounce_kind = soft``), sin suprimir nada ni tocar ``status``.
- Una **queja** marca ``complaint_at`` y suprime ``email_suppressed_complaint``; **nunca**
  suprime el nivel ``immediate`` (la entidad no tiene ese campo, BR-LAZ-126) y nunca toca la otra
  marca de supresión.
- **Conmutativo**: procesar primero el rebote duro y después la queja del mismo correo, o al
  revés, deja el mismo estado final.
- **Idempotente**: reentregar el mismo aviso (mismo ``provider_message_id``) una o varias veces
  no cambia el estado tras la primera vez, y cada mensaje recibido se borra de la cola.
- Un mensaje cuyo ``provider_message_id`` no está en ``email_provider_message_key`` (ajeno o
  reentrega de un envío que esta plataforma no hizo) no se borra, para que la cola lo reintente.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert

from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.notification_support import NotificationWorld, notification_world
from vigia_platform.notifications.adapters.postgres.tables import (
    EMAIL_MESSAGE,
    USER_NOTIFICATION_PREFERENCE,
)
from vigia_platform.notifications.adapters.ses.delivery_events import EmailDeliveryEventsConsumer
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel

pytestmark = pytest.mark.integration

EXAMPLES: Final = 25


@dataclass
class FakeSqsClient:
    """Una cola de SQS en memoria: ``receive_message`` devuelve y consume por lotes."""

    queue: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def enqueue(self, body: str) -> None:
        self.queue.append({"Body": body, "ReceiptHandle": str(uuid.uuid4())})

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        limit = int(kwargs.get("MaxNumberOfMessages", 10))
        batch, self.queue = self.queue[:limit], self.queue[limit:]
        return {"Messages": batch}

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        self.deleted.append(str(kwargs["ReceiptHandle"]))
        return {}


def _bounce_body(provider_message_id: str, bounce_type: str) -> str:
    inner = {
        "notificationType": "Bounce",
        "bounce": {"bounceType": bounce_type, "bouncedRecipients": []},
        "mail": {"messageId": provider_message_id},
    }
    return json.dumps({"Type": "Notification", "Message": json.dumps(inner)})


def _complaint_body(provider_message_id: str) -> str:
    inner = {
        "notificationType": "Complaint",
        "complaint": {"complainedRecipients": []},
        "mail": {"messageId": provider_message_id},
    }
    return json.dumps({"Type": "Notification", "Message": json.dumps(inner)})


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NotificationWorld]:
    with notification_world(postgres_endpoint, "pr_laz_58") as built:
        yield built


def _consumer(world: NotificationWorld, sqs: FakeSqsClient) -> EmailDeliveryEventsConsumer:
    return EmailDeliveryEventsConsumer(
        sqs=sqs,
        queue_url="https://sqs.us-east-1.amazonaws.com/000000000000/test",
        database=world.database,
        provider_context=world.contexts.provider_audit_context(),
        contexts=world.contexts,
        clock=world.clock,
    )


@dataclass(frozen=True, slots=True)
class _Organization:
    """El «evento» con el que se pide el contexto del sistema de la organización sembrada."""

    organization_id: uuid.UUID
    correlation_id: uuid.UUID


def _org_context(world: NotificationWorld, organization_id: uuid.UUID) -> Any:
    return world.contexts.context_from_event(
        _Organization(organization_id, uuid7()), unit=ActorUnit.U04
    )


async def _seed_sent_email(
    world: NotificationWorld,
    organization_id: uuid.UUID,
    to_user_id: uuid.UUID,
    provider_message_id: str,
) -> tuple[uuid.UUID, datetime]:
    """Un ``EmailMessage`` ya ``sent`` (lo que ``SesEmailSender`` habría dejado) con su clave de
    idempotencia ya reclamada por el disparador de la base
    (``claim_email_provider_message_key``). Se escribe como ``vigia_app``, con el mismo contexto
    de sistema que usa el motor real: la fila de la clave de idempotencia lleva su propia
    seguridad a nivel de fila forzada."""
    message_id = uuid7()
    now = world.clock.now()
    async with world.database.transaction(_org_context(world, organization_id)) as transaction:
        await transaction.execute(
            insert(EMAIL_MESSAGE).values(
                message_id=message_id,
                organization_id=organization_id,
                to_user_id=to_user_id,
                subject_es="Invitación a Vigía",
                body_es="Cuerpo",
                kind="platform_invitation",
                related_ids=[],
                queued_at=now,
                sent_at=now,
                status="sent",
                provider_message_id=provider_message_id,
            )
        )
    return message_id, now


async def _seed_preference(
    world: NotificationWorld, organization_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    async with world.database.transaction(_org_context(world, organization_id)) as transaction:
        await transaction.execute(
            insert(USER_NOTIFICATION_PREFERENCE).values(
                user_id=user_id,
                organization_id=organization_id,
                email_enabled_normal=True,
                email_enabled_digest=True,
                weekly_digest_weekday=1,
                updated_at=world.clock.now(),
                email_suppressed_hard_bounce=False,
                email_suppressed_complaint=False,
                email_suppressed_at=None,
            )
        )


def _email_row(world: NotificationWorld, message_id: uuid.UUID) -> dict[str, Any]:
    rows = world.fetch(
        "SELECT * FROM notifications.email_message WHERE message_id = $1", message_id
    )
    assert len(rows) == 1
    return rows[0]


def _preference_row(
    world: NotificationWorld, organization_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    rows = world.fetch(
        "SELECT * FROM notifications.user_notification_preference"
        " WHERE organization_id = $1 AND user_id = $2",
        organization_id,
        user_id,
    )
    assert len(rows) == 1
    return rows[0]


def _setup(world: NotificationWorld) -> tuple[uuid.UUID, uuid.UUID, str]:
    organization_id, _plant_id, _zone_id = world.new_organization()
    member = world.add_member(
        organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
    )
    provider_message_id = f"ses-{uuid.uuid4().hex}"
    world.run(_seed_preference(world, organization_id, member.user_id))
    world.run(_seed_sent_email(world, organization_id, member.user_id, provider_message_id))
    return organization_id, member.user_id, provider_message_id


def _run_once(world: NotificationWorld, sqs: FakeSqsClient) -> None:
    consumer = _consumer(world, sqs)
    dummy_event = world.event(world.provider_id, "email_delivery_events_poll", {})
    world.run(consumer(dummy_event, None))  # type: ignore[arg-type]


@given(bounce_type=st.sampled_from(["Permanent", "Transient", "Undetermined"]))
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_a_bounce_marks_the_message_and_suppresses_only_hard_bounces(
    world: NotificationWorld, bounce_type: str
) -> None:
    organization_id, user_id, provider_message_id = _setup(world)
    sqs = FakeSqsClient()
    sqs.enqueue(_bounce_body(provider_message_id, bounce_type))
    _run_once(world, sqs)

    email = _email_row(world, _message_id_of(world, provider_message_id))
    preference = _preference_row(world, organization_id, user_id)
    is_hard = bounce_type == "Permanent"
    assert email["bounce_kind"] == ("hard" if is_hard else "soft")
    assert email["status"] == ("failed" if is_hard else "sent")
    assert preference["email_suppressed_hard_bounce"] is is_hard
    assert preference["email_suppressed_complaint"] is False
    assert (preference["email_suppressed_at"] is not None) is is_hard
    assert sqs.queue == [] and len(sqs.deleted) == 1


def _message_id_of(world: NotificationWorld, provider_message_id: str) -> uuid.UUID:
    rows = world.fetch(
        "SELECT message_id FROM notifications.email_provider_message_key"
        " WHERE provider_message_id = $1",
        provider_message_id,
    )
    assert len(rows) == 1
    return rows[0]["message_id"]


def test_a_complaint_suppresses_normal_and_digest_never_immediate(world: NotificationWorld) -> None:
    organization_id, user_id, provider_message_id = _setup(world)
    sqs = FakeSqsClient()
    sqs.enqueue(_complaint_body(provider_message_id))
    _run_once(world, sqs)

    email = _email_row(world, _message_id_of(world, provider_message_id))
    preference = _preference_row(world, organization_id, user_id)
    assert email["complaint_at"] is not None
    assert email["status"] == "sent"
    assert preference["email_suppressed_complaint"] is True
    assert preference["email_suppressed_hard_bounce"] is False
    # BR-LAZ-126: ninguna columna silencia ``immediate``; la imposibilidad es estructural.
    assert "email_suppressed_immediate" not in preference
    assert set(preference) == {
        "user_id",
        "organization_id",
        "email_enabled_normal",
        "email_enabled_digest",
        "weekly_digest_weekday",
        "updated_at",
        "email_suppressed_hard_bounce",
        "email_suppressed_complaint",
        "email_suppressed_at",
    }
    assert sqs.queue == [] and len(sqs.deleted) == 1


@given(order=st.sampled_from(["bounce_first", "complaint_first"]))
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_bounce_and_complaint_are_commutative(world: NotificationWorld, order: str) -> None:
    """El mismo correo rebota (duro) y recibe una queja: el orden no cambia el resultado final."""
    organization_id, user_id, provider_message_id = _setup(world)
    first_body = _bounce_body(provider_message_id, "Permanent")
    second_body = _complaint_body(provider_message_id)
    if order == "complaint_first":
        first_body, second_body = second_body, first_body

    sqs = FakeSqsClient()
    sqs.enqueue(first_body)
    _run_once(world, sqs)
    sqs.enqueue(second_body)
    _run_once(world, sqs)

    email = _email_row(world, _message_id_of(world, provider_message_id))
    preference = _preference_row(world, organization_id, user_id)
    assert email["bounce_kind"] == "hard"
    assert email["status"] == "failed"
    assert email["complaint_at"] is not None
    assert preference["email_suppressed_hard_bounce"] is True
    assert preference["email_suppressed_complaint"] is True


@pytest.mark.parametrize("repetitions", [1, 2, 3])
def test_redelivery_of_the_same_bounce_is_idempotent(
    world: NotificationWorld, repetitions: int
) -> None:
    organization_id, user_id, provider_message_id = _setup(world)
    sqs = FakeSqsClient()
    for _ in range(repetitions):
        sqs.enqueue(_bounce_body(provider_message_id, "Permanent"))
        _run_once(world, sqs)

    email = _email_row(world, _message_id_of(world, provider_message_id))
    preference = _preference_row(world, organization_id, user_id)
    assert email["bounce_kind"] == "hard" and email["status"] == "failed"
    assert preference["email_suppressed_hard_bounce"] is True
    suppressed_at = preference["email_suppressed_at"]
    assert suppressed_at is not None
    # Reentregar no mueve la marca de supresión (``COALESCE``, nunca sobrescribe).
    sqs.enqueue(_bounce_body(provider_message_id, "Permanent"))
    _run_once(world, sqs)
    assert _preference_row(world, organization_id, user_id)["email_suppressed_at"] == suppressed_at
    assert len(sqs.deleted) == repetitions + 1


def test_an_unknown_provider_message_id_is_left_for_the_dead_letter_queue(
    world: NotificationWorld,
) -> None:
    sqs = FakeSqsClient()
    sqs.enqueue(_bounce_body(f"unknown-{uuid.uuid4().hex}", "Permanent"))
    _run_once(world, sqs)
    assert sqs.deleted == []  # No se borra: la cola lo reintenta y, agotado el tope, a su DLQ.


def test_a_malformed_message_is_left_for_the_dead_letter_queue(world: NotificationWorld) -> None:
    sqs = FakeSqsClient()
    sqs.enqueue("no es JSON en absoluto")
    _run_once(world, sqs)
    assert sqs.deleted == []


def test_fs_laz_12_a_burst_of_200_bounces_is_all_declared_without_any_real_call(
    world: NotificationWorld,
) -> None:
    """Una ráfaga de 200 rebotes duros de la misma organización: cada uno se declara (marca a su
    usuario y se borra de la cola) y ninguna llamada real sale del proceso (``FakeSqsClient`` no
    tiene red); el tope de 200 por hora y organización de ``notifications.grouping`` (LC-LAZ-32,
    fuera de alcance de esta tarea) protege los envíos *salientes*, no esta ingestión, que el
    consumidor nunca convierte en correo saliente (no crea notificaciones)."""
    organization_id, _plant_id, _zone_id = world.new_organization()
    sqs = FakeSqsClient()
    provider_message_ids = []
    for _ in range(200):
        member = world.add_member(
            organization_id, Role.ADMINISTRATOR, ScopeLevel.ORGANIZATION, organization_id
        )
        provider_message_id = f"ses-{uuid.uuid4().hex}"
        world.run(_seed_preference(world, organization_id, member.user_id))
        world.run(_seed_sent_email(world, organization_id, member.user_id, provider_message_id))
        provider_message_ids.append(provider_message_id)
        sqs.enqueue(_bounce_body(provider_message_id, "Permanent"))

    _run_once(world, sqs)

    assert sqs.queue == []
    assert len(sqs.deleted) == 200
    for provider_message_id in provider_message_ids:
        email = _email_row(world, _message_id_of(world, provider_message_id))
        assert email["bounce_kind"] == "hard" and email["status"] == "failed"
