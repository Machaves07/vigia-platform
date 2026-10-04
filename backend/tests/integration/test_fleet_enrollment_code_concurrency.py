"""Un solo uso y un solo ``active`` por nodo con peticiones simultáneas (TASK-218; PR-GOB-15).

Contra PostgreSQL 16 real como ``vigia_app``, con ``EnrollmentCodeService`` real y conexiones
distintas para cada operación (``asyncio.gather`` sobre el pool de la base):

- **Consumo**: 8 altas presentan a la vez el mismo código válido. Todas lo verifican antes de que
  ninguna consuma (barrera), así que solo la condición ``status = 'active'`` del ``UPDATE`` de
  ``consume`` impide un segundo uso: exactamente un éxito y 7 ``enrollment_code_used``.
- **Emisión, ficha bloqueada** (revisión de la ronda 2): 3 emisiones simultáneas del mismo nodo se
  ordenan en la ficha (``FOR UPDATE``): cada una retiene su transacción tras ``supersede_active``
  y ninguna otra la alcanza; terminan las 3 y queda exactamente un ``active`` (el último).
- **Emisión, índice**: con la ficha sin bloquear (``UnlockedNodes``, la segunda barrera sola), 8
  emisiones pasan todas ``supersede_active`` antes de que ninguna anexe su código (barrera), así
  que solo el índice único parcial ``enrollment_code_one_active_per_node`` impide dos ``active``:
  una responde con su código y las 7 perdedoras chocan en él (``ConcurrentIssue``, ``conflict``)
  sin dejar código ni registro.

- **Marca global de la lista** (revisión de la ronda 1): 8 revocaciones de nodos de organizaciones
  distintas llegan juntas a la marca (barrera) y ``dirty_generation`` sube exactamente 8; una
  revocación y una re-alta de la misma planta terminan las dos (el mismo orden de candados, cadena
  de la planta → fila global, en los dos caminos).

Las pruebas fallan si se quita la condición, el índice, la atomicidad del incremento o el orden
de los candados (mutaciones en el PR). Topes: la barrera espera 30 s y la base 60 s (retro 15);
nada decide por tiempo de pared.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.fleet_http_support import REASON, FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.adapters.postgres.revocation_mark_store import (
    PostgresRevocationMarkStore,
)
from vigia_platform.fleet.application.enrollment_codes import (
    ConcurrentIssue,
    IssuedCode,
)
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult
from vigia_platform.fleet.domain.node_fleet_record import FleetNode
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

PARTIES: Final = 8
QUEUED: Final = 3
HOLD_SECONDS: Final = 5.0
"""Cuánto retiene cada emisión su transacción esperando que otra la alcance. Con la ficha
bloqueada nunca llega nadie y la espera se agota sin decidir nada (retro 14)."""
BARRIER_TIMEOUT_SECONDS: Final = 30.0
GATE_SECONDS: Final = 15.0
"""Cuánto espera la revocación a que la re-alta marque antes que ella. Con el orden de candados
bueno la re-alta no puede marcar (espera la cadena que tiene la revocación) y la espera se agota
sin decidir nada: no es un tope de «llegó a tiempo» (retro 14)."""


@pytest.fixture(scope="module")
def fleet(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    # Una conexión por operación simultánea, y holgura para las lecturas de la prueba.
    with fleet_stack(postgres_endpoint, "fleet_code_concurrency", pool=3 * PARTIES) as stack:
        yield stack


class BarrierEnrollmentStore(PostgresEnrollmentStore):
    """Retiene la primera pasada de cada emisión tras ``supersede_active`` hasta que las
    ``PARTIES`` la han hecho: ninguna ha anexado aún su código."""

    def __init__(self) -> None:
        self.barrier = asyncio.Barrier(PARTIES)
        self.passes = 0

    async def supersede_active(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        changed = await super().supersede_active(transaction, node_id)
        self.passes += 1
        if self.passes <= PARTIES:
            async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
                await self.barrier.wait()
        return changed


class UnlockedNodes(PostgresNodeFleetStore):
    """La ficha leída sin bloquear: deja al índice único como la única barrera de la emisión."""

    async def lock(self, transaction: Transaction, node_id: uuid.UUID) -> FleetNode | None:
        return await self.read(transaction, node_id)


class HoldingEnrollmentStore(PostgresEnrollmentStore):
    """Retiene cada emisión tras ``supersede_active`` hasta que otra la alcance, o
    ``HOLD_SECONDS``: con la ficha bloqueada ninguna la alcanza y la espera se agota sin decidir
    nada; sin el candado, las demás pasan a la vez y chocan en el índice."""

    def __init__(self) -> None:
        self.inside = 0
        self.overlapped = asyncio.Event()

    async def supersede_active(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        changed = await super().supersede_active(transaction, node_id)
        self.inside += 1
        if self.inside > 1:
            self.overlapped.set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(HOLD_SECONDS):
                await self.overlapped.wait()
        self.inside -= 1
        return changed


class BarrierMarks(PostgresRevocationMarkStore):
    """Retiene cada marca hasta que las ``parties`` revocaciones han llegado a ella: todas leen
    la generación antes de que ninguna confirme."""

    def __init__(self, parties: int) -> None:
        self.barrier = asyncio.Barrier(parties)

    async def mark_dirty(self, transaction: Transaction, marked_at: datetime) -> int:
        async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
            await self.barrier.wait()
        return await super().mark_dirty(transaction, marked_at)


class GatedMarks(PostgresRevocationMarkStore):
    """La revocación: avisa de que ya escribió su registro (tiene la cadena de la planta) y espera
    a que la re-alta haya marcado, o ``GATE_SECONDS`` si la re-alta no llega a marcar antes."""

    def __init__(self, arrived: asyncio.Event, other_marked: asyncio.Event) -> None:
        self.arrived = arrived
        self.other_marked = other_marked

    async def mark_dirty(self, transaction: Transaction, marked_at: datetime) -> int:
        self.arrived.set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(GATE_SECONDS):
                await self.other_marked.wait()
        return await super().mark_dirty(transaction, marked_at)


class SignalingMarks(PostgresRevocationMarkStore):
    """La re-alta: avisa en cuanto su marca tiene la fila global."""

    def __init__(self, marked: asyncio.Event) -> None:
        self.marked = marked

    async def mark_dirty(self, transaction: Transaction, marked_at: datetime) -> int:
        generation = await super().mark_dirty(transaction, marked_at)
        self.marked.set()
        return generation


def _declared_node(fleet: FleetStack) -> tuple[Any, str]:
    site = fleet.site(plants=1, zones=1)
    plant = next(iter(site.plants))
    installer = fleet.installer(site)
    response = fleet.declare(installer, plant, [])
    assert response.status_code == 201, response.text
    return installer, response.json()["node_id"]


def _organization(fleet: FleetStack, node: str) -> uuid.UUID:
    (row,) = fleet.fetch(
        "SELECT organization_id FROM identity.node_identity WHERE node_id = $1", uuid.UUID(node)
    )
    return uuid.UUID(str(row["organization_id"]))


def _context(fleet: FleetStack, who: Any) -> ScopeContext:
    cookie, concession = who
    scope = fleet.run(fleet.authz.contexts.context_from_session(cookie, concession_id=concession))
    context: ScopeContext = scope.context
    return context


def test_eight_simultaneous_consumptions_of_one_code_leave_one_success(fleet: FleetStack) -> None:
    installer, node = _declared_node(fleet)
    issued = fleet.issue(installer, node)
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    service = fleet.services.enrollment_codes
    assert service is not None
    scope = fleet.run(
        fleet.authz.contexts.context_from_node_enrollment(
            PostgresNodeContextStore(fleet.database), uuid.UUID(node)
        )
    )
    now = fleet.authz.now()
    barrier = asyncio.Barrier(PARTIES)

    async def enroll() -> str:
        check = await service.verify(scope, code)
        assert check.valid and check.code is not None
        async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
            await barrier.wait()  # todas verificaron: ninguna ha consumido
        async with fleet.database.transaction(scope.context) as transaction:
            consumed = await service.consume(transaction, check.code.code_id, now)
        if consumed:
            return EnrollmentAttemptResult.ACCEPTED.value
        return (await service.verify(scope, code)).result.value

    async def race() -> list[str]:
        return list(await asyncio.gather(*(enroll() for _ in range(PARTIES))))

    results = fleet.run(race())

    assert sorted(results) == ["accepted"] + ["enrollment_code_used"] * (PARTIES - 1), results
    assert [row["status"] for row in fleet.codes(node)] == ["used"]


def test_simultaneous_issues_queue_on_the_record_and_leave_one_active_code(
    fleet: FleetStack,
) -> None:
    installer, node = _declared_node(fleet)
    store = HoldingEnrollmentStore()
    service = fleet.codes_service(enrollment=store)
    context = _context(fleet, installer)

    async def race() -> list[Any]:
        return list(
            await asyncio.gather(
                *(service.issue(context, uuid.UUID(node)) for _ in range(QUEUED)),
                return_exceptions=True,
            )
        )

    outcomes = fleet.run(race())

    # Ninguna emisión alcanzó a otra dentro de su transacción: todas terminan, en fila.
    assert all(isinstance(outcome, IssuedCode) for outcome in outcomes), outcomes
    assert not store.overlapped.is_set()
    rows = fleet.codes(node)
    assert sorted(row["status"] for row in rows) == ["active"] + ["superseded"] * (QUEUED - 1)
    assert len(fleet.records("enrollment_code_issued", _organization(fleet, node))) == QUEUED


def test_eight_simultaneous_issues_leave_exactly_one_active_code(fleet: FleetStack) -> None:
    installer, node = _declared_node(fleet)
    store = BarrierEnrollmentStore()
    service = fleet.codes_service(enrollment=store, nodes=UnlockedNodes(fleet.deps.database))
    context = _context(fleet, installer)

    async def race() -> list[Any]:
        return list(
            await asyncio.gather(
                *(service.issue(context, uuid.UUID(node)) for _ in range(PARTIES)),
                return_exceptions=True,
            )
        )

    outcomes = fleet.run(race())

    issued = [outcome for outcome in outcomes if isinstance(outcome, IssuedCode)]
    lost = [outcome for outcome in outcomes if isinstance(outcome, ConcurrentIssue)]
    assert (len(issued), len(lost)) == (1, PARTIES - 1), outcomes
    # Exactamente un active: el de la única respuesta 201; las perdedoras no dejan nada.
    rows = fleet.codes(node)
    assert [(str(row["code_id"]), row["status"]) for row in rows] == [
        (str(issued[0].code_id), "active")
    ]
    assert len(fleet.records("enrollment_code_issued", _organization(fleet, node))) == 1
    assert store.passes == PARTIES


# --- Marca global de la lista de revocación (revisión de VIG-147, ronda 1) ----------------------


def test_simultaneous_revocations_raise_the_dirty_generation_by_exactly_n(
    fleet: FleetStack,
) -> None:
    # Una organización por nodo: las cadenas de planta no se comparten, así que las PARTIES
    # revocaciones llegan juntas a la marca (barrera) y solo el incremento atómico de la fila
    # global impide perder alguna.
    targets = []
    for _ in range(PARTIES):
        installer, node = _declared_node(fleet)
        targets.append((fleet.context(installer), uuid.UUID(node)))
    service = NodeRevocationService(dataclasses.replace(fleet.deps, marks=BarrierMarks(PARTIES)))
    before = fleet.revocation_state()["dirty_generation"]

    async def race() -> list[Any]:
        return list(
            await asyncio.gather(
                *(service.revoke(context, node, REASON) for context, node in targets),
                return_exceptions=True,
            )
        )

    outcomes = fleet.run(race())

    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert not failures, failures
    generations = sorted(outcome.dirty_generation for outcome in outcomes)
    assert generations == list(range(before + 1, before + PARTIES + 1)), generations
    assert fleet.revocation_state()["dirty_generation"] == before + PARTIES


def test_a_revocation_and_a_re_enrollment_in_one_plant_both_finish(fleet: FleetStack) -> None:
    # La revocación de A escribe node_revoked (cadena de la planta) y espera en la marca; la
    # re-alta de B (credencial vencida) revoca su credencial y emite. Con el orden de candados
    # común (cadena → fila global) la re-alta espera la cadena y las dos terminan; con la marca
    # de la re-alta antes de su registro, cada una espera a la otra (bloqueo mutuo).
    site = fleet.site(plants=1, zones=2)
    plant = next(iter(site.plants))
    zones = site.plants[plant]
    installer = fleet.installer(site)
    revoked = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    expired = fleet.declare(installer, plant, [zones[1]]).json()["node_id"]
    fleet.enroll(expired, plant, zones[1], expires_at=fleet.authz.now() - timedelta(seconds=1))
    context = fleet.context(installer)
    arrived, marked = asyncio.Event(), asyncio.Event()
    revocations = NodeRevocationService(
        dataclasses.replace(fleet.deps, marks=GatedMarks(arrived, marked))
    )
    issues = fleet.codes_service(marks=SignalingMarks(marked))
    before = fleet.revocation_state()["dirty_generation"]

    async def race() -> list[Any]:
        revocation = asyncio.ensure_future(revocations.revoke(context, uuid.UUID(revoked), REASON))
        async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
            await arrived.wait()  # la revocación ya tiene la cadena de la planta
        reissue = asyncio.ensure_future(issues.issue(context, uuid.UUID(expired)))
        return list(await asyncio.gather(revocation, reissue, return_exceptions=True))

    outcomes = fleet.run(race())

    failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert not failures, failures
    assert isinstance(outcomes[1], IssuedCode) and outcomes[1].re_enrollment
    assert fleet.node_row(revoked)["status"] == "revoked"
    assert fleet.node_row(expired)["status"] == "re_enrollment_pending"
    assert fleet.revocation_state()["dirty_generation"] == before + 2
