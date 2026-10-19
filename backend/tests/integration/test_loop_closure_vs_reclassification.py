"""Cierre y reclasificación concurrentes sobre el mismo hallazgo (TASK-343, VIG-233; orden de
candados de ``loop.application.locks``; VIG-212, BR-LAZ-25; AGENTS.md «Orden de candados entre
operaciones»).

Contra PostgreSQL 16 real como ``vigia_app``, con ``ClosureService`` y ``ClassificationService``
reales (``actions_stack``): ``ClosureService.close`` (T09) y ``ClassificationService.classify``
(T15 o T19, VIG-212) comparten **un solo** candado de los cinco del módulo: ``finding`` (paso 1 de
``LOCK_ORDER``). La reclasificación nunca toma ``corrective_actions`` (paso 2; su propio docstring
lo dice: «La clasificación no toca ``loop.corrective_action``»). Con un solo candado compartido,
un interbloqueo entre estas dos operaciones es **topológicamente imposible**: quien llega primero a
``pg_advisory_xact_lock`` se queda con el candado hasta confirmar o revertir; la otra espera, lee el
estado que dejó la primera y responde en consecuencia. Esta prueba demuestra esa coherencia (nunca
``TemporarilyUnavailable``, y el estado final es el que deja quien escribió al final) en los dos
sentidos de la carrera.

Como el par cierre/reclasificación no puede interbloquearse por construcción (un solo candado
compartido), la mutación del orden que exige la tabla de garantías del PR se demuestra sobre el par
que **sí** comparte los dos primeros candados del módulo (``finding`` y ``corrective_actions``):
cierre contra reasignación de la acción vigente, exactamente como ``test_loop_locks_order.py``
(TASK-311) ya probó para reasignar contra añadir evidencia. Invertir, solo en
``ClosureService.close``, el orden de ``lock_finding`` y ``lock_actions`` (tomar
``corrective_actions`` antes que ``finding``) cruza con el orden correcto de ``ActionService`` y
PostgreSQL detecta el interbloqueo: una de las dos operaciones falla. La prueba de abajo corre con
el orden correcto (en verde); la inversión se verificó a mano, fuera de la suite, y su salida en
rojo queda citada en ``Evidence`` del PR.

Solo datos generados. Topes de las esperas: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from vigia_platform.loop.application.actions import (
    ActionRejected,
    ActionService,
    ActionUnavailable,
    AssignmentResult,
)
from vigia_platform.loop.application.classification import (
    ClassificationRejected,
    ClassificationResult,
    ClassificationUnavailable,
)
from vigia_platform.loop.application.closure import (
    ClosureRejected,
    ClosureResult,
    ClosureService,
    ClosureUnavailable,
)
from vigia_platform.loop.application.locks import LOCK_ORDER
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0
"""Tope para ver a la otra operación esperando un candado (no decide el resultado)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ActionsStack]:
    with actions_stack(postgres_endpoint, "loop_closure_vs_reclassification") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: ActionsStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


async def _waiting_locks(stack: ActionsStack) -> int:
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


def test_the_declared_order_is_finding_then_actions_then_chain() -> None:
    assert LOCK_ORDER == (
        "finding",
        "corrective_actions",
        "plant_chain",
        "projection_checkpoint",
        "subject_rows",
    )


# --- cierre vs reclasificación: un solo candado compartido, coherencia sin interbloqueo -----


@pytest.mark.parametrize("round_", range(3))
def test_close_and_reclassify_race_without_deadlock_or_unavailable(
    stack: ActionsStack, round_: int
) -> None:
    coordinator: Who = stack.member(stack.world.a)
    assignee = stack.user(stack.world.a)
    finding, action_id = stack.with_evidence(coordinator, assignee)
    fsid = stack.finding_state_id(finding)
    context = stack.context(coordinator)

    async def close() -> Any:
        try:
            return await stack.closure.close(
                context,
                fsid,
                closure_description=stack.closure_body()["closure_description"],
                verification_note=stack.closure_body()["verification_note"],
                professional_license=stack.closure_body()["professional_license"],
            )
        except (ClosureRejected, ClosureUnavailable) as error:
            return error

    async def reclassify() -> Any:
        try:
            return await stack.classification.classify(
                context,
                fsid,
                outcome="false_positive",
                reason_category="no_person_present",
                reason_text="Revisión posterior: no había ninguna persona en el cruce",
                reinforced_reason=True,
            )
        except (ClassificationRejected, ClassificationUnavailable) as error:
            return error

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(close(), reclassify(), return_exceptions=True))

    closed, reclassified = stack.run(race())
    # Nunca TemporarilyUnavailable (ni ninguna excepción no controlada): un interbloqueo es
    # imposible con un solo candado compartido (``finding``).
    assert not isinstance(closed, BaseException) or isinstance(closed, ClosureRejected)
    assert not isinstance(reclassified, BaseException) or isinstance(
        reclassified, ClassificationRejected
    )
    row = stack.world.finding_state(finding)
    assert row is not None
    if isinstance(closed, ClosureResult):
        # El cierre ganó la exclusión del hallazgo primero: la reclasificación, al llegar,
        # encuentra ``closed`` y la salida distinta de ``confirmed`` es T19 (VIG-212, BR-LAZ-25).
        assert isinstance(reclassified, ClassificationResult), reclassified
        assert row["lifecycle_state"] == "closed"
        assert row["closed_with_later_different_classification"] is True
        assert row["closure_record_id"] == closed.record_id
    else:
        # La reclasificación ganó primero (T15, suspende el lazo activo): el cierre, al llegar,
        # encuentra ``suspended_by_reclassification`` y no hay transición de cierre desde ahí.
        assert isinstance(closed, ClosureRejected), closed
        assert closed.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
        assert isinstance(reclassified, ClassificationResult), reclassified
        assert row["lifecycle_state"] == "suspended_by_reclassification"
        assert row["previous_lifecycle_state"] == "action_assigned"
    assert stack.action_rows(finding)[0]["action_id"] == action_id  # nunca se tocó por el cierre


# --- la mutación: cierre y reasignación comparten ``finding`` y ``corrective_actions`` -------


class _HoldsFirst:
    """Con sus candados tomados, espera a que la otra operación esté esperando un candado."""

    stack: ActionsStack
    holding: asyncio.Event

    async def _checked(self, transaction: Transaction) -> None:
        self.holding.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            while await _waiting_locks(self.stack) == 0:
                await asyncio.sleep(0.05)


class _FirstActions(_HoldsFirst, ActionService):
    pass


class _FirstClosure(_HoldsFirst, ClosureService):
    pass


def test_close_and_reassign_at_once_share_the_declared_lock_order(stack: ActionsStack) -> None:
    """Cierre y reasignación comparten ``finding`` y ``corrective_actions`` (pasos 1 y 2): con el
    orden declarado, ninguna de las dos se interbloquea ni recibe ``TemporarilyUnavailable``.

    Esta es la prueba cruzada real del orden de candados para esta pareja (las dos toman los
    mismos dos candados, en el mismo orden). La mutación de la tabla de garantías del PR —
    invertir, solo en ``ClosureService.close``, el orden de ``lock_finding`` y ``lock_actions``—
    se verificó a mano contra esta misma prueba: con la inversión, PostgreSQL detecta el
    interbloqueo y una de las dos operaciones falla (ver ``Evidence`` del PR); se repuso el orden
    correcto y esta prueba queda en verde, tal como se la ve aquí."""
    world = stack.world
    coordinator = stack.member(world.a)
    finding, action_id = stack.with_evidence(coordinator, stack.user(world.a))
    fsid = stack.finding_state_id(finding)
    context = stack.context(coordinator)
    replacement = stack.user(world.a)
    holding = asyncio.Event()
    closure: Any = stack.service_like(_FirstClosure)
    reassign_service: Any = stack.service_like(_FirstActions)
    closure.stack, closure.holding = stack, holding
    reassign_service.stack, reassign_service.holding = stack, holding

    async def close() -> Any:
        try:
            return await closure.close(
                context,
                fsid,
                closure_description=stack.closure_body()["closure_description"],
                verification_note=stack.closure_body()["verification_note"],
                professional_license=stack.closure_body()["professional_license"],
            )
        except (ClosureRejected, ClosureUnavailable) as error:
            return error

    async def reassign() -> Any:
        try:
            return await reassign_service.assign(
                context,
                fsid,
                action_type="engineering",
                action_description="Reinstalar el resguardo fijo en el acceso de la zona",
                assignee_user_id=replacement,
                deadline_days=15,
                supersedes_action_id=action_id,
            )
        except ActionRejected as error:
            return error

    async def race() -> list[Any]:
        async with asyncio.timeout(4 * LOCK_WAIT_SECONDS):
            return list(await asyncio.gather(close(), reassign(), return_exceptions=True))

    closed, reassigned = stack.run(race())
    # Nunca interbloqueo ni TemporarilyUnavailable: el orden declarado (``finding`` y después
    # ``corrective_actions``) sirve a las dos operaciones por igual. Como las dos toman los
    # mismos dos candados en el mismo orden, quien llega primero a ``finding`` termina del todo
    # (incluida la cadena y la proyección) antes de que la otra pueda seguir: la serie queda
    # limpia, sin entrelazado más fino que comprobar.
    assert not isinstance(closed, ClosureUnavailable), closed
    assert not isinstance(reassigned, ActionUnavailable), reassigned
    row = stack.world.finding_state(finding)
    assert row is not None
    if isinstance(closed, ClosureResult):
        # El cierre tomó ``finding`` primero y cerró con la acción original y su evidencia; la
        # reasignación, al llegar, ve el hallazgo ya ``closed`` (no ``action_assigned`` ni
        # ``overdue``) y no hay transición de reasignación desde ahí.
        assert isinstance(reassigned, ActionRejected), reassigned
        assert reassigned.detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
        assert row["lifecycle_state"] == "closed"
        assert row["closure_record_id"] == closed.record_id
        assert stack.action_rows(finding)[0]["action_id"] == action_id
    else:
        # La reasignación tomó ``finding`` primero y sustituyó la acción (BR-LAZ-49, el estado no
        # cambia): el cierre, al llegar, ve la acción **nueva** vigente, que no tiene ninguna
        # entrada de evidencia propia, y responde ``closure_evidence_missing`` sin efecto.
        assert isinstance(reassigned, AssignmentResult), reassigned
        assert isinstance(closed, ClosureRejected), closed
        assert closed.detail_code is LoopDetailCode.CLOSURE_EVIDENCE_MISSING
        assert row["lifecycle_state"] == "action_assigned"
        states = {a["action_id"]: a["state"] for a in stack.action_rows(finding)}
        assert states == {action_id: "superseded", reassigned.action_id: "assigned"}
