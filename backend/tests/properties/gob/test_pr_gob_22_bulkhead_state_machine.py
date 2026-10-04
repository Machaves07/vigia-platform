"""PR-GOB-22: el mamparo por clase de ruta frente a un modelo (TASK-205; LC-GOB-20; PBT-06).

Para cualquier secuencia generada de llegadas y salidas de peticiones de las dos clases y de
avances del reloj, sobre ``Bulkheads`` real (semáforos de ``asyncio`` en un bucle propio y el
temporizador de la espera sobre un ``SimulatedClock``, nunca la pared):

- las peticiones ``node`` en curso **nunca superan su cupo**;
- **siempre queda al menos el 30 %** de los puestos del trabajador para ``person`` (los que la
  clase ``node`` no puede ocupar);
- **ninguna petición de persona se rechaza mientras haya cupo de persona libre**: solo se rechaza
  la que lleva 2 s esperando con su semáforo lleno durante toda la espera;
- **todo rechazo de nodo es ``temporarily_unavailable`` con ``retry_after_seconds`` > 0** (nunca
  ``rate_limited``), y es inmediato.

El modelo es independiente del código: cuenta los puestos de cada clase, una cola de personas en
espera por orden de llegada con su plazo, y el reloj. Tamaños: los del diseño (35 y 15) y otros
pequeños que respetan la reserva, para que la saturación aparezca en pocas llegadas. Los avances
del reloj incluyen los bordes de la espera (justo antes de 2 s, 2 s exactos).

Semilla registrada: con el perfil ``ci`` corre con la semilla fija del proyecto y con la de la
sesión (que se imprime al final de pytest para reproducir).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

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

from tests.bulkhead_support import ManualTimer, bulkheads_with_reader, gauge
from tests.conftest import _seeds_for_profile
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.bulkheads import (
    PERSON_RESERVE_PERCENT,
    PERSON_WAIT_SECONDS,
    Bulkheads,
    BulkheadSaturated,
)
from vigia_platform.shared.db import RouteClass
from vigia_platform.shared.observability.metrics import MetricName

NODE, PERSON = RouteClass.NODE, RouteClass.PERSON

SIZES = st.sampled_from([(35, 15), (7, 3), (2, 1), (1, 1), (3, 2), (4, 4)])
"""``(node, person)`` que respetan la reserva del 30 %."""
TICKS = st.one_of(
    st.sampled_from([0.0, 0.5, 1.0, PERSON_WAIT_SECONDS - 0.001, PERSON_WAIT_SECONDS]),
    st.floats(0, 3, allow_nan=False, allow_infinity=False),
)


@dataclass
class Request:
    """Una petición en curso, en espera o terminada (lo que ve el manejador)."""

    route_class: RouteClass
    gate: asyncio.Event
    state: str = "pending"
    """``pending`` (esperando puesto), ``admitted``, ``done`` o ``rejected``."""
    error: BulkheadSaturated | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class Model:
    node: int
    person: int
    in_node: int = 0
    in_person: int = 0
    waiting: deque[tuple[int, float]] = field(default_factory=deque)
    """Personas en espera: ``(índice, plazo)`` por orden de llegada."""


class BulkheadMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.bulkheads: Bulkheads | None = None
        self.timer = ManualTimer()
        self.reader: Any = None
        self.model = Model(0, 0)
        self.requests: list[Request] = []

    # --- Utilidades -----------------------------------------------------------------------------

    def _settle(self) -> None:
        async def spin() -> None:
            for _ in range(20):
                await asyncio.sleep(0)

        self.loop.run_until_complete(spin())

    async def _run(self, request: Request) -> None:
        assert self.bulkheads is not None
        try:
            async with self.bulkheads.slot(request.route_class):
                request.state = "admitted"
                await request.gate.wait()
            request.state = "done"
        except BulkheadSaturated as error:
            request.state = "rejected"
            request.error = error

    def _admitted(self, route_class: RouteClass) -> list[int]:
        return [
            index
            for index, request in enumerate(self.requests)
            if request.route_class is route_class and request.state == "admitted"
        ]

    # --- Reglas ---------------------------------------------------------------------------------

    @initialize(sizes=SIZES)
    def start(self, sizes: tuple[int, int]) -> None:
        node, person = sizes
        self.bulkheads, self.timer, self.reader = bulkheads_with_reader(
            node=node, person=person, timer=self.timer
        )
        self.model = Model(node, person)

    @rule(route_class=st.sampled_from(RouteClass))
    def arrive(self, route_class: RouteClass) -> None:
        request = Request(route_class, asyncio.Event())
        index = len(self.requests)
        self.requests.append(request)
        request.task = self.loop.create_task(self._run(request))
        self._settle()
        model = self.model
        if route_class is NODE:
            if model.in_node < model.node:
                model.in_node += 1
                assert request.state == "admitted"
            else:
                # Rechazo inmediato, sin avanzar el reloj ni esperar.
                assert request.state == "rejected"
                assert request.error is not None
                api_error = translate(request.error)
                assert api_error.code is ApiErrorCode.TEMPORARILY_UNAVAILABLE
                assert api_error.retry_after_seconds is not None
                assert api_error.retry_after_seconds > 0
        elif model.in_person < model.person and not model.waiting:
            model.in_person += 1
            assert request.state == "admitted"
        else:
            deadline = self.timer.clock.monotonic() + PERSON_WAIT_SECONDS
            model.waiting.append((index, deadline))
            assert request.state == "pending"

    @precondition(lambda self: self.model.in_node > 0 or self.model.in_person > 0)
    @rule(data=st.data())
    def depart(self, data: st.DataObject) -> None:
        candidates = self._admitted(NODE) + self._admitted(PERSON)
        index = data.draw(st.sampled_from(candidates))
        request = self.requests[index]
        request.gate.set()
        self._settle()
        assert request.state == "done"
        model = self.model
        if request.route_class is NODE:
            model.in_node -= 1
        else:
            model.in_person -= 1
            if model.waiting:
                # El puesto pasa a la primera persona en espera.
                first, _ = model.waiting.popleft()
                model.in_person += 1
                assert self.requests[first].state == "admitted"

    @rule(seconds=TICKS)
    def tick(self, seconds: float) -> None:
        self.timer.advance(seconds)
        self._settle()
        model = self.model
        now = self.timer.clock.monotonic()
        while model.waiting and model.waiting[0][1] <= now:
            index, _ = model.waiting.popleft()
            request = self.requests[index]
            # Solo se rechaza con el semáforo de personas lleno.
            assert model.in_person == model.person
            assert request.state == "rejected"
            assert request.error is not None
            assert translate(request.error).code is ApiErrorCode.TEMPORARILY_UNAVAILABLE

    # --- Invariantes ----------------------------------------------------------------------------

    @invariant()
    def node_never_exceeds_its_quota(self) -> None:
        if self.bulkheads is None:
            return
        assert self.bulkheads.in_use(NODE) == self.model.in_node <= self.model.node

    @invariant()
    def at_least_30_percent_stays_for_persons(self) -> None:
        if self.bulkheads is None:
            return
        total = self.model.node + self.model.person
        free_for_persons = total - self.bulkheads.in_use(NODE)
        assert free_for_persons * 100 >= PERSON_RESERVE_PERCENT * total

    @invariant()
    def persons_match_the_model_and_none_waits_with_a_free_slot(self) -> None:
        if self.bulkheads is None:
            return
        assert self.bulkheads.in_use(PERSON) == self.model.in_person <= self.model.person
        if self.model.waiting:
            assert self.model.in_person == self.model.person
        pending = [i for i, r in enumerate(self.requests) if r.state == "pending"]
        assert pending == [index for index, _ in self.model.waiting]

    @invariant()
    def the_published_occupancy_is_the_real_one(self) -> None:
        if self.bulkheads is None:
            return
        for route_class in RouteClass:
            value = gauge(self.reader, MetricName.BULKHEAD_IN_USE, route_class)
            assert value == self.bulkheads.in_use(route_class)

    def teardown(self) -> None:
        for request in self.requests:
            request.gate.set()
        self._settle()
        for request in self.requests:
            if request.task is not None and not request.task.done():
                request.task.cancel()
        self._settle()
        self.loop.close()


def test_pr_gob_22_bulkhead_matches_the_model() -> None:
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(BulkheadMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=60))
