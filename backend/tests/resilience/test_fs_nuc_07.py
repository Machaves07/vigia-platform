"""FS-NUC-07 · Consumidor con dependencia externa caída (PR-NUC-30, PAT-NUC-RES-04; BR-NUC-79).

**Inyección**: el manejador de prueba del consumidor con dependencia externa
(``dispatch_external`` de ``tests/dispatch_support``) lanza ``ExternalDependencyDown`` durante
**3 minutos**, mientras **dos despachadores** con pools propios (dos procesos de trabajo) hacen una
ronda cada 5 s **a la vez** sobre eventos de varias organizaciones y plantas (particiones
distintas; cuántas, de la semilla). El reloj es simulado: los 3 minutos no se esperan.
PostgreSQL de verdad como ``vigia_app``.

**Resultado esperado**: **circuito abierto en el primer fallo** (``circuit_opened_at`` el instante
del fallo); **entregas pausadas en todas las particiones sin consumir intentos** (``pending`` con
``attempts = 0``); **una sonda cada 60 s** entre los dos procesos (nunca dos en el mismo
intervalo); al volver la dependencia, la sonda **cierra** el circuito y la bandeja se **drena
completa**, cada evento entregado una vez; **cero en cola muerta**.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.dispatch_support import EXTERNAL, Behavior, DispatchEnvironment, dispatch_environment
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import scenario
from vigia_platform.shared.outbox.dispatcher import Dispatcher, Outcome

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

ROUND_SECONDS: Final = 5
DOWN_MINUTES: Final = 3
PROBE_INTERVAL_SECONDS: Final = 60


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "fs_nuc_07") as environment:
        environment.run(environment.quiesce())
        yield environment


async def _round(dispatchers: list[Dispatcher]) -> list[Any]:
    """Una ronda de cada despachador, a la vez (dos procesos de trabajo)."""
    return list(await asyncio.gather(*(d.dispatch_once(EXTERNAL) for d in dispatchers)))


def test_fs_nuc_07_consumer_dependency_down_for_three_minutes(env: DispatchEnvironment) -> None:
    with scenario(
        "FS-NUC-07",
        title="Consumidor con dependencia externa caída",
        injection="manejador de prueba que lanza ExternalDependencyDown durante 3 min",
        expected=(
            "circuito abierto en el primer fallo; entregas pausadas en todas las particiones sin"
            " consumir intentos; sonda cada 60 s; cierre y drenaje completo; cero en cola muerta"
        ),
    ) as run:
        organizations = [uuid.uuid4() for _ in range(run.random.randint(2, 4))]
        events = []
        for organization in organizations:
            plants: list[uuid.UUID | None] = [
                run.random.choice([None, uuid.uuid4()]) for _ in range(run.random.randint(2, 4))
            ]
            events += env.run(env.publish(organization, plants))
        partitions = {event.partition_key for event in events}
        handler = env.handlers[EXTERNAL]
        handler.mode = Behavior.DEPENDENCY_DOWN
        dispatchers = [env.dispatcher(), env.dispatcher(env.new_database())]

        down_since = env.clock.now()
        # El primer fallo abre el circuito; el otro proceso, en el mismo instante, ya lo ve
        # abierto y no entrega nada (ninguna partición consume un intento).
        first = env.run(_round(dispatchers[:1])) + env.run(_round(dispatchers[1:]))
        opened = env.run(env.circuit(EXTERNAL))
        failures_when_opened = sum(report.count(Outcome.DEPENDENCY_DOWN) for report in first)

        probes_by_minute: dict[int, int] = {}
        while env.clock.now() < down_since + timedelta(minutes=DOWN_MINUTES):
            env.clock.advance(ROUND_SECONDS)
            for report in env.run(_round(dispatchers)):
                if report.probed:
                    elapsed = (env.clock.now() - down_since).total_seconds()
                    minute = int(elapsed // PROBE_INTERVAL_SECONDS)
                    probes_by_minute[minute] = probes_by_minute.get(minute, 0) + 1
        paused = env.run(env.deliveries(EXTERNAL))
        dead_while_down = env.run(env.dead_letters())
        invocations_while_down = len(handler.invocations)

        # La dependencia vuelve: la sonda siguiente cierra y los dos procesos drenan.
        handler.mode = Behavior.OK
        env.clock.advance(PROBE_INTERVAL_SECONDS)
        closing = env.run(_round(dispatchers))
        closed = env.run(env.circuit(EXTERNAL))
        for _ in range(100):
            reports = env.run(_round(dispatchers))
            if not any(r.progressed or r.count(Outcome.SKIPPED) for r in reports):
                break
        delivered = env.run(env.deliveries(EXTERNAL))
        dead = env.run(env.dead_letters())
        completed = handler.completed()

        run.observe(
            organizations=len(organizations),
            events=len(events),
            partitions=len(partitions),
            opened={"state": opened[0], "at_first_failure": opened[1] == down_since},
            failures_when_opened=failures_when_opened,
            invocations_while_down=invocations_while_down,
            probes_by_minute={str(k): v for k, v in sorted(probes_by_minute.items())},
            paused={
                "statuses": sorted({row.status for row in paused}),
                "max_attempts": max(row.attempts for row in paused),
            },
            dead_letters_while_down=len(dead_while_down),
            closed_by_probe=any(report.probed for report in closing),
            closed_state=closed[0],
            delivered=sorted({row.status for row in delivered}),
            delivered_events=len(completed),
            dead_letters=len(dead),
        )
        assert opened == ("open", down_since), "circuito abierto en el primer fallo"
        assert failures_when_opened == 1, "el segundo proceso ya vio el circuito abierto"
        # Una sonda por intervalo de 60 s entre los dos procesos: 3 en los 3 minutos.
        assert sorted(probes_by_minute.items()) == [(1, 1), (2, 1), (3, 1)], probes_by_minute
        assert invocations_while_down == 1 + DOWN_MINUTES
        assert {(row.status, row.attempts) for row in paused} == {("pending", 0)}
        assert {row.event_id for row in paused} == {event.event_id for event in events}
        assert dead_while_down == []
        assert any(report.probed for report in closing) and closed == ("closed", None)
        assert {row.status for row in delivered} == {"delivered"}
        assert set(completed) == {event.event_id for event in events}
        assert max(completed.values()) == 1, "cada evento entregado una vez"
        assert dead == []
