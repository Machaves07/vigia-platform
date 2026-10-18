"""PR-LAZ-52, parte de la exportación (TASK-317; PAT-LAZ-ESC-02; NFR-LAZ-25): los límites exactos
de **5 por hora y usuario** y **20 por hora y organización**, contados sobre
``ExportRequest.requested_at`` en la transacción de admisión.

Contra PostgreSQL 16 real como ``vigia_app``, con ``ExportRequests`` sobre **tres pools** (tres
procesos simulados) y las solicitudes de cada tanda lanzadas **a la vez** (``asyncio.gather``):

- **Propiedad**: para cualquier secuencia generada de tandas concurrentes de tres usuarios de una
  organización, separadas por saltos del reloj inyectado (con los bordes de la ventana: 0, 1 s,
  59 min 59 s, 1 h y 1 h 1 s), **nunca se admiten más de las permitidas en ninguna ventana
  deslizante de una hora** (por usuario y por organización), y en cada tanda se admiten
  **exactamente** las que caben (``min`` del hueco de la organización y la suma de los huecos de
  los usuarios): ni una de más ni una de menos. Cada rechazo es ``ExportRateLimited``.
- **El contador y la huella son la misma fila**: las solicitudes admitidas son exactamente las
  filas de ``accreditation.export_request`` (y sus entradas de auditoría); no hay tabla contadora.
- **Concurrencia dirigida**: 12 solicitudes simultáneas de un usuario admiten 5; 30 de seis
  usuarios (cinco cada uno) admiten 20; en tres rondas cada una. La mutación «comprobar y luego
  escribir» (sin el candado consultivo de la organización) cae en rojo aquí (Evidence del PR).

Solo datos generados (NFR-CTR-43). El reloj es el simulado.
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

from tests.export_support import ExportStack, export_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.export.request import (
    EXPORTS_PER_ORGANIZATION_PER_HOUR,
    EXPORTS_PER_USER_PER_HOUR,
    RATE_WINDOW,
    ExportRateLimited,
    ExportRequests,
    ExportScope,
)
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.context import ScopeContext

pytestmark = pytest.mark.integration

PROCESSES: Final = 3
USERS: Final = 3
ROUNDS: Final = 3

gaps = st.one_of(
    st.sampled_from([0, 1, 3_599, 3_600, 3_601]),
    st.integers(0, 4_000),
)
batches = st.lists(
    st.tuples(gaps, st.lists(st.integers(0, USERS - 1), min_size=1, max_size=12)),
    min_size=1,
    max_size=4,
)


@dataclass
class World:
    stack: ExportStack
    processes: list[ExportRequests]


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with export_stack(postgres_endpoint, "export_rate") as stack:
        processes = [
            ExportRequests(
                database=stack.new_database(),
                authorizer=stack.authz.authorizer,
                audit=stack.authz.sessions.audit,
                outbox=stack.outbox,
                clock=stack.clock,
            )
            for _ in range(PROCESSES)
        ]
        yield World(stack, processes)


@dataclass
class Organization:
    organization_id: uuid.UUID
    scope: ExportScope
    contexts: list[ScopeContext]


def _organization(world: World, users: int) -> Organization:
    stack = world.stack
    site = stack.site(plants=1, zones=1)
    plant = next(iter(site.plants))
    start = stack.now() - timedelta(days=10)
    scope = ExportScope(
        plant_id=plant,
        zone_ids=site.plants[plant],
        period_from=start,
        period_to=start + timedelta(days=7),
        package_form=PackageForm.WITHOUT_CLIPS,
    )
    contexts = [stack.context(stack.member(site)) for _ in range(users)]
    return Organization(site.organization_id, scope, contexts)


async def _batch(world: World, organization: Organization, users: Sequence[int]) -> list[Any]:
    """Las solicitudes de la tanda, a la vez, repartidas entre los tres procesos."""
    return await asyncio.gather(
        *(
            world.processes[index % PROCESSES].request(
                organization.contexts[user], organization.scope
            )
            for index, user in enumerate(users)
        ),
        return_exceptions=True,
    )


def _windows_hold(stamps: Sequence[datetime], limit: int) -> bool:
    """Ninguna ventana deslizante de una hora (``(t - 1 h, t]``) tiene más de ``limit``."""
    ordered = sorted(stamps)
    return all(
        sum(1 for other in ordered if moment - RATE_WINDOW < other <= moment) <= limit
        for moment in ordered
    )


def _rows(world: World, organization: Organization) -> list[Any]:
    return world.stack.requests_of(organization.organization_id)


@settings(max_examples=8, deadline=None)
@given(plan=batches)
def test_pr_laz_52_the_exact_limits_hold_under_concurrency(world: World, plan: Any) -> None:
    stack = world.stack
    organization = _organization(world, USERS)
    users_of = {context.actor.id: index for index, context in enumerate(organization.contexts)}
    admitted_total = 0
    for gap, users in plan:
        stack.tick(gap)
        now = stack.now()
        rows = _rows(world, organization)
        in_window = [row for row in rows if row["requested_at"] > now - RATE_WINDOW]
        per_user = Counter(users_of[row["user_id"]] for row in in_window)
        asked = Counter(users)
        room = sum(
            min(count, EXPORTS_PER_USER_PER_HOUR - per_user[user]) for user, count in asked.items()
        )
        expected = min(EXPORTS_PER_ORGANIZATION_PER_HOUR - len(in_window), room)
        results = stack.run(_batch(world, organization, users))
        unexpected = [r for r in results if isinstance(r, BaseException)
                      and not isinstance(r, ExportRateLimited)]  # fmt: skip
        assert unexpected == [], unexpected
        admitted = [result for result in results if not isinstance(result, BaseException)]
        assert len(admitted) == expected, (gap, users, len(admitted), expected)
        admitted_total += len(admitted)
        rows = _rows(world, organization)
        # El contador y la huella son la misma fila.
        assert len(rows) == admitted_total
        assert {row["export_id"] for row in rows} >= {status.export_id for status in admitted}
        assert len(stack.audit(organization.organization_id)) == admitted_total
        for user in range(USERS):
            stamps = [row["requested_at"] for row in rows if users_of[row["user_id"]] == user]
            assert _windows_hold(stamps, EXPORTS_PER_USER_PER_HOUR), user
        assert _windows_hold(
            [row["requested_at"] for row in rows], EXPORTS_PER_ORGANIZATION_PER_HOUR
        )


def test_twelve_simultaneous_requests_of_one_user_admit_exactly_five(world: World) -> None:
    for _ in range(ROUNDS):
        organization = _organization(world, 1)
        results = world.stack.run(_batch(world, organization, [0] * 12))
        admitted = [result for result in results if not isinstance(result, BaseException)]
        limited = [result for result in results if isinstance(result, ExportRateLimited)]
        assert (len(admitted), len(limited)) == (5, 7), results
        assert len(_rows(world, organization)) == 5


def test_thirty_simultaneous_requests_of_an_organization_admit_exactly_twenty(
    world: World,
) -> None:
    for _ in range(ROUNDS):
        organization = _organization(world, 6)
        users = [user for user in range(6) for _ in range(5)]
        results = world.stack.run(_batch(world, organization, users))
        admitted = [result for result in results if not isinstance(result, BaseException)]
        limited = [result for result in results if isinstance(result, ExportRateLimited)]
        assert (len(admitted), len(limited)) == (20, 10), results
        assert len(_rows(world, organization)) == 20
