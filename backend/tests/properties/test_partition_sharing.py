"""PR-NUC-46: reparto de particiones entre N despachadores concurrentes (TASK-129; PBT-06).

"Con N despachadores concurrentes sobre eventos y particiones generados, ninguna partición es
procesada por dos despachadores a la vez, el orden de entrega por partición es el de publicación
y el conjunto entregado más la cola muerta iguala al publicado al drenar" (PAT-NUC-ESC-05).

Contra PostgreSQL 16 real, como ``vigia_app``. Cada despachador tiene **su propio pool** de
conexiones, como un proceso de trabajo distinto, y todos corren a la vez en el mismo bucle: el
manejador cede el control varias veces a mitad de la entrega, así que las rondas se intercalan de
verdad mientras cada transacción tiene su bloqueo consultivo. Los comandos:

- ``publish``: de uno a cuatro eventos en una transacción, en dos organizaciones y cuatro
  particiones;
- ``script``: defectos para un evento (de uno a nueve: los de ocho o más acaban en la cola
  muerta y la partición continúa);
- ``concurrent_round(n)``: una ronda de ``n`` despachadores a la vez (de 2 a 4);
- ``advance``: el reloj, para que venzan los reintentos.

**Después de cada comando**: ninguna partición tuvo dos entregas en curso a la vez (el
manejador lo anota al entrar y al salir); por partición, la secuencia de invocaciones nunca
retrocede en el orden de publicación (un evento solo se invoca cuando todos los anteriores de su
partición están resueltos; los reintentos repiten el mismo); el manejador corre con la
organización del evento (PR-NUC-33). **Al drenar**: lo entregado más la cola muerta es
exactamente lo publicado, cada entrega completa una sola vez y su efecto en la base existe una
vez.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import _seeds_for_profile
from tests.dispatch_support import (
    PLAIN,
    Behavior,
    DispatchEnvironment,
    dispatch_environment,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS

pytestmark = pytest.mark.integration

STEPS_PER_EXAMPLE = 12
MAX_DISPATCHERS = 4
_OPEN = ("pending", "retrying")


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "partition_sharing") as env:
        PartitionSharingMachine.env = env
        PartitionSharingMachine.dispatchers = [
            env.dispatcher(env.new_database()) for _ in range(MAX_DISPATCHERS)
        ]
        yield env


class PartitionSharingMachine(RuleBasedStateMachine):
    env: DispatchEnvironment
    dispatchers: list[Dispatcher]

    def __init__(self) -> None:
        super().__init__()
        self.organizations = (uuid.uuid4(), uuid.uuid4())
        self.plants = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        self.order: dict[uuid.UUID, int] = {}
        self.partition: dict[uuid.UUID, str] = {}

    @initialize()
    def start(self) -> None:
        self.env.run(self.env.quiesce())
        self.env.handlers[PLAIN].interleave = 3

    @property
    def handler(self):  # type: ignore[no-untyped-def]
        return self.env.handlers[PLAIN]

    @rule(
        organization=st.integers(0, 1),
        partitions=st.lists(st.integers(0, 3), min_size=1, max_size=4),
    )
    def publish(self, organization: int, partitions: list[int]) -> None:
        plants = [self.plants[p] if p < 3 else None for p in partitions]
        for event in self.env.run(self.env.publish(self.organizations[organization], plants)):
            self.order[event.event_id] = len(self.order)
            self.partition[event.event_id] = event.partition_key

    @precondition(lambda self: bool(self.order))
    @rule(index=st.integers(0), defects=st.integers(1, MAX_ATTEMPTS + 1))
    def script(self, index: int, defects: int) -> None:
        events = sorted(self.order, key=self.order.__getitem__)
        self.handler.plan(events[index % len(events)], [Behavior.DEFECT] * defects)

    @rule(seconds=st.sampled_from([0, 1, 16, 600, 601]))
    def advance(self, seconds: int) -> None:
        self.env.clock.advance(seconds)

    @rule(count=st.integers(2, MAX_DISPATCHERS))
    def concurrent_round(self, count: int) -> None:
        async def together() -> None:
            await asyncio.gather(
                *(dispatcher.dispatch_once(PLAIN) for dispatcher in self.dispatchers[:count])
            )

        self.env.run(together())

    # --- invariantes ---------------------------------------------------------------------------

    @invariant()
    def no_partition_is_processed_by_two_dispatchers_at_once(self) -> None:
        assert self.handler.overlaps == []

    @invariant()
    def delivery_order_per_partition_is_publication_order(self) -> None:
        last: dict[str, int] = {}
        for invocation in self.handler.invocations:
            order = self.order[invocation.event_id]
            assert order >= last.get(invocation.partition_key, -1)
            last[invocation.partition_key] = order
            assert invocation.context_organization_id == invocation.event_organization_id

    def teardown(self) -> None:
        for _ in range(len(self.order) * (MAX_ATTEMPTS + 2) + 10):
            rows = self.env.run(self.env.deliveries(PLAIN))
            if all(row.status not in _OPEN for row in rows):
                break
            self.env.clock.advance(601)
            self.concurrent_round(MAX_DISPATCHERS)
        self.no_partition_is_processed_by_two_dispatchers_at_once()
        self.delivery_order_per_partition_is_publication_order()
        rows = self.env.run(self.env.deliveries(PLAIN))
        assert {row.event_id for row in rows} == set(self.order)
        assert all(row.status in ("delivered", "dead_letter") for row in rows)
        letters = {letter["event_id"] for letter in self.env.run(self.env.dead_letters())}
        delivered = {row.event_id for row in rows if row.status == "delivered"}
        assert letters == {row.event_id for row in rows if row.status == "dead_letter"}
        assert delivered | letters == set(self.order) and not delivered & letters
        completed = self.handler.completed()
        echoes = self.env.run(self.env.echoes())
        for event_id in self.order:
            expected = 1 if event_id in delivered else 0
            assert completed[event_id] == expected
            assert echoes[(PLAIN, event_id)] == expected


def test_pr_nuc_46_partitions_are_shared_without_overlap_and_in_order(
    environment: DispatchEnvironment,
) -> None:
    """PR-NUC-46 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(PartitionSharingMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
