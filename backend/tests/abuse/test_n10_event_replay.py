"""N-10 · Repetición de eventos (business-rules §14).

**Qué intenta**: que un evento reentregado (dos procesos de trabajo a la vez, una caída entre el
efecto y la confirmación, o el reproceso de la cola muerta) dispare dos veces su efecto: dos
notificaciones, dos exportaciones.

**Qué lo detiene** (BR-NUC-76, con BR-NUC-82):

- BR-NUC-76: entrega al menos una vez; la infraestructura registra ``(event_id, consumer)``
  entregado y no vuelve a invocar al manejador, y el manejador es idempotente por ``event_id``
  porque una caída entre el efecto y el registro provoca una segunda invocación;
- BR-NUC-82: el reproceso de la cola muerta reentrega con el **mismo** ``event_id`` y una vez
  entregado no se puede reprocesar otra vez.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator

import pytest

from tests.dispatch_support import (
    PLAIN,
    Behavior,
    DispatchEnvironment,
    SimulatedCrash,
    dispatch_environment,
    operator_context,
    replay_service,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "abuse_n10") as environment:
        yield environment


@pytest.fixture(autouse=True)
def _quiet(env: DispatchEnvironment) -> None:
    env.run(env.quiesce())


def test_n10_two_workers_dispatching_at_once_invoke_each_event_once(
    env: DispatchEnvironment,
) -> None:
    published = []
    for _ in range(3):
        published += env.run(env.publish(uuid.uuid4(), [uuid.uuid4(), None, uuid.uuid4()]))
    first, second = env.dispatcher(), env.dispatcher(env.new_database())

    async def both() -> None:
        await asyncio.gather(first.dispatch_once(PLAIN), second.dispatch_once(PLAIN))

    for _ in range(20):
        env.run(both())
        if all(row.status == "delivered" for row in env.run(env.deliveries(PLAIN))):
            break
    invoked = Counter(i.event_id for i in env.handlers[PLAIN].invocations)
    assert invoked == Counter({event.event_id: 1 for event in published})
    echoes = env.run(env.echoes())
    assert all(echoes[(PLAIN, event.event_id)] == 1 for event in published)


def test_n10_a_crash_between_effect_and_ack_redelivers_but_the_effect_stays_single(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    handler = env.handlers[PLAIN]
    handler.plan(event.event_id, [Behavior.CRASH_AFTER_EFFECT])
    with pytest.raises(SimulatedCrash):
        env.run(env.dispatcher().dispatch_once(PLAIN))
    for _ in range(5):
        env.run(env.dispatcher().dispatch_once(PLAIN))
    assert [i.event_id for i in handler.invocations] == [event.event_id] * 2
    assert handler.raw_effects[event.event_id] == 2  # lo vio dos veces…
    assert handler.effects[event.event_id] == 1  # …y su efecto ocurrió una
    assert env.run(env.echoes())[(PLAIN, event.event_id)] == 1
    # Entregado: más rondas no lo vuelven a invocar.
    for _ in range(3):
        env.run(env.dispatcher().dispatch_once(PLAIN))
    assert len(handler.invocations) == 2


def test_n10_the_dead_letter_is_replayed_once_with_the_same_event_id(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    handler = env.handlers[PLAIN]
    handler.plan(event.event_id, [Behavior.DEFECT] * MAX_ATTEMPTS)
    dispatcher = env.dispatcher()
    for _ in range(MAX_ATTEMPTS):
        env.run(dispatcher.dispatch_once(PLAIN))
        env.clock.advance(601)
    assert env.run(env.deliveries(PLAIN))[0].status == "dead_letter"
    replay, _ = replay_service(env)
    operator = operator_context(env)
    receipt = env.run(replay.replay(operator, event.event_id, PLAIN))
    assert receipt.event_id == event.event_id
    for _ in range(3):
        env.run(dispatcher.dispatch_once(PLAIN))
    assert handler.completed() == Counter({event.event_id: 1})
    assert handler.effects[event.event_id] == 1
    # Ya entregado: un segundo reproceso no lo encuentra y no hay otra invocación.
    with pytest.raises(ResourceNotFound):
        env.run(replay.replay(operator, event.event_id, PLAIN))
    env.run(dispatcher.dispatch_once(PLAIN))
    assert handler.completed() == Counter({event.event_id: 1})
