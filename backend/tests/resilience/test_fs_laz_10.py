"""FS-LAZ-10: tarea periódica que arranca dos veces con el arrendamiento vencido artificialmente.

- ``evaluate_deadlines`` con dos ``PeriodicScheduler`` reales de U-02 (dos dueños) sobre
  ``shared.periodic_task``: la primera ejecución, con los candados de sus hallazgos tomados, ve
  vencer su arrendamiento de 60 s en el reloj inyectado y la segunda lo toma mientras la primera
  sigue viva. La valla de ``advance`` deshace lo de la primera; la segunda declara cada vencimiento
  con su clave ``UUID v5 (action_id, due_at, level)`` (U04-H-07) y una tercera, a la hora
  siguiente, no escribe nada: los registros son exactamente los de una sola ejecución.
- ``monthly_package`` con dos ``MonthlyPackageTask`` reales (dueños distintos) sobre PostgreSQL y
  LocalStack: el arrendamiento de la composición de la primera vence (10 min) mientras compone, la
  segunda sella, la primera ya no sube ni sella y una tercera pasada no vuelve a sellar.

Solo datos generados. Un solo reloj simulado; los topes de pared (120 s) solo evitan colgarse.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.integration.test_monthly_package_task import _BASELINE, BOGOTA, World
from tests.laz_support import LoopOrganization, loop_world
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from tests.resilience.harness import scenario
from tests.resilience.laz_support import PROFILES
from tests.signing_support import bootstrapped_world
from vigia_platform.accreditation.monthly.package import MonthlyPackageComposer
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository, object_key
from vigia_platform.accreditation.monthly.task import MonthlyPackageTask
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.loop.application.deadlines import (
    EVALUATE_DEADLINES_TASK,
    DeadlineEvaluator,
    overdue_id,
    register_evaluate_deadlines,
)
from vigia_platform.loop.calendar import month_bounds
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import EscalationLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.storage import S3Storage
from vigia_platform.shared.worker.leases import LeaseSettings, SqlLeaseStore, TaskOutcome
from vigia_platform.shared.worker.scheduler import PeriodicScheduler

pytestmark = pytest.mark.integration

WAIT_SECONDS: Final = 120.0
"""Tope de pared de las esperas entre las dos ejecuciones: no decide el resultado."""
DAY: Final = timedelta(days=1)


# --- evaluate_deadlines ---------------------------------------------------------------------------


async def _waiting_locks(stack: DeadlinesStack) -> int:
    rows = await stack.actions.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted"
        " AND (database IS NULL"
        " OR database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    )
    return int(rows[0]["n"])


class _ExpiresWhileHolding(DeadlineEvaluator):
    """Primera ejecución: con todos sus candados, vence su arrendamiento (reloj inyectado),
    arranca la segunda ejecución y espera a verla bloqueada en un candado; después escribe."""

    stack: DeadlinesStack
    organization_id: uuid.UUID
    second: Any
    started: list[asyncio.Task[Any]]

    async def _checked(self, transaction: Transaction) -> None:
        if transaction.context.organization_id != self.organization_id or self.started:
            return
        (row,) = await self.stack.actions.world.stack.authz.sessions.admin.fetch(
            "SELECT lease_until FROM shared.periodic_task WHERE task_name = $1",
            EVALUATE_DEADLINES_TASK,
        )
        # Vencido en el mismo reloj que compara el arrendamiento; la tarea sigue viva.
        self.stack.tick_to(row["lease_until"] + timedelta(milliseconds=1))
        self.started.append(asyncio.create_task(self.second.run_due(EVALUATE_DEADLINES_TASK)))
        async with asyncio.timeout(WAIT_SECONDS):
            while await _waiting_locks(self.stack) == 0 and not self.started[0].done():
                await asyncio.sleep(0.05)


def _scheduler(stack: DeadlinesStack, evaluator: DeadlineEvaluator, owner: str) -> Any:
    registry = PeriodicTaskRegistry()
    register_evaluate_deadlines(registry, evaluator)
    authz = stack.actions.world.stack.authz
    database = stack.actions.database
    return PeriodicScheduler(
        database=database,
        registry=registry,
        leases=SqlLeaseStore(database=database, contexts=authz.contexts, settings=LeaseSettings()),
        contexts=authz.contexts,
        clock=authz.sessions.clock,
        owner=owner,
        handler_timeout_seconds=WAIT_SECONDS,
    )


def _due_now(stack: DeadlinesStack) -> None:
    stack.actions.world.stack.execute(
        "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
        " VALUES ($1, 'U-04', 'every:3600s', $2)"
        " ON CONFLICT (task_name) DO UPDATE SET next_run_at = EXCLUDED.next_run_at,"
        " lease_owner = NULL, lease_until = NULL, progress_run_at = NULL,"
        " progress_organization_id = NULL, progress_failures = 0",
        EVALUATE_DEADLINES_TASK,
        stack.now(),
    )


def _deadlines_round(stack: DeadlinesStack, organization: LoopOrganization) -> dict[str, Any]:
    stack.set_highlight_days(organization, 1)
    coordinator = stack.actions.member(organization)
    assignee = stack.actions.user(organization)
    one, action_one, due_one = stack.with_action(organization, coordinator, assignee, 1)
    two, action_two, due_two = stack.with_action(organization, coordinator, assignee, 3)
    stack.tick_to(max(due_one + DAY, due_two) + timedelta(minutes=1))
    expected = {
        str(overdue_id(action_one, due_one, EscalationLevel.COORDINATOR)),
        str(overdue_id(action_one, due_one, EscalationLevel.MANAGER_WEEKLY)),
        str(overdue_id(action_two, due_two, EscalationLevel.COORDINATOR)),
    }
    _due_now(stack)
    second = _scheduler(stack, stack.evaluator_like(), "worker-b")
    held: Any = stack.evaluator_like(_ExpiresWhileHolding)
    held.stack, held.organization_id, held.second, held.started = (
        stack,
        organization.organization_id,
        second,
        [],
    )
    first = _scheduler(stack, held, "worker-a")

    async def both() -> tuple[Any, Any]:
        async with asyncio.timeout(4 * WAIT_SECONDS):
            report_a = await first.run_due(EVALUATE_DEADLINES_TASK)
            assert held.started, "la segunda ejecución no llegó a arrancar"
            report_b = await held.started[0]
            return report_a, report_b

    report_a, report_b = stack.run(both())
    assert report_a is not None and report_a.lease_lost
    assert report_b is not None and report_b.outcome is TaskOutcome.SUCCEEDED
    keys = [c["overdue_id"] for c in stack.organization_overdue(organization)]
    assert len(keys) == len(set(keys)) == 3
    assert [len(stack.overdue_records(s)) for s in (one, two)] == [2, 1]
    # Una tercera ejecución, a la hora siguiente, no escribe nada (mismos registros que una).
    stack.tick(3_601)
    third = _scheduler(stack, stack.evaluator_like(), "worker-c")
    report_c = stack.run(third.run_due(EVALUATE_DEADLINES_TASK))
    assert report_c is not None and report_c.outcome is TaskOutcome.SUCCEEDED
    after = [c["overdue_id"] for c in stack.organization_overdue(organization)]
    assert after == keys
    assert set(keys) == expected
    return {"first_lease_lost": report_a.lease_lost, "keys": len(keys), "third_run_new": 0}


@pytest.mark.parametrize("nightly", PROFILES)
def test_evaluate_deadlines_started_twice_writes_one_record_per_key(
    postgres_endpoint: PostgresEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "FS-LAZ-10a",
            title="evaluate_deadlines arranca dos veces",
            injection="Arrendamiento de 60 s vencido en el reloj inyectado con la tarea viva",
            expected="Mismos registros que una ejecución; UUID v5 (action_id, due_at, level)",
        ) as run,
        deadlines_stack(postgres_endpoint, "fs_laz_10a") as stack,
    ):
        rounds = [
            _deadlines_round(stack, stack.actions.world.organization())
            for _ in range(3 if nightly else 1)
        ]
        run.observe(rounds=rounds)


# --- monthly_package ------------------------------------------------------------------------------


@contextlib.contextmanager
def _monthly_world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    """El mundo de ``test_monthly_package_task`` (expediente, firmante y S3 reales)."""
    with loop_world(postgres_endpoint, "fs_laz_10b") as loop:
        signing = loop.stack.run(bootstrapped_world())
        sessions = loop.stack.authz.sessions
        store = SqlCheckpointStore(
            database=loop.stack.primary.database,
            writer=loop.stack.primary.writer,
            audit=sessions.audit,
            outbox=loop.stack.outbox,
        )
        checkpoints = CheckpointService(
            store=store,
            signer=signing.service,
            clock=signing.clock,
            writer_context=loop.stack.authz.contexts.checkpoint_writer,
        )
        with versioned_bucket(localstack_endpoint.aws_client("s3"), "fs-laz-10b") as bucket:
            yield World(
                loop=loop,
                checkpoints=checkpoints,
                config=PostgresLoopConfigurationRepository(loop.stack.primary.database),
                repository=MonthlyPackageRepository(database=loop.stack.primary.database),
                evidence=S3Storage(localstack_endpoint.storage_settings(bucket), sessions.clock),
                signer=signing.service,
            )


class _HeldComposer(MonthlyPackageComposer):
    """Antes de comprobar su arrendamiento para subir, avisa y espera."""

    holding: asyncio.Event
    resume: asyncio.Event

    async def _lease_valid(self, context: Any, zone: Any, period: str) -> bool:
        self.holding.set()
        async with asyncio.timeout(WAIT_SECONDS):
            await self.resume.wait()
        return await super()._lease_valid(context, zone, period)


def _composer(world: World, owner: str, cls: type[MonthlyPackageComposer]) -> Any:
    sessions = world.loop.stack.authz.sessions
    return cls(
        database=world.database,
        reader=LectorExpediente(database=world.database, audit=sessions.audit, clock=world.clock),
        repository=world.repository,
        evidence=world.evidence,
        checkpoints=world.checkpoints,
        writer=world.loop.stack.primary.writer,
        clock=world.clock,
        signer=world.signer,
        owner=owner,
    )


def _task(world: World, composer: MonthlyPackageComposer) -> MonthlyPackageTask:
    return MonthlyPackageTask(
        config=world.config,
        repository=world.repository,
        composer=composer,
        clock=world.clock,
        max_catchup_months=1,
    )


def _monthly_round(world: World) -> dict[str, Any]:
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    world.grant_coordinator(organization)
    zone = world.zone(organization)
    period = "2026-09"
    period_from, period_to = month_bounds(BOGOTA, period)
    world.clock.set(period_from + timedelta(days=5))
    world.classify(world.loop.ingest_finding(organization), outcome="confirmed")
    world.clock.set(period_to + timedelta(days=5))
    context = world.loop.event_context(organization)
    held: Any = _composer(world, "worker-a", _HeldComposer)
    held.holding, held.resume = asyncio.Event(), asyncio.Event()
    first = _task(world, held)
    second = _task(world, _composer(world, "worker-b", MonthlyPackageComposer))

    async def once(task: MonthlyPackageTask) -> None:
        async with world.database.transaction(context) as transaction:
            await task.run(transaction)

    async def both() -> None:
        async with asyncio.timeout(4 * WAIT_SECONDS):
            alive = asyncio.create_task(once(first))
            try:
                await held.holding.wait()
                world.clock.advance(11 * 60)  # el arrendamiento de 10 min vence con la tarea viva
                await once(second)
            finally:
                held.resume.set()
                await alive

    world.loop.stack.run(both())
    rows = world.sealed_rows(zone.zone_id)
    sealed = [row for row in rows if row["state"] == "sealed"]
    assert len(sealed) == 1, rows
    assert world.produced_periods(zone.zone_id) == [period]
    key = object_key(
        organization_id=zone.organization_id,
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        period=period,
    )
    stored = world.loop.stack.run(world.evidence.get_object(key))
    (produced,) = world.loop.stack.fetch(
        "SELECT ledger.vigia_bytes_to_jsonb(content) ->> 'package_sha256' AS sha256"
        " FROM ledger.ledger_record WHERE record_type = 'monthly_package_produced'"
        " AND scope_zone_id = $1",
        zone.zone_id,
    )
    # El objeto del depósito es el que declara el registro sellado (nunca el de la primera).
    assert hashlib.sha256(stored).hexdigest() == produced["sha256"]
    # Un paquete sellado no se vuelve a sellar: otra pasada no escribe nada.
    world.clock.advance(DAY.total_seconds())
    world.loop.stack.run(once(_task(world, _composer(world, "worker-c", MonthlyPackageComposer))))
    assert world.sealed_rows(zone.zone_id) == rows
    assert world.produced_periods(zone.zone_id) == [period]
    return {"sealed": len(sealed), "produced": 1, "object_matches_seal": True}


@pytest.mark.parametrize("nightly", PROFILES)
def test_monthly_package_started_twice_seals_once(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint, nightly: bool
) -> None:
    with (
        scenario(
            "FS-LAZ-10b",
            title="monthly_package arranca dos veces",
            injection="Arrendamiento de la composición (10 min) vencido con la tarea viva",
            expected="Un solo sellado y un solo registro; el objeto es el del sellado",
        ) as run,
        _monthly_world(postgres_endpoint, localstack_endpoint) as world,
    ):
        rounds = [_monthly_round(world) for _ in range(3 if nightly else 1)]
        run.observe(rounds=rounds)
