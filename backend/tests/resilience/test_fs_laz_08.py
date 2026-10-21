"""FS-LAZ-08: evento duplicado y evento tardío sobre los seis consumidores de U-04.

Para cada consumidor (``laz_consumers.consumer_case``: manejador, registro, publicación y
despachador reales sobre PostgreSQL 16), eventos en dos particiones distintas:

1. una entrega de cada evento con las dos particiones intercaladas en orden aleatorio de la
   semilla (los de una partición llegan **tarde** respecto a los de la otra) → estado de referencia;
2. la **reentrega** de cada ``event_id`` en otro orden aleatorio, también entre particiones;
3. la ronda del despachador real, que entrega por tercera vez cada entrega aún pendiente.

El estado final (registros, notificaciones, elementos de cola, solicitudes, exportaciones y marcas)
es **igual** al de referencia, cada evento deja **un solo** efecto (``shape``) y los efectos
externos no se repiten: una sola llamada al proveedor por solicitud y una sola composición por
exportación (PR-LAZ-40, 43 y 09; NFR-LAZ-38).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import scenario
from tests.resilience.laz_consumers import CONSUMERS, ConsumerCase, consumer_case, dispatched
from tests.resilience.laz_support import PROFILES
from vigia_platform.shared.outbox.dispatcher import Outcome
from vigia_platform.shared.outbox.publish import OutboxEvent

pytestmark = pytest.mark.integration


def _interleave(random: Any, partitions: list[list[OutboxEvent]]) -> list[OutboxEvent]:
    """Las particiones entremezcladas al azar, conservando el orden dentro de cada una."""
    queues = [list(events) for events in partitions]
    order: list[OutboxEvent] = []
    while any(queues):
        queue = random.choice([q for q in queues if q])
        order.append(queue.pop(0))
    return order


def _external_effects(case: ConsumerCase, events: list[OutboxEvent]) -> dict[str, int]:
    if case.name == "assistant_worker":
        return {"provider_calls": case.extra["provider"].call_count}
    if case.name == "export_worker":
        return {"compositions": len(case.extra["composer"].jobs)}
    if case.name == "email_delivery_events":
        return {"notices_deleted": len(case.extra["sqs"].deleted)}
    return {}


@pytest.mark.parametrize("nightly", PROFILES)
@pytest.mark.parametrize("name", CONSUMERS)
def test_duplicate_and_late_events_leave_the_same_final_state(
    postgres_endpoint: PostgresEndpoint, name: str, nightly: bool
) -> None:
    per_partition = 3 if nightly else 2
    with (
        scenario(
            f"FS-LAZ-08{'abcdef'[CONSUMERS.index(name)]}",
            title=f"Evento duplicado y tardío en {name}",
            injection="Reentrega del mismo event_id y entrega fuera de orden entre particiones",
            expected="Un solo efecto por evento; estado final igual que sin repeticiones",
        ) as run,
        consumer_case(postgres_endpoint, name) as case,
    ):
        partitions = [[case.produce(p) for _ in range(per_partition)] for p in (0, 1)]
        events = [event for partition in partitions for event in partition]
        first = _interleave(run.random, partitions)
        for event in first:
            case.deliver(event)
        reference = case.effects()
        shapes = {event.event_id: case.shape(event) for event in events}
        external = _external_effects(case, events)
        if name == "assistant_worker":
            assert external["provider_calls"] == len(events)
        if name == "export_worker":
            assert external["compositions"] == len(events)

        again = list(events)
        run.random.shuffle(again)
        for event in again:
            case.deliver(event)
        reports = case.drain(events, settled={"delivered"})
        assert dispatched(reports, Outcome.DELIVERED) == len(events)
        assert dispatched(reports, Outcome.DEAD_LETTERED) == 0

        assert case.effects() == reference
        assert {event.event_id: case.shape(event) for event in events} == shapes
        assert _external_effects(case, events) == external
        if name in ("loop_projection", "review_queue"):
            # Una sola aplicación: cada evento es el último aplicado de su sujeto, una vez.
            assert all(shape == [(shape[0][0], True)] for shape in shapes.values()), shapes
        if name == "notification_engine":
            assert all(shape for shape in shapes.values())  # cada aviso, con sus entregas
        run.observe(
            consumer=name,
            events=len(events),
            deliveries_per_event=3,
            first_order=[str(event.partition_key) for event in first],
            external_effects=external,
            dispatcher_delivered=dispatched(reports, Outcome.DELIVERED),
        )
