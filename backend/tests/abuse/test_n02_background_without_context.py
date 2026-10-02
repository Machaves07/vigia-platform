"""N-2 · Proceso de fondo sin contexto (business-rules §14; SECURITY-11).

**Qué intenta**: un consumidor de la bandeja o una tarea periódica consulta «todas las
organizaciones», sin ``ScopeContext`` o saltándose el que recibió.

**Qué lo detiene** (BR-NUC-02, BR-NUC-80, BR-NUC-81):

- BR-NUC-02: sin contexto, ``ContextAbsent`` **antes** de pedir una conexión (ninguna consulta),
  ``context_absent_attempt`` en la cadena de auditoría de la proveedora y ``security_alert``;
- BR-NUC-80: en un consumidor, ``ContextAbsent`` cuenta como fallo de intento (defecto): tras el
  octavo, cola muerta con ``dead_letter_created``; ningún consumidor consulta sin contexto;
- BR-NUC-81: la tarea periódica recorre una organización por iteración, con el contexto de esa
  organización: aunque la consulta pida «todo», la base solo devuelve lo de esa organización.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import event, text

from tests.dispatch_support import PLAIN, DispatchEnvironment, dispatch_environment
from tests.integration.conftest import PostgresEndpoint
from tests.platform_support import T0, Platform
from vigia_platform.identity.authz.context import ContextAbsentAuditor
from vigia_platform.shared.context import ActorUnit, ContextAbsent
from vigia_platform.shared.outbox.dispatcher import Outcome
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS

pytestmark = pytest.mark.integration

ALL_ORGANIZATIONS = text("SELECT organization_id FROM identity.organization")


@dataclass(frozen=True)
class _Task:
    """Una tarea periódica que «quiere» recorrer todas las organizaciones de una vez."""

    task_name: str = "sweep_everything_probe"
    unit: ActorUnit = ActorUnit.U02


SWEEP = _Task()


@pytest.fixture(scope="module")
def dispatch(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "abuse_n02") as environment:
        yield environment


def _checkouts(database: Any) -> tuple[list[int], Any, Any]:
    """Cuenta las conexiones que se piden a los pools de ``database``."""
    taken: list[int] = []

    def count(*_: object) -> None:
        taken.append(1)

    engines = [pool.engine for pool in database._pools.values()]

    def install() -> None:
        for engine in engines:
            event.listen(engine.sync_engine.pool, "checkout", count)

    def remove() -> None:
        for engine in engines:
            event.remove(engine.sync_engine.pool, "checkout", count)

    return taken, install, remove


def test_n02_a_consumer_that_queries_without_context_ends_in_the_dead_letter_queue(
    dispatch: DispatchEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = dispatch
    env.run(env.quiesce())
    handler = env.handlers[PLAIN]
    attempts: list[type[BaseException]] = []
    taken, install, remove = _checkouts(env.database)

    async def everything(event: OutboxEvent, transaction: Any) -> None:
        # El consumidor ignora la transacción con contexto que recibe y pide «todo».
        install()
        try:
            await env.database.read(None, ALL_ORGANIZATIONS)  # type: ignore[arg-type]
        except BaseException as error:
            attempts.append(type(error))
            raise
        finally:
            remove()

    monkeypatch.setattr(handler, "_handle", everything)
    (published,) = env.run(env.publish(uuid.uuid4(), [None]))
    dispatcher = env.dispatcher()
    for _ in range(MAX_ATTEMPTS):
        report = env.run(dispatcher.dispatch_once(PLAIN))
        assert report.count(Outcome.DELIVERED) == 0
        env.clock.advance(601)
    assert attempts == [ContextAbsent] * MAX_ATTEMPTS
    (letter,) = env.run(env.dead_letters())
    assert (letter["event_id"], letter["consumer_name"], letter["attempts"]) == (
        published.event_id,
        PLAIN,
        MAX_ATTEMPTS,
    )
    (row,) = env.run(
        env.fetch(
            "SELECT last_error_code FROM shared.outbox_delivery"
            " WHERE event_id = $1 AND consumer_name = $2",
            published.event_id,
            PLAIN,
        )
    )
    assert row["last_error_code"] == "context_absent"
    assert [r["organization_id"] for r in env.run(env.events_named("dead_letter_created"))] == [
        published.organization_id
    ]
    # La consulta sin contexto se cortó antes del pool: ninguna conexión, ninguna consulta.
    assert taken == []


def test_n02_the_attempt_is_cut_before_the_pool_audited_and_alerted(platform: Platform) -> None:
    database = platform.authz.sessions.database
    provider = platform.provider
    auditor = ContextAbsentAuditor(contexts=platform.authz.contexts, audit=platform.authz.audit)
    entries = len(platform.audit_entries(provider, "context_absent_attempt"))
    alerts = len(platform.alerts(provider, "context_absent_attempt"))
    taken, install, remove = _checkouts(database)

    async def attempt() -> None:
        install()
        try:
            with pytest.raises(ContextAbsent):
                await database.read(None, ALL_ORGANIZATIONS)
        finally:
            remove()
        await auditor.drain()

    auditor.install()
    try:
        platform.run(attempt())
    finally:
        auditor.uninstall()
    assert taken == []  # ninguna conexión: ninguna consulta
    after = platform.audit_entries(provider, "context_absent_attempt")
    assert len(after) == entries + 1
    assert (after[-1]["outcome"], after[-1]["actor_kind"]) == ("denied", "system")
    assert len(platform.alerts(provider, "context_absent_attempt")) == alerts + 1


def test_n02_a_periodic_iteration_asking_for_everything_sees_only_its_organization(
    platform: Platform,
) -> None:
    first, second = platform.site(), platform.site()
    for site in (first, second):
        platform.gate(site, *site.zones()[0], T0)
    contexts = platform.authz.contexts
    database = platform.authz.sessions.database
    everything = text("SELECT DISTINCT organization_id FROM ledger.ledger_record")

    async def iterate() -> dict[uuid.UUID, set[uuid.UUID]]:
        seen: dict[uuid.UUID, set[uuid.UUID]] = {}
        for organization_id in (first.organization_id, second.organization_id):
            context = contexts.context_for_organization(SWEEP, organization_id)
            rows = await database.read(context, everything)
            seen[organization_id] = {uuid.UUID(str(row.organization_id)) for row in rows}
        return seen

    seen = platform.run(iterate())
    assert seen == {
        first.organization_id: {first.organization_id},
        second.organization_id: {second.organization_id},
    }
