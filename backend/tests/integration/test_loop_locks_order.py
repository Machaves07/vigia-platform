"""El orden único de candados del lazo (TASK-311; ``loop.application.locks``; AGENTS.md).

Prueba **cruzada** sobre PostgreSQL 16 real como ``vigia_app``: dos operaciones **distintas** que
comparten candados, a la vez y sobre el mismo hallazgo, desde dos conexiones:

- **reasignar la acción** (``ActionService.assign`` con ``supersedes_action_id``): hallazgo →
  acciones → cadena de la planta → control de la proyección → filas del sujeto;
- **añadir evidencia** a la acción vigente (``EvidenceService.add``): el mismo orden.

La primera en llegar espera, con sus candados tomados, a que la otra esté **esperando** un candado
(``pg_locks``); así el entrelazado es siempre el mismo y no depende de la suerte. En los dos
sentidos ninguna termina en interbloqueo ni en ``TemporarilyUnavailable``, y el estado final es
coherente con el último registro:

- reasignar primero: la evidencia llega tarde a una acción ya sustituida y recibe
  ``transition_not_allowed`` (no queda entrada, P5);
- evidencia primero: la entrada queda sobre la acción que después se sustituye.

En ambos, la acción anterior queda ``superseded``, la nueva es la vigente de la proyección y
``last_record_id`` es el ``action_assigned`` de la reasignación.

**Mutación** (tabla de garantías del PR): tomar en ``EvidenceService.add`` la exclusión del
hallazgo y las acciones **después** de escribir en el expediente (invertir el orden) hace que la
evidencia tenga la cadena y espere el hallazgo mientras la reasignación tiene el hallazgo y espera
la cadena: PostgreSQL detecta el interbloqueo y una de las dos falla; la prueba cae en rojo.

Solo datos generados. Topes de las esperas: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import DESCRIPTION, EVIDENCE, ROLE, ActionsStack, actions_stack
from vigia_platform.loop.application.actions import (
    ActionRejected,
    ActionService,
    AssignmentResult,
)
from vigia_platform.loop.application.evidence import EvidenceResult, EvidenceService
from vigia_platform.loop.application.locks import LOCK_ORDER
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver a la otra operación esperando un candado (no decide el resultado)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "loop_locks_order") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: ActionsStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


async def _waiting_locks(stack: ActionsStack) -> int:
    """Candados pedidos y no concedidos en la base de la prueba (de cualquier tipo)."""
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


class _HoldsFirst:
    """Mezcla: con sus candados tomados, avisa (``holding``) y espera a que la otra operación
    esté esperando un candado; después sigue y escribe."""

    stack: ActionsStack
    holding: asyncio.Event

    async def _checked(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


class _FirstActions(_HoldsFirst, ActionService):
    pass


class _FirstEvidence(_HoldsFirst, EvidenceService):
    pass


def test_the_declared_order_is_finding_then_actions_then_chain() -> None:
    assert LOCK_ORDER == (
        "finding",
        "corrective_actions",
        "plant_chain",
        "projection_checkpoint",
        "subject_rows",
    )


@pytest.mark.parametrize("first", ["reassign", "evidence"])
@pytest.mark.parametrize("round_", range(3))
def test_reassign_and_add_evidence_at_once_share_one_lock_order(
    stack: ActionsStack, first: str, round_: int
) -> None:
    world = stack.world
    coordinator = stack.member(world.a)
    finding = stack.opened()
    previous = stack.user(world.a)
    response = stack.assign(coordinator, finding, stack.action_body(previous))
    assert response.status_code == 201, response.text
    action_id = uuid.UUID(response.json()["action_id"])
    fsid = stack.finding_state_id(finding)
    context = stack.context(coordinator)
    replacement = stack.user(world.a)
    holding = asyncio.Event()
    actions: Any = stack.service_like(_FirstActions if first == "reassign" else ActionService)
    evidence: Any = stack.service_like(_FirstEvidence if first == "evidence" else EvidenceService)
    for service in (actions, evidence):
        service.stack, service.holding = stack, holding
    performed = format_timestamp(stack.now() - timedelta(hours=1))
    stack.tick()

    async def reassign() -> Any:
        if first != "reassign":
            await holding.wait()
        return await actions.assign(
            context,
            fsid,
            action_type="engineering",
            action_description=DESCRIPTION,
            assignee_user_id=replacement,
            deadline_days=15,
            supersedes_action_id=action_id,
        )

    async def add() -> Any:
        if first != "evidence":
            await holding.wait()
        return await evidence.add(
            context,
            action_id,
            description=EVIDENCE,
            performed_at=performed,
            performed_by_role=ROLE,
        )

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(reassign(), add(), return_exceptions=True))

    reassigned, added = stack.run(race())
    # Sin interbloqueo ni TemporarilyUnavailable: la reasignación gana siempre; la evidencia se
    # escribe si llegó antes, o recibe transition_not_allowed si llegó a una acción sustituida.
    assert isinstance(reassigned, AssignmentResult), repr(reassigned)
    if first == "evidence":
        assert isinstance(added, EvidenceResult), repr(added)
        assert [str(e["action_id"]) for e in stack.entry_rows(action_id)] == [str(action_id)]
    else:
        assert isinstance(added, ActionRejected), repr(added)
        assert added.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
        assert stack.entry_rows(action_id) == []
    states = {a["action_id"]: a["state"] for a in stack.action_rows(finding)}
    assert states == {action_id: "superseded", reassigned.action_id: "assigned"}
    # Coherente con el último registro del sujeto que cambia la proyección.
    last = stack.records_of(finding, "action_assigned")[-1]
    assert last["record_id"] == reassigned.record_id
    row = world.finding_state(finding)
    assert row is not None
    assert row["last_record_id"] == reassigned.record_id
    assert (row["lifecycle_state"], row["current_action_id"]) == (
        "action_assigned",
        reassigned.action_id,
    )
    assert row["assignee_user_id"] == replacement
