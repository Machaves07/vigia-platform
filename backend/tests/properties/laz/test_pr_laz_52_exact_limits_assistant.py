"""PR-LAZ-52, parte del asistente (TASK-316; PAT-LAZ-ESC-01 y 02; NFR-LAZ-25 y 35): los límites
exactos de **20 por hora y usuario** y **200 por día y organización**, contados sobre
``AssistantRequest.requested_at`` en la transacción de admisión, y el tope mensual de fichas.

Contra PostgreSQL 16 real como ``vigia_app``, con ``AssistantService`` sobre **tres pools** (tres
procesos simulados) y las solicitudes de cada tanda lanzadas **a la vez** (``asyncio.gather``):

- **Propiedad**: para cualquier secuencia generada de tandas concurrentes de tres usuarios,
  separadas por saltos del reloj inyectado (con los bordes de la ventana: 0, 1 s, 59 min 59 s,
  1 h y 1 h 1 s), **nunca** se admiten más de 20 en ninguna hora deslizante de un usuario, y en
  cada tanda se admiten **exactamente** las que caben; cada rechazo es ``AssistantRateLimited``.
- **Concurrencia dirigida**: 30 solicitudes simultáneas de un usuario admiten 20; con 195
  solicitudes de la organización en las últimas 24 h, 12 simultáneas de cuatro usuarios admiten
  5. Tres rondas cada una. La mutación «comprobar y luego escribir» (sin el candado consultivo de
  la organización) cae en rojo aquí (tabla de garantías del PR).
- **Bordes** (secuenciales): la solicitud que sale de la ventana a 1 h (o 24 h) exactas deja
  sitio; 1 ms antes, no, y la espera que se anuncia es la que falta.
- **Tope de fichas**: una ficha por debajo del tope mensual se admite; en el tope, la admisión
  responde ``AssistantUnavailable(budget_exceeded)`` **sin fila, sin evento y sin construir el
  contexto** (la prueba del consumidor está en ``tests/integration/test_loop_assistant_flow.py``).
- **El contador y la huella son la misma fila**: las admitidas son exactamente las filas de
  ``loop.assistant_request`` (y sus eventos); no hay tabla contadora.

Solo datos generados. El reloj es el simulado.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.loop_assistant_support import AssistantStack, assistant_stack
from vigia_platform.loop.assistant import service as service_module
from vigia_platform.loop.assistant.budget import (
    MONTHLY_OUTPUT_TOKENS,
    ORGANIZATION_WINDOW,
    REQUESTS_PER_ORGANIZATION_PER_DAY,
    REQUESTS_PER_USER_PER_HOUR,
    USER_WINDOW,
    AssistantRateLimited,
)
from vigia_platform.loop.assistant.service import AssistantService, AssistantUnavailable
from vigia_platform.loop.domain.enums import AssistantUnavailableCause
from vigia_platform.shared.context import ScopeContext

pytestmark = pytest.mark.integration

PROCESSES: Final = 3
USERS: Final = 3
ROUNDS: Final = 3
FRESH_DAY: Final = (ORGANIZATION_WINDOW + timedelta(hours=1)).total_seconds()
"""Salto entre ejemplos: la ventana de la organización queda vacía."""

gaps = st.one_of(st.sampled_from([0, 1, 3_599, 3_600, 3_601]), st.integers(0, 4_000))
batches = st.lists(
    st.tuples(gaps, st.lists(st.integers(0, USERS - 1), min_size=1, max_size=24)),
    min_size=1,
    max_size=4,
)


@dataclass
class World:
    stack: AssistantStack
    processes: list[AssistantService]


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with assistant_stack(postgres_endpoint, "pr_laz_52_assistant") as stack:
        base = stack.base.world.stack
        databases = [stack.database() for _ in range(PROCESSES)]
        processes = [
            AssistantService(
                database=database,
                authorizer=base.authz.authorizer,
                outbox=base.outbox,
                clock=stack.clock,
            )
            for database in databases
        ]
        yield World(stack, processes)
        for database in databases:
            stack.run(database.dispose())


@dataclass
class Scenario:
    organization_id: uuid.UUID
    analysis_id: uuid.UUID
    contexts: list[ScopeContext]


def _scenario(world: World, users: int, which: str = "a") -> Scenario:
    """Un día nuevo (la ventana de la organización vacía), un análisis y ``users`` personas."""
    stack = world.stack
    stack.clock.advance(FRESH_DAY)
    stack.clock.set(to_ms(stack.now()))  # en milisegundos, como ``requested_at``
    people = [stack.coordinator(which) for _ in range(users)]
    analysis = uuid.UUID(stack.analysis(people[0], which))
    contexts = [stack.base.context(person) for person in people]
    return Scenario(stack.organization(which).organization_id, analysis, contexts)


async def _batch(world: World, scenario: Scenario, users: Sequence[int]) -> list[Any]:
    """Las solicitudes de la tanda, a la vez, repartidas entre los tres procesos."""
    return await asyncio.gather(
        *(
            world.processes[index % PROCESSES].request(
                scenario.contexts[user], scenario.analysis_id
            )
            for index, user in enumerate(users)
        ),
        return_exceptions=True,
    )


def _rows(world: World, scenario: Scenario) -> list[dict[str, Any]]:
    """Las solicitudes del escenario (las de su análisis: cada escenario tiene el suyo)."""
    return [
        row
        for row in world.stack.requests_of(scenario.organization_id)
        if row["analysis_id"] == scenario.analysis_id
    ]


def _windows_hold(stamps: Sequence[datetime], window: timedelta, limit: int) -> bool:
    """Ninguna ventana deslizante ``(t - window, t]`` tiene más de ``limit``."""
    ordered = sorted(stamps)
    return all(
        sum(1 for other in ordered if moment - window < other <= moment) <= limit
        for moment in ordered
    )


def _split(results: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    unexpected = [
        r
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, AssistantRateLimited)
    ]
    assert unexpected == [], unexpected
    admitted = [r for r in results if not isinstance(r, BaseException)]
    limited = [r for r in results if isinstance(r, AssistantRateLimited)]
    return admitted, limited


def _seed(world: World, scenario: Scenario, count: int, at: datetime) -> None:
    """``count`` solicitudes ya admitidas de la organización, desde ``at`` (1 ms entre cada una),
    de una persona que no pide en la prueba."""
    seeder = world.stack.base.context(world.stack.coordinator())
    world.stack.execute(
        "INSERT INTO loop.assistant_request (request_id, organization_id, plant_id, zone_id,"
        " analysis_id, user_id, requested_by, requested_at, state, outbox_event_id,"
        " attempt_count, context_digest)"
        " SELECT gen_random_uuid(), organization_id, plant_id, zone_id, analysis_id, $2,"
        " '{}'::jsonb, $3::timestamptz + (n * interval '1 millisecond'), 'pending',"
        " gen_random_uuid(), 0,"
        " repeat('0', 64) FROM loop.root_cause_analysis, generate_series(0, $4 - 1) AS n"
        " WHERE analysis_id = $1",
        scenario.analysis_id,
        seeder.actor.id,
        at,
        count,
    )


@settings(max_examples=8, deadline=None)
@given(plan=batches)
def test_pr_laz_52_the_user_limit_holds_under_concurrency(world: World, plan: Any) -> None:
    stack = world.stack
    scenario = _scenario(world, USERS)
    users_of = {context.actor.id: index for index, context in enumerate(scenario.contexts)}
    admitted_total = 0
    for gap, users in plan:
        stack.clock.advance(gap)
        now = stack.now()
        rows = _rows(world, scenario)
        per_user = Counter(
            users_of[row["user_id"]] for row in rows if row["requested_at"] > now - USER_WINDOW
        )
        asked = Counter(users)
        expected = sum(
            min(count, REQUESTS_PER_USER_PER_HOUR - per_user[user]) for user, count in asked.items()
        )
        admitted, _limited = _split(stack.run(_batch(world, scenario, users)))
        assert len(admitted) == expected, (gap, users, len(admitted), expected)
        admitted_total += len(admitted)
        rows = _rows(world, scenario)
        assert len(rows) == admitted_total  # el contador y la huella son la misma fila
        assert {row["request_id"] for row in rows} >= {view.request_id for view in admitted}
        for user in range(USERS):
            stamps = [row["requested_at"] for row in rows if users_of[row["user_id"]] == user]
            assert _windows_hold(stamps, USER_WINDOW, REQUESTS_PER_USER_PER_HOUR), user


def test_thirty_simultaneous_requests_of_one_user_admit_exactly_twenty(world: World) -> None:
    for _ in range(ROUNDS):
        scenario = _scenario(world, 1)
        admitted, limited = _split(world.stack.run(_batch(world, scenario, [0] * 30)))
        assert (len(admitted), len(limited)) == (20, 10)
        assert len(_rows(world, scenario)) == 20


def test_the_organization_limit_of_200_a_day_holds_under_concurrency(world: World) -> None:
    for _ in range(ROUNDS):
        scenario = _scenario(world, 4)
        start = world.stack.now() - timedelta(hours=23)
        _seed(world, scenario, REQUESTS_PER_ORGANIZATION_PER_DAY - 5, start)
        users = [user for user in range(4) for _ in range(3)]
        admitted, limited = _split(world.stack.run(_batch(world, scenario, users)))
        assert (len(admitted), len(limited)) == (5, 7)
        rows = _rows(world, scenario)
        assert len(rows) == REQUESTS_PER_ORGANIZATION_PER_DAY
        assert _windows_hold(
            [row["requested_at"] for row in rows],
            ORGANIZATION_WINDOW,
            REQUESTS_PER_ORGANIZATION_PER_DAY,
        )


def test_the_windows_open_exactly_at_one_hour_and_one_day(world: World) -> None:
    stack = world.stack
    scenario = _scenario(world, 2)
    service = world.processes[0]
    for _ in range(REQUESTS_PER_USER_PER_HOUR):
        stack.run(service.request(scenario.contexts[0], scenario.analysis_id))
    stack.clock.advance(USER_WINDOW.total_seconds() - 0.001)
    with pytest.raises(AssistantRateLimited) as early:
        stack.run(service.request(scenario.contexts[0], scenario.analysis_id))
    assert early.value.retry_after_seconds == 1
    stack.clock.advance(0.001)
    stack.run(service.request(scenario.contexts[0], scenario.analysis_id))  # sale de la ventana
    # El día de la organización: 200 justo dentro de la ventana, otra persona.
    later = _scenario(world, 1)
    stack.clock.advance(1)
    edge = stack.now() - ORGANIZATION_WINDOW + timedelta(milliseconds=1)
    _seed(world, later, REQUESTS_PER_ORGANIZATION_PER_DAY, edge)
    with pytest.raises(AssistantRateLimited):
        stack.run(service.request(later.contexts[0], later.analysis_id))
    stack.clock.advance(0.001)  # la primera sembrada sale: queda sitio para una
    stack.run(service.request(later.contexts[0], later.analysis_id))
    with pytest.raises(AssistantRateLimited):
        stack.run(service.request(later.contexts[0], later.analysis_id))


def to_ms(moment: datetime) -> datetime:
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def test_at_the_token_cap_nothing_is_created_nor_built(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = world.stack
    scenario = _scenario(world, 1, "b")
    service = world.processes[0]
    first = stack.run(service.request(scenario.contexts[0], scenario.analysis_id))
    used = sum(int(row["output_tokens"]) for row in stack.requests_of(scenario.organization_id))
    stack.add_tokens(
        scenario.organization_id, input_tokens=0, output_tokens=MONTHLY_OUTPUT_TOKENS - used - 1
    )
    stack.run(service.request(scenario.contexts[0], scenario.analysis_id))  # cabe
    stack.add_tokens(scenario.organization_id, input_tokens=0, output_tokens=1)
    built: list[Any] = []
    real = service_module.build_context

    def counting(*args: Any, **kwargs: Any) -> Any:
        built.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(service_module, "build_context", counting)
    rows = len(stack.requests_of(scenario.organization_id))
    with pytest.raises(AssistantUnavailable) as refused:
        stack.run(service.request(scenario.contexts[0], scenario.analysis_id))
    assert refused.value.cause is AssistantUnavailableCause.BUDGET_EXCEEDED
    assert built == []
    assert len(stack.requests_of(scenario.organization_id)) == rows
    assert first.request_id in {
        row["request_id"] for row in stack.requests_of(scenario.organization_id)
    }
