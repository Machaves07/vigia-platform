"""Un solo uso y un solo ``active`` por nodo con peticiones simultáneas (TASK-218; PR-GOB-15).

Contra PostgreSQL 16 real como ``vigia_app``, con ``EnrollmentCodeService`` real y conexiones
distintas para cada operación (``asyncio.gather`` sobre el pool de la base):

- **Consumo**: 8 altas presentan a la vez el mismo código válido. Todas lo verifican antes de que
  ninguna consuma (barrera), así que solo la condición ``status = 'active'`` del ``UPDATE`` de
  ``consume`` impide un segundo uso: exactamente un éxito y 7 ``enrollment_code_used``.
- **Emisión**: 8 emisiones para el mismo nodo pasan todas ``supersede_active`` antes de que
  ninguna anexe su código (barrera), así que solo el índice único parcial
  ``enrollment_code_one_active_per_node`` impide dos ``active``: una responde con su código y las
  7 perdedoras chocan en él (``ConcurrentIssue``, ``conflict``) sin dejar código ni registro;
  queda exactamente un ``active``.

Las dos pruebas fallan si se quita la condición o el índice (mutaciones en el PR). Topes: la
barrera espera 30 s y la base 60 s (retro 15); nada decide por tiempo de pared.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.fleet_http_support import FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.fleet.application.enrollment_codes import ConcurrentIssue, IssuedCode
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

PARTIES: Final = 8
BARRIER_TIMEOUT_SECONDS: Final = 30.0


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


def test_eight_simultaneous_issues_leave_exactly_one_active_code(fleet: FleetStack) -> None:
    installer, node = _declared_node(fleet)
    store = BarrierEnrollmentStore()
    service = fleet.codes_service(enrollment=store)
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
