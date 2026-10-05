"""Modo ``global`` del planificador de ``vigia-worker`` (TASK-220; D-7, extensión de TASK-130).

Contra PostgreSQL 16 real, como ``vigia_app``, con la tarea ``per_organization`` de U-02
(``worker_probe``, ``tests/worker_support.py``) y una tarea ``global`` de prueba
(``worker_global_probe``) en el **mismo** registro:

- la tarea ``global`` corre **una vez** por ejecución, sin cursor: sus lecturas son transacciones
  de **solo lectura** con el contexto de iteración de cada organización activa (la RLS ve solo esa
  organización) y ``control`` es el actor del sistema en la proveedora;
- la ``per_organization`` sigue recorriendo las organizaciones con su cursor;
- dos workers que toman el ciclo a la vez: lo ejecuta uno (arrendamiento);
- un fallo o el tope del manejador quedan registrados (``partial_failure``, sin
  ``last_success_at``, registro de error con el código y métrica ``failed`` por tarea sin
  organización); una parada antes de empezar no avanza ``next_run_at``; un arrendamiento perdido
  no se libera.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text

from tests.dispatch_support import metrics_with_reader
from tests.integration.conftest import PostgresEndpoint
from tests.revocation_list_support import histogram_points
from tests.worker_support import (
    PROBE_TASK,
    ProbeDefect,
    ProbeTask,
    WorkerEnvironment,
    synchronize,
    worker_catalog,
    worker_environment,
)
from vigia_platform.shared.context import ActorKind, ActorUnit, ContextOrigin, ScopeContext
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import (
    GlobalTaskScope,
    OutboxCatalog,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.worker.leases import SqlLeaseStore, TaskOutcome
from vigia_platform.shared.worker.scheduler import OrganizationOutcome, PeriodicScheduler

pytestmark = pytest.mark.integration

GLOBAL_TASK = "worker_global_probe"
WAIT_SECONDS = 30.0
"""Espera máxima de un suceso en la prueba concurrente (no es el asunto de la prueba)."""


@dataclass
class GlobalProbe:
    """Manejador ``global`` guionizado: anota lecturas y contextos y falla o espera a voluntad."""

    invocations: int = 0
    reads: list[tuple[uuid.UUID, ScopeContext, int]] = field(default_factory=list)
    controls: list[ScopeContext] = field(default_factory=list)
    write_errors: list[str] = field(default_factory=list)
    fail: bool = False
    hang: bool = False
    steal_lease: Any = None
    entered: asyncio.Event | None = None
    go: asyncio.Event | None = None

    async def __call__(self, run: GlobalTaskScope) -> None:
        self.invocations += 1
        for organization_id in await run.organizations():
            async with run.read(organization_id) as transaction:
                visible = (
                    await transaction.execute(
                        text("SELECT count(*) AS n FROM identity.organization")
                    )
                ).one()
                self.reads.append((organization_id, transaction.context, int(visible.n)))
        try:
            async with run.read(organization_id) as transaction:
                await transaction.execute(
                    text("UPDATE shared.periodic_task SET schedule = schedule WHERE false")
                )
        except Exception as error:
            self.write_errors.append(str(getattr(error, "orig", error)))
        async with run.control() as transaction:
            self.controls.append(transaction.context)
        if self.steal_lease is not None:
            await self.steal_lease()
        await run.ensure_lease()
        if self.entered is not None and self.go is not None:
            self.entered.set()
            await asyncio.wait_for(self.go.wait(), timeout=WAIT_SECONDS)
        if self.fail:
            raise ProbeDefect("fallo inyectado")
        if self.hang:
            await asyncio.Event().wait()


def _catalog(probe: ProbeTask, global_probe: GlobalProbe) -> OutboxCatalog:
    catalog = worker_catalog(probe)
    catalog.periodic_tasks.register(
        GLOBAL_TASK,
        Schedule.every(60),
        global_probe,
        unit=ActorUnit.U03,
        iteration=TaskIteration.GLOBAL,
    )
    return catalog


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "worker_global") as env:
        yield env


class Setup:
    def __init__(self, env: WorkerEnvironment, organizations: int = 3) -> None:
        self.env = env
        self.probe = ProbeTask()
        self.global_probe = GlobalProbe()
        self.catalog = _catalog(self.probe, self.global_probe)
        env.run(synchronize(env.database, self.catalog, env.clock))
        self.probe.outbox = Outbox(self.catalog, env.clock)
        env.run(env.suspend_all_organizations())
        self.organizations = env.run(env.add_organizations(organizations))
        env.run(env.put_task(PROBE_TASK, env.clock.now() + dt.timedelta(hours=1)))
        env.run(env.put_task(GLOBAL_TASK, env.clock.now()))
        self.metrics, self.reader = metrics_with_reader()

    def scheduler(
        self, owner: str, *, database: Any = None, handler_timeout_seconds: float = 300.0
    ) -> PeriodicScheduler:
        database = database or self.env.database
        return PeriodicScheduler(
            database=database,
            registry=self.catalog.periodic_tasks,
            leases=SqlLeaseStore(database=database, contexts=self.env.contexts),
            contexts=self.env.contexts,
            clock=self.env.clock,
            owner=owner,
            metrics=self.metrics,
            handler_timeout_seconds=handler_timeout_seconds,
        )

    def durations(self) -> list[dict[str, Any]]:
        return [
            attributes
            for attributes in histogram_points(self.reader, MetricName.PERIODIC_TASK_DURATION_MS)
            if attributes.get("task") == GLOBAL_TASK
        ]


def test_a_global_task_runs_once_with_read_only_reads_per_organization(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env)
    (report,) = env.run(setup.scheduler("worker-a").run_pending())

    probe = setup.global_probe
    assert (report.task_name, probe.invocations, report.results) == (GLOBAL_TASK, 1, [])
    assert (report.global_outcome, report.outcome) == (
        OrganizationOutcome.SUCCEEDED,
        TaskOutcome.SUCCEEDED,
    )
    assert [organization for organization, _, _ in probe.reads] == setup.organizations
    for organization, context, visible in probe.reads:
        assert context.organization_id == organization
        assert (context.actor.kind, context.origin) == (
            ActorKind.SYSTEM,
            ContextOrigin.PERIODIC_ITERATION,
        )
        assert visible == 1  # la RLS de la organización: ninguna otra es visible
    assert len(probe.write_errors) == 1 and "read-only transaction" in probe.write_errors[0]
    (control,) = probe.controls
    assert (control.organization_id, control.actor.kind) == (
        env.provider_organization_id,
        ActorKind.SYSTEM,
    )
    assert setup.probe.invocations == []  # la per_organization no vencía
    row = env.run(env.task_row(GLOBAL_TASK))
    assert (row.lease_owner, row.progress_organization_id, row.progress_failures) == (None, None, 0)
    assert row.last_outcome == "succeeded" and row.last_success_at == env.clock.now()
    assert row.next_run_at > env.clock.now()
    assert setup.durations() == [{"task": GLOBAL_TASK, "result": "succeeded"}]


def test_per_organization_tasks_keep_their_cursor_next_to_a_global_task(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env)
    env.run(env.put_task(PROBE_TASK, env.clock.now()))
    reports = env.run(setup.scheduler("worker-a").run_pending())
    by_task = {report.task_name: report for report in reports}
    assert set(by_task) == {PROBE_TASK, GLOBAL_TASK}
    per_organization = by_task[PROBE_TASK]
    assert [result.organization_id for result in per_organization.results] == setup.organizations
    assert per_organization.global_outcome is None
    assert [i.organization_id for i in setup.probe.invocations] == setup.organizations
    assert setup.global_probe.invocations == 1
    assert env.run(env.task_row(PROBE_TASK)).progress_organization_id == setup.organizations[-1]
    assert env.run(env.task_row(GLOBAL_TASK)).progress_organization_id is None


def test_two_workers_taking_the_global_cycle_at_once_run_it_once(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env)
    probe = setup.global_probe
    probe.entered, probe.go = asyncio.Event(), asyncio.Event()
    first = setup.scheduler("worker-a")
    second = setup.scheduler("worker-b", database=env.new_database())

    async def both() -> tuple[Any, Any]:
        running = asyncio.create_task(first.run_due(GLOBAL_TASK))
        assert probe.entered is not None and probe.go is not None
        await asyncio.wait_for(probe.entered.wait(), timeout=WAIT_SECONDS)
        other = await asyncio.wait_for(second.run_due(GLOBAL_TASK), timeout=WAIT_SECONDS)
        probe.go.set()
        return await running, other

    report, other = env.run(both())
    assert other is None
    assert report.outcome is TaskOutcome.SUCCEEDED
    assert probe.invocations == 1


def test_a_failing_global_handler_is_recorded_without_last_success(
    environment: WorkerEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    env = environment
    setup = Setup(env)
    setup.global_probe.fail = True
    with caplog.at_level(logging.ERROR):
        (report,) = env.run(setup.scheduler("worker-a").run_pending())
    assert (report.global_outcome, report.global_error_code, report.outcome) == (
        OrganizationOutcome.FAILED,
        "probe_defect",
        TaskOutcome.PARTIAL_FAILURE,
    )
    row = env.run(env.task_row(GLOBAL_TASK))
    assert (row.last_outcome, row.last_success_at, row.lease_owner) == (
        "partial_failure",
        None,
        None,
    )
    assert row.next_run_at > env.clock.now()
    failures = [r for r in caplog.records if "tarea periódica global fallida" in r.getMessage()]
    assert [getattr(r, "vigia_fields", None) for r in failures] == [
        {"task": GLOBAL_TASK, "code": "probe_defect"}
    ]
    assert setup.durations() == [{"task": GLOBAL_TASK, "result": "failed"}]


def test_a_global_handler_over_its_limit_counts_as_handler_timeout(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env)
    setup.global_probe.hang = True
    # La prueba trata del tope: uno corto, solo aquí (en producción, 300 s).
    (report,) = env.run(setup.scheduler("worker-a", handler_timeout_seconds=2.0).run_pending())
    assert (report.global_error_code, report.outcome) == (
        "handler_timeout",
        TaskOutcome.PARTIAL_FAILURE,
    )


def test_a_stop_before_starting_releases_without_advancing(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env)
    stop = asyncio.Event()
    stop.set()
    due = env.run(env.task_row(GLOBAL_TASK)).next_run_at
    report = env.run(setup.scheduler("worker-a").run_due(GLOBAL_TASK, stop))
    assert report.outcome is TaskOutcome.INTERRUPTED
    assert setup.global_probe.invocations == 0
    row = env.run(env.task_row(GLOBAL_TASK))
    assert (row.next_run_at, row.lease_owner) == (due, None)


def test_a_lost_lease_stops_before_the_external_effect(environment: WorkerEnvironment) -> None:
    env = environment
    setup = Setup(env)

    async def steal() -> None:
        await env.execute(
            "UPDATE shared.periodic_task SET lease_owner = 'otro-proceso' WHERE task_name = $1",
            GLOBAL_TASK,
        )

    setup.global_probe.steal_lease = steal
    setup.global_probe.fail = True  # no debe llegar: ensure_lease corta antes
    report = env.run(setup.scheduler("worker-a").run_due(GLOBAL_TASK))
    assert report.lease_lost and report.outcome is None
    assert report.global_outcome is None
    assert env.run(env.task_row(GLOBAL_TASK)).lease_owner == "otro-proceso"
