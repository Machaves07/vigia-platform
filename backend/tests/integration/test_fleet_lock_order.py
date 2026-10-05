"""Un solo orden de candados entre las operaciones de identidad del nodo (TASK-218; revisión de
VIG-147, ronda 2; AGENTS.md «Orden de candados entre operaciones»).

El orden está en ``fleet.application.common``: código del nodo → ficha de flota → filas de zona
→ identidad, credenciales y códigos → cadena de la planta → marca global. Cada prueba lanza a la
vez dos operaciones **distintas** que comparten candados, contra PostgreSQL 16 real como
``vigia_app``: la primera escribe su primer registro con el escritor de la flota (ya tiene la
cadena de la planta y todo lo anterior del orden) y retiene su transacción ``HOLD_SECONDS``; la
segunda arranca entonces. Con el orden único, la segunda espera un candado que la primera tomó al
empezar, sin tener nada que la primera necesite, y las dos terminan con el resultado de ir una
detrás de otra. Con el orden invertido (mutaciones en el PR), una de las dos acaba en
``TemporarilyUnavailable`` por bloqueo mutuo, o la segunda decide con un estado viejo.

La retención no decide nada: con el orden bueno el resultado es el mismo dure lo que dure
(retro 14). Topes: la base 60 s (``LOCK_TIMEOUT_MS`` de la pila), la llegada 30 s.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, Final

import pytest

from tests.fleet_http_support import REASON, FleetStack, fleet_stack, new_code
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.application.common import FleetRejected
from vigia_platform.fleet.application.enrollment_codes import IssuedCode
from vigia_platform.fleet.application.node_declaration import (
    DeclaredNode,
    NodeDeclarationService,
)
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.identity.application.common import IdentityRejected, IdentityRejection

pytestmark = pytest.mark.integration

HOLD_SECONDS: Final = 5.0
"""Cuánto retiene la primera operación su transacción tras su primer registro: el tiempo para que
la segunda arranque y tome lo que pueda. No es un tope de «llegó a tiempo»."""
ARRIVAL_TIMEOUT_SECONDS: Final = 30.0


class HoldingWriter:
    """El escritor de la flota: tras el **primer** registro avisa (``arrived``) y retiene la
    transacción ``HOLD_SECONDS``; los demás pasan sin esperar."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.arrived = asyncio.Event()

    async def write(self, *args: Any, **kwargs: Any) -> Any:
        written = await self._inner.write(*args, **kwargs)
        if not self.arrived.is_set():
            self.arrived.set()
            await asyncio.sleep(HOLD_SECONDS)
        return written

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture(scope="module")
def fleet(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_lock_order", pool=6) as stack:
        yield stack


def _holding(fleet: FleetStack) -> tuple[Any, HoldingWriter]:
    writer = HoldingWriter(fleet.deps.writer)
    return dataclasses.replace(fleet.deps, writer=writer), writer


def _race(
    fleet: FleetStack,
    writer: HoldingWriter,
    first: Callable[[], Awaitable[Any]],
    second: Callable[[], Awaitable[Any]],
) -> list[Any]:
    """``first`` hasta su primer registro; entonces ``second``; los dos resultados."""
    fleet.tick()

    async def race() -> list[Any]:
        held = asyncio.ensure_future(first())
        arrival = asyncio.ensure_future(writer.arrived.wait())
        await asyncio.wait(
            {held, arrival}, timeout=ARRIVAL_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
        )
        arrival.cancel()
        assert writer.arrived.is_set(), "la primera operación no llegó a escribir"
        follower = asyncio.ensure_future(second())
        return list(await asyncio.gather(held, follower, return_exceptions=True))

    outcomes: list[Any] = fleet.run(race())
    return outcomes


def _site(fleet: FleetStack, zones: int) -> tuple[Any, uuid.UUID, list[uuid.UUID]]:
    site = fleet.site(plants=1, zones=zones)
    plant = next(iter(site.plants))
    return fleet.installer(site), plant, list(site.plants[plant])


def _declared(fleet: FleetStack, installer: Any, plant: uuid.UUID, zones: list[uuid.UUID]) -> str:
    response = fleet.declare(installer, plant, zones)
    assert response.status_code == 201, response.text
    node: str = response.json()["node_id"]
    return node


def _rejected(outcome: Any, detail: FleetDetailCode) -> bool:
    return isinstance(outcome, FleetRejected) and outcome.detail_code is detail


def test_a_declaration_and_an_assignment_of_its_zone_both_finish(fleet: FleetStack) -> None:
    # La declaración toma la fila de Z al empezar; asignar Z a otro nodo toma la ficha de ese nodo
    # y espera la fila. Sin el bloqueo previo de zonas, la declaración tendría la cadena antes que
    # Z y la asignación Z antes que la cadena (bloqueo mutuo).
    installer, plant, zones = _site(fleet, zones=2)
    other = _declared(fleet, installer, plant, [zones[1]])
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    declarations = NodeDeclarationService(deps)
    assign = fleet.services.declarations
    assert assign is not None

    outcomes = _race(
        fleet,
        writer,
        lambda: declarations.declare(context, plant, code=new_code(), zone_ids=[zones[0]]),
        lambda: assign.assign_zone(context, uuid.UUID(other), zones[0]),
    )

    assert isinstance(outcomes[0], DeclaredNode), outcomes
    assert _rejected(outcomes[1], FleetDetailCode.ZONE_ALREADY_SERVED), outcomes
    assert [a.zone_id for a in outcomes[0].assignments] == [zones[0]]


def test_a_replacement_and_an_unassignment_of_the_old_zone_both_finish(fleet: FleetStack) -> None:
    # El reemplazo toma la ficha del viejo y su zona Z al empezar; retirar Z del viejo toma la
    # ficha del viejo y espera. Sin la ficha en la retirada, tomaría Z y esperaría la cadena que
    # el reemplazo tiene, y el reemplazo esperaría Z.
    installer, plant, zones = _site(fleet, zones=1)
    old = _declared(fleet, installer, plant, [zones[0]])
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    declarations = NodeDeclarationService(deps)
    unassign = fleet.services.declarations
    assert unassign is not None

    outcomes = _race(
        fleet,
        writer,
        lambda: declarations.declare(
            context, plant, code=new_code(), zone_ids=[], replaces_node_id=uuid.UUID(old)
        ),
        lambda: unassign.unassign_zone(context, uuid.UUID(old), zones[0], REASON),
    )

    assert isinstance(outcomes[0], DeclaredNode), outcomes
    # Cuando la retirada entra, Z ya es del nodo nuevo: el viejo no la tiene.
    assert isinstance(outcomes[1], IdentityRejected), outcomes
    assert outcomes[1].code is IdentityRejection.ZONE_WITHOUT_NODE
    assert [a.zone_id for a in outcomes[0].assignments] == [zones[0]]
    assert fleet.node_row(old)["decommissioned_at"] is not None


def test_a_replacement_and_an_issue_for_the_old_node_both_finish(fleet: FleetStack) -> None:
    # El reemplazo toma la ficha del viejo y deja su código superseded antes de la cadena; la
    # emisión para el viejo toma su ficha, espera, y decide con el viejo ya dado de baja.
    installer, plant, zones = _site(fleet, zones=1)
    old = _declared(fleet, installer, plant, [zones[0]])
    assert fleet.issue(installer, old).status_code == 201
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    declarations = NodeDeclarationService(deps)
    codes = fleet.services.enrollment_codes
    assert codes is not None

    outcomes = _race(
        fleet,
        writer,
        lambda: declarations.declare(
            context, plant, code=new_code(), zone_ids=[], replaces_node_id=uuid.UUID(old)
        ),
        lambda: codes.issue(context, uuid.UUID(old)),
    )

    assert isinstance(outcomes[0], DeclaredNode), outcomes
    assert _rejected(outcomes[1], FleetDetailCode.NODE_NOT_DECLARED), outcomes
    assert [row["status"] for row in fleet.codes(old)] == ["superseded"]


def test_a_replacement_and_a_revocation_of_the_old_node_both_finish(fleet: FleetStack) -> None:
    # Los dos toman primero la ficha del viejo: la revocación espera y responde la revocación que
    # dejó el reemplazo, sin un segundo node_revoked.
    installer, plant, zones = _site(fleet, zones=1)
    old = _declared(fleet, installer, plant, [zones[0]])
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    declarations = NodeDeclarationService(deps)
    revocations = NodeRevocationService(fleet.deps)

    outcomes = _race(
        fleet,
        writer,
        lambda: declarations.declare(
            context, plant, code=new_code(), zone_ids=[], replaces_node_id=uuid.UUID(old)
        ),
        lambda: revocations.revoke(context, uuid.UUID(old), REASON),
    )

    assert isinstance(outcomes[0], DeclaredNode), outcomes
    assert not isinstance(outcomes[1], BaseException), outcomes
    assert outcomes[1].dirty_generation is None  # ya constaba revocado
    revoked = [
        record
        for record in fleet.records("node_revoked", context.organization_id)
        if record["content"]["node_id"] == old
    ]
    assert len(revoked) == 1


def test_a_replacement_and_a_declaration_with_the_same_code_both_finish(
    fleet: FleetStack,
) -> None:
    # El reemplazo toma la exclusión del código C al empezar y declara el nuevo al final; la otra
    # declaración con C espera esa exclusión y choca con el código ya usado. Sin la exclusión,
    # la otra insertaría C y esperaría la cadena, y el reemplazo esperaría el índice de C.
    installer, plant, zones = _site(fleet, zones=1)
    old = _declared(fleet, installer, plant, [zones[0]])
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    declarations = NodeDeclarationService(deps)
    plain = fleet.services.declarations
    assert plain is not None
    code = new_code()

    outcomes = _race(
        fleet,
        writer,
        lambda: declarations.declare(
            context, plant, code=code, zone_ids=[], replaces_node_id=uuid.UUID(old)
        ),
        lambda: plain.declare(context, plant, code=code, zone_ids=[]),
    )

    assert isinstance(outcomes[0], DeclaredNode), outcomes
    assert _rejected(outcomes[1], FleetDetailCode.CODE_IN_USE), outcomes


def test_a_revocation_and_an_issue_for_the_same_node_leave_a_consistent_code(
    fleet: FleetStack,
) -> None:
    # Menor 1 de la ronda 2: la emisión decide con la ficha bloqueada, así que ve la revocación
    # ya hecha y emite como re-alta (el nodo pasa a re_enrollment_pending). Decidiendo con una
    # lectura sin bloqueo, emitiría como declared un código active sobre un nodo revocado.
    installer, plant, zones = _site(fleet, zones=1)
    node = _declared(fleet, installer, plant, [zones[0]])
    deps, writer = _holding(fleet)
    context = fleet.context(installer)
    revocations = NodeRevocationService(deps)
    codes = fleet.services.enrollment_codes
    assert codes is not None

    outcomes = _race(
        fleet,
        writer,
        lambda: revocations.revoke(context, uuid.UUID(node), REASON),
        lambda: codes.issue(context, uuid.UUID(node)),
    )

    assert not isinstance(outcomes[0], BaseException), outcomes
    assert isinstance(outcomes[1], IssuedCode), outcomes
    assert outcomes[1].re_enrollment
    assert fleet.node_row(node)["status"] == "re_enrollment_pending"
