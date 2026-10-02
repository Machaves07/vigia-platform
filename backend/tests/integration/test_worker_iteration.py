"""Iteración por organización, retoma tras caída y dos procesos de trabajo (TASK-130).

Contra PostgreSQL 16 real, como ``vigia_app``, con la tarea de prueba ``worker_probe``
(``tests/worker_support.py``), que por organización publica un evento en la transacción de esa
organización (el efecto en la base):

- **BR-NUC-81**: una tarea recorre las organizaciones **activas** una a una, cada una en su
  transacción con su contexto de iteración (actor del sistema, la organización); el fallo en una
  se registra (cursor con el fallo, registro de error, métrica ``failed``, ``partial_failure``)
  y no detiene a las demás; lo que el manejador hizo en la organización que falló se deshace.
- **FS-NUC-08 en proceso**: un proceso muere a mitad de una ejecución (``BaseException``, como
  una señal no capturable); otro no la toma antes de que venza el arrendamiento (ni a los 59 ni
  a los 60 s) y, al vencer, continúa por la organización siguiente a la última confirmada: cada
  organización con su efecto una sola vez.
- **La valla**: un proceso que perdió el arrendamiento no confirma nada (su transacción se
  deshace) y no libera la tarea de otro.
- **Parada ordenada**: libera sin avanzar ``next_run_at``; otro proceso la toma enseguida y
  continúa.
- **FS-NUC-08 a nivel de proceso**: dos ``vigia-worker`` reales (``tests/worker_process.py``) con
  la misma tarea; ninguna organización corre en los dos a la vez; al matar con ``SIGKILL`` al que
  la tiene, el otro la retoma **después** de que venza su arrendamiento y la termina sin duplicar
  ningún efecto. Después, ``/health/live`` responde 200 en el que queda y ``SIGTERM`` lo para en
  orden: sale con 0 y no deja arrendamientos.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader
from sqlalchemy import text

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from tests.worker_support import (
    PROBE_TASK,
    ProbeTask,
    SimulatedCrash,
    WorkerEnvironment,
    synchronize,
    worker_catalog,
    worker_environment,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorKind, ContextOrigin
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.outbox.publish import NewEvent, Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.worker.leases import (
    LeaseLost,
    LeaseSettings,
    SqlLeaseStore,
    TaskOutcome,
)
from vigia_platform.shared.worker.main import OutboxAgeMonitor
from vigia_platform.shared.worker.scheduler import (
    DEFAULT_HANDLER_TIMEOUT_SECONDS,
    OrganizationOutcome,
    PeriodicScheduler,
    _Holder,
)

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "worker_iteration") as env:
        yield env


class Setup:
    """Una tarea ``worker_probe`` sincronizada, vencida ahora, y organizaciones nuevas."""

    def __init__(self, env: WorkerEnvironment, organizations: int) -> None:
        self.env = env
        self.probe = ProbeTask()
        self.catalog: OutboxCatalog = worker_catalog(self.probe)
        env.run(synchronize(env.database, self.catalog, env.clock))
        self.probe.outbox = Outbox(self.catalog, env.clock)
        env.run(env.suspend_all_organizations())
        self.organizations = env.run(env.add_organizations(organizations))
        self.since = env.clock.now()
        env.run(env.put_task(PROBE_TASK, env.clock.now()))
        self.metrics, self.reader = metrics_with_reader()

    def scheduler(
        self,
        owner: str,
        *,
        database: Any = None,
        settings: LeaseSettings | None = None,
        handler_timeout_seconds: float = DEFAULT_HANDLER_TIMEOUT_SECONDS,
    ) -> PeriodicScheduler:
        database = database or self.env.database
        return PeriodicScheduler(
            database=database,
            registry=self.catalog.periodic_tasks,
            leases=SqlLeaseStore(database=database, contexts=self.env.contexts, settings=settings),
            contexts=self.env.contexts,
            clock=self.env.clock,
            owner=owner,
            metrics=self.metrics,
            handler_timeout_seconds=handler_timeout_seconds,
        )

    def effects(self) -> list[uuid.UUID]:
        mine = set(self.organizations)
        return [org for org in self.env.run(self.env.effects()) if org in mine]


def _histogram(
    reader: InMemoryMetricReader, metrics: PlatformMetrics, name: MetricName
) -> list[dict[str, Any]]:
    data = reader.get_metrics_data()
    points: list[dict[str, Any]] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    points.extend(
                        dict(point.attributes or {})
                        for point in metric.data.data_points
                        if isinstance(point, HistogramDataPoint)
                    )
    return points


# --- BR-NUC-81 ---------------------------------------------------------------------------------


def test_a_failing_organization_is_recorded_and_the_others_continue(
    environment: WorkerEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    env = environment
    setup = Setup(env, 4)
    suspended = env.run(env.add_organizations(1, status="suspended"))[0]
    first, failing, *rest = setup.organizations
    setup.probe.fail_in.add(failing)
    scheduler = setup.scheduler("worker-a")

    with caplog.at_level(logging.ERROR):
        (report,) = env.run(scheduler.run_pending())

    # Una a una, en orden, cada una en su contexto; la suspendida no se recorre.
    assert [i.organization_id for i in setup.probe.invocations] == setup.organizations
    assert suspended not in [i.organization_id for i in setup.probe.invocations]
    for invocation in setup.probe.invocations:
        assert invocation.context_organization_id == invocation.organization_id
        assert invocation.actor_kind is ActorKind.SYSTEM
        assert invocation.origin is ContextOrigin.PERIODIC_ITERATION
    # El fallo no detiene a las demás y lo que hizo en esa organización se deshace.
    assert setup.effects() == [first, *rest]
    assert [(r.organization_id, r.outcome, r.error_code) for r in report.results] == [
        (first, OrganizationOutcome.SUCCEEDED, None),
        (failing, OrganizationOutcome.FAILED, "probe_defect"),
        *[(org, OrganizationOutcome.SUCCEEDED, None) for org in rest],
    ]
    assert report.outcome is TaskOutcome.PARTIAL_FAILURE
    # Registrado: en la fila de la tarea, en el registro de errores y en la métrica.
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner is None and row.lease_until is None
    assert row.last_outcome == "partial_failure"
    assert row.progress_failures == 1
    assert row.progress_organization_id == setup.organizations[-1]
    assert row.next_run_at > env.clock.now()
    assert row.last_success_at is None
    failures = [r for r in caplog.records if "fallida en una organización" in r.getMessage()]
    assert [getattr(r, "vigia_fields", None) for r in failures] == [
        {"task": PROBE_TASK, "organization_id": str(failing), "code": "probe_defect"}
    ]
    points = _histogram(setup.reader, setup.metrics, MetricName.PERIODIC_TASK_DURATION_MS)
    by_org = {p["organization_id"]: p["result"] for p in points if p["task"] == PROBE_TASK}
    assert by_org[str(failing)] == "failed"
    assert all(by_org[str(org)] == "succeeded" for org in (first, *rest))

    # La siguiente ejecución, sin fallos: ``succeeded`` y ``last_success_at``.
    setup.probe.reset()
    env.clock.advance(3600)
    (second,) = env.run(scheduler.run_pending())
    assert second.outcome is TaskOutcome.SUCCEEDED
    assert second.resumed_after is None
    row = env.run(env.task_row(PROBE_TASK))
    assert row.last_outcome == "succeeded" and row.progress_failures == 0
    assert row.last_success_at == env.clock.now()


def test_an_organization_that_breaks_the_transaction_rolls_back_only_itself(
    environment: WorkerEnvironment,
) -> None:
    """Un manejador que hace fallar una sentencia: el ``SAVEPOINT`` lo deshace y la tarea sigue."""
    env = environment
    setup = Setup(env, 3)
    broken = setup.organizations[1]

    async def divide_by_zero(transaction: Any) -> None:
        if transaction.context.organization_id == broken:
            await transaction.execute(text("SELECT 1 / 0"))

    setup.probe.after = divide_by_zero
    (report,) = env.run(setup.scheduler("worker-a").run_pending())
    assert [r.outcome for r in report.results] == [
        OrganizationOutcome.SUCCEEDED,
        OrganizationOutcome.FAILED,
        OrganizationOutcome.SUCCEEDED,
    ]
    assert setup.effects() == [setup.organizations[0], setup.organizations[2]]


# --- Funciones de la base: solo el actor del sistema y solo identificadores ---------------------


async def _as_app(env: WorkerEnvironment, actor_kind: str, sql: str, *args: Any) -> list[Any]:
    """``sql`` como ``vigia_app`` con una organización inexistente y ``actor_kind`` fijados."""
    connection = await env.migrated.connect("vigia_app")
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true),"
                " set_config('vigia.actor_kind', $2, true),"
                " set_config('vigia.periodic_iteration', 'on', true),"
                " set_config('vigia.outbox_dispatch', 'on', true)",
                str(uuid.uuid4()),
                actor_kind,
            )
            return list(await connection.fetch(sql, *args))
    finally:
        await connection.close()


_NOT_SYSTEM = ("user", "provider_user", "node", "operator", "")


def test_the_worker_functions_only_answer_the_system_actor(environment: WorkerEnvironment) -> None:
    env = environment
    env.run(env.suspend_all_organizations())
    active = env.run(env.add_organizations(2))
    env.run(env.add_organizations(1, status="suspended"))
    probe = ProbeTask()
    catalog = worker_catalog(probe)
    env.run(synchronize(env.database, catalog, env.clock))
    outbox = Outbox(catalog, env.clock)
    published_at = env.clock.now()

    async def publish() -> None:
        context = make_context(kind=ActorKind.USER, organization_id=active[0])
        async with env.database.transaction(context) as transaction:
            await outbox.publish(
                transaction,
                NewEvent(
                    event_name="security_alert",
                    payload={
                        "alert_kind": "context_absent_attempt",
                        "occurred_at": format_timestamp(published_at),
                    },
                ),
            )

    env.run(publish())

    organizations = "SELECT organization_id FROM shared.vigia_active_organizations()"
    oldest = "SELECT shared.vigia_outbox_oldest_pending('alert_metrics') AS oldest"
    heads = (
        "SELECT * FROM shared.vigia_outbox_due_heads('alert_metrics', now() + interval '1 day', 10)"
    )
    # El actor del sistema ve solo identificadores de las activas, en orden.
    rows = env.run(_as_app(env, "system", organizations))
    assert [row["organization_id"] for row in rows] == active
    assert env.run(_as_app(env, "system", oldest))[0]["oldest"] == published_at
    assert len(env.run(_as_app(env, "system", heads))) == 1
    # Cualquier otro actor, nada (aunque fije él mismo las variables de las políticas).
    for kind in _NOT_SYSTEM:
        assert env.run(_as_app(env, kind, organizations)) == [], kind
        assert env.run(_as_app(env, kind, oldest))[0]["oldest"] is None, kind
        assert env.run(_as_app(env, kind, heads)) == [], kind
    direct = env.run(_as_app(env, "system", "SELECT count(*) AS n FROM identity.organization"))
    assert direct[0]["n"] == 0

    # La métrica de antigüedad del evento pendiente más viejo, por consumidor.
    metrics, reader = metrics_with_reader()
    monitor = OutboxAgeMonitor(
        database=env.database,
        catalog=catalog,
        contexts=env.contexts,
        clock=env.clock,
        metrics=metrics,
    )
    catalog.seal()
    env.clock.advance(42)
    assert env.run(monitor.report_once()) == {"alert_metrics": 42.0}
    points = metric_points(reader, MetricName.OUTBOX_OLDEST_PENDING_AGE_SECONDS)
    assert points == [({"consumer": "alert_metrics"}, 42.0)]


# --- FS-NUC-08 en proceso --------------------------------------------------------------------


def test_a_crashed_run_is_resumed_after_the_lease_expires_without_duplicates(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env, 4)
    organizations = setup.organizations
    setup.probe.crash_in.add(organizations[2])
    started = env.clock.now()

    with pytest.raises(SimulatedCrash):
        env.run(setup.scheduler("worker-a").run_due(PROBE_TASK))
    assert setup.effects() == organizations[:2]
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner == "worker-a"
    assert row.progress_organization_id == organizations[1]

    setup.probe.reset()
    other = setup.scheduler("worker-b", database=env.new_database())
    # Ni a los 59 s ni a los 60 s (``lease_until < ahora`` estricto): sigue siendo de A.
    for seconds in (59, 1):
        env.clock.advance(seconds)
        assert env.run(other.run_due(PROBE_TASK)) is None
    assert env.clock.now() == started + timedelta(seconds=60)
    env.clock.advance(0.001)
    report = env.run(other.run_due(PROBE_TASK))
    assert report is not None
    assert report.resumed_after == organizations[1]
    assert [i.organization_id for i in setup.probe.invocations] == organizations[2:]
    assert report.outcome is TaskOutcome.SUCCEEDED
    # Cada organización con su efecto una sola vez.
    assert sorted(setup.effects()) == sorted(organizations)
    assert len(setup.effects()) == len(set(setup.effects()))
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner is None and row.last_outcome == "succeeded"


def test_a_worker_that_lost_its_lease_commits_nothing(environment: WorkerEnvironment) -> None:
    """La valla: A se detiene más de 60 s en la primera organización y B toma la tarea."""
    env = environment
    setup = Setup(env, 2)
    other_leases = SqlLeaseStore(database=env.new_database(), contexts=env.contexts)
    taken: list[Any] = []

    async def stall(_: Any) -> None:
        if not taken:
            env.clock.advance(61)
            taken.append(await other_leases.acquire(PROBE_TASK, "worker-b", env.clock.now()))

    setup.probe.after = stall
    report = env.run(setup.scheduler("worker-a").run_due(PROBE_TASK))

    assert taken and taken[0] is not None
    assert report is not None and report.lease_lost
    assert report.results == [] and report.outcome is None
    # Lo que A publicó en la organización se deshizo; la tarea sigue siendo de B, sin avance.
    assert setup.effects() == []
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner == "worker-b"
    assert row.progress_organization_id is None


def test_a_lagging_clock_does_not_let_a_worker_run_a_task_another_worker_owns(
    environment: WorkerEnvironment,
) -> None:
    """Reloj de A 61 s por detrás del de B: A cree vigente su arrendamiento, B ya lo tomó.

    La comprobación de la base al abrir cada organización (``holds``) impide que A invoque
    siquiera al manejador; la valla de ``advance`` solo deshacería lo que hiciera.
    """
    env = environment
    setup = Setup(env, 2)
    lagging = setup.scheduler("worker-a")
    leases = SqlLeaseStore(database=env.database, contexts=env.contexts)
    lease = env.run(leases.acquire(PROBE_TASK, "worker-a", env.clock.now()))
    assert lease is not None
    ahead = env.clock.now() + timedelta(seconds=61)
    assert env.run(leases.acquire(PROBE_TASK, "worker-b", ahead)) is not None

    holder = _Holder(lease)
    task = setup.catalog.periodic_tasks.get(PROBE_TASK)
    assert task is not None
    assert holder.usable(env.clock.now(), leases.settings)
    with pytest.raises(LeaseLost):
        env.run(lagging._run_for(task, holder, setup.organizations[0]))
    assert setup.probe.invocations == []
    assert setup.effects() == []


def test_no_organization_starts_when_the_lease_is_about_to_expire_unrenewed(
    environment: WorkerEnvironment,
) -> None:
    """Sin renovación confirmada, a 5 s del vencimiento no se empieza otra organización."""
    env = environment
    setup = Setup(env, 3)

    async def slow(_: Any) -> None:
        env.clock.advance(56)  # 60 s de vigencia - 5 s de margen < 56 s, sin renovar

    setup.probe.after = slow
    report = env.run(setup.scheduler("worker-a").run_due(PROBE_TASK))
    assert report is not None and report.outcome is TaskOutcome.INTERRUPTED
    assert [i.organization_id for i in setup.probe.invocations] == setup.organizations[:1]
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner is None and row.progress_organization_id == setup.organizations[0]


def test_a_graceful_stop_releases_without_advancing_and_another_worker_continues(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env, 3)
    stop = asyncio.Event()

    async def stopping(_: Any) -> None:
        stop.set()  # la parada llega mientras se procesa la primera organización

    setup.probe.after = stopping
    run_at = env.run(env.task_row(PROBE_TASK)).next_run_at
    report = env.run(setup.scheduler("worker-a").run_due(PROBE_TASK, stop))

    assert report is not None and report.outcome is TaskOutcome.INTERRUPTED
    assert [r.organization_id for r in report.results] == setup.organizations[:1]
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner is None and row.lease_until is None
    assert row.next_run_at == run_at
    assert row.last_outcome == "interrupted"

    setup.probe.after = None
    other = env.run(setup.scheduler("worker-b").run_due(PROBE_TASK))
    assert other is not None and other.resumed_after == setup.organizations[0]
    assert sorted(setup.effects()) == sorted(setup.organizations)


def test_concurrent_acquire_has_a_single_winner(environment: WorkerEnvironment) -> None:
    """``acquire`` es una sola sentencia condicional: ocho pools a la vez, un ganador.

    Las demás pruebas son secuenciales; esta falla si ``acquire`` comprueba y después escribe
    (modelo de la revisión independiente de la sesión de control).
    """
    env = environment
    stores = [SqlLeaseStore(database=env.new_database(), contexts=env.contexts) for _ in range(8)]
    winners: list[int] = []
    for _ in range(15):
        env.run(env.put_task(PROBE_TASK, env.clock.now()))

        async def race() -> int:
            now = env.clock.now()
            results = await asyncio.gather(
                *(store.acquire(PROBE_TASK, f"w{i}", now) for i, store in enumerate(stores))
            )
            return sum(1 for result in results if result is not None)

        winners.append(env.run(race()))
        env.clock.advance(100)
    assert winners == [1] * 15


def test_a_run_longer_than_the_lease_is_renewed_and_no_one_else_takes_it(
    environment: WorkerEnvironment,
) -> None:
    """Cuatro organizaciones de 30 s cada una (120 s, el doble de la vigencia): la renovación en
    segundo plano mantiene la tarea; otro proceso lo intenta en cada organización y no puede."""
    env = environment
    setup = Setup(env, 4)
    rival = SqlLeaseStore(database=env.new_database(), contexts=env.contexts)
    attempts: list[Any] = []

    async def long_organization(_: Any) -> None:
        env.clock.advance(30)
        await asyncio.sleep(0.25)  # la renovación (cada 50 ms reales) corre mientras tanto
        attempts.append(await rival.acquire(PROBE_TASK, "worker-b", env.clock.now()))

    setup.probe.after = long_organization
    settings = LeaseSettings(
        duration=timedelta(seconds=60),
        renew_every=timedelta(milliseconds=50),
        safety_margin=timedelta(seconds=5),
    )
    report = env.run(setup.scheduler("worker-a", settings=settings).run_due(PROBE_TASK))

    assert attempts == [None] * 4
    assert report is not None and report.outcome is TaskOutcome.SUCCEEDED
    assert [i.organization_id for i in setup.probe.invocations] == setup.organizations
    assert sorted(setup.effects()) == sorted(setup.organizations)
    row = env.run(env.task_row(PROBE_TASK))
    assert row.lease_owner is None and row.last_outcome == "succeeded"


def test_a_handler_over_its_timeout_fails_that_organization_and_the_others_continue(
    environment: WorkerEnvironment,
) -> None:
    env = environment
    setup = Setup(env, 3)
    hanging = setup.organizations[1]

    async def hang(transaction: Any) -> None:
        if transaction.context.organization_id == hanging:
            await asyncio.sleep(30)

    setup.probe.after = hang
    scheduler = setup.scheduler("worker-a", handler_timeout_seconds=0.3)
    report = env.run(asyncio.wait_for(scheduler.run_due(PROBE_TASK), 20))

    assert report is not None and report.outcome is TaskOutcome.PARTIAL_FAILURE
    assert [(r.organization_id, r.outcome, r.error_code) for r in report.results] == [
        (setup.organizations[0], OrganizationOutcome.SUCCEEDED, None),
        (hanging, OrganizationOutcome.FAILED, "handler_timeout"),
        (setup.organizations[2], OrganizationOutcome.SUCCEEDED, None),
    ]
    # Lo que hizo antes de colgarse se deshizo; las demás confirmaron.
    assert setup.effects() == [setup.organizations[0], setup.organizations[2]]


# --- FS-NUC-08 a nivel de proceso --------------------------------------------------------------

LEASE_SECONDS = 3.0
RENEW_SECONDS = 1.0
ORG_SECONDS = 0.4
ORGANIZATIONS = 6


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            entry["at"] = datetime.fromisoformat(entry["at"])
            events.append(entry)
    return events


async def _wait_for(condition: Any, timeout: float, message: str) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await condition()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.05)


def test_two_worker_processes_never_overlap_and_a_killed_one_is_resumed(
    environment: WorkerEnvironment, tmp_path: Path
) -> None:
    env = environment
    env.run(env.suspend_all_organizations())
    organizations = env.run(env.add_organizations(ORGANIZATIONS))

    clock = SystemClock()
    env.run(env.put_task(PROBE_TASK, clock.now() - timedelta(seconds=1)))
    log = tmp_path / "worker.jsonl"
    ports = {name: _free_port() for name in ("a", "b")}
    processes: dict[str, subprocess.Popen[bytes]] = {}
    outputs = {}
    base = {
        **os.environ,
        "VIGIA_TEST_DATABASE_URL": env.migrated.as_role("vigia_app").sqlalchemy_url,
        "VIGIA_TEST_PROVIDER_ORGANIZATION": str(env.provider_organization_id),
        "VIGIA_TEST_WORKER_LOG": str(log),
        "VIGIA_TEST_WORKER_ORG_SECONDS": str(ORG_SECONDS),
        "VIGIA_TEST_LEASE_SECONDS": str(LEASE_SECONDS),
        "VIGIA_TEST_RENEW_SECONDS": str(RENEW_SECONDS),
    }
    try:
        for name, port in ports.items():
            outputs[name] = (tmp_path / f"{name}.out").open("wb")
            processes[name] = subprocess.Popen(
                [sys.executable, "-m", "tests.worker_process"],
                cwd=BACKEND,
                env={**base, "VIGIA_WORKER_HEALTH_PORT": str(port)},
                stdout=outputs[name],
                stderr=subprocess.STDOUT,
            )
        pids = {f"proceso-{process.pid}": name for name, process in processes.items()}

        async def holder_with_two_done() -> str | None:
            ends = [e for e in _events(log) if e["event"] == "end"]
            starts = [e for e in _events(log) if e["event"] == "start"]
            if len(ends) >= 2 and len(starts) > len(ends):
                return str(starts[-1]["owner"])
            return None

        holder = env.run(_wait_for(holder_with_two_done, 60, "ningún worker tomó la tarea"))
        killed = pids[holder]
        survivor = "b" if killed == "a" else "a"
        processes[killed].send_signal(signal.SIGKILL)
        processes[killed].wait(10)
        killed_at = clock.now()

        async def finished() -> bool:
            row = await env.task_row(PROBE_TASK)
            return row.last_outcome == "succeeded" and row.lease_owner is None

        env.run(_wait_for(finished, 60, "el otro worker no terminó la tarea"))
        events = _events(log)

        # Cada organización con su efecto una sola vez.
        effects = [org for org in env.run(env.effects()) if org in set(organizations)]
        assert sorted(effects) == sorted(organizations)
        assert len(effects) == len(organizations)
        # Mientras vivían los dos, solo uno trabajó; el otro empezó después de que venciera el
        # arrendamiento del muerto (renovado cada 1 s, vigente 3 s: al menos 2 s después).
        before = [e for e in events if e["at"] <= killed_at]
        assert {e["owner"] for e in before} == {holder}
        after = [e for e in events if e["owner"] != holder]
        assert after, "el superviviente no retomó la tarea"
        assert min(e["at"] for e in after) - killed_at >= timedelta(
            seconds=LEASE_SECONDS - RENEW_SECONDS - 0.5
        )
        # El superviviente continuó por la organización en curso al morir el otro: ninguna
        # anterior se repitió.
        last = [e for e in events if e["owner"] == holder][-1]
        resumed = [uuid.UUID(e["organization_id"]) for e in after if e["event"] == "start"]
        position = organizations.index(uuid.UUID(last["organization_id"]))
        # Si murió antes de confirmar la organización en curso, se repite esa; si la confirmó
        # (``end`` anotado justo antes del ``COMMIT``), se sigue por la siguiente.
        assert resumed in (organizations[position:], organizations[position + 1 :])

        # El que queda atiende /health/live y se para en orden con SIGTERM.
        live = httpx.get(f"http://127.0.0.1:{ports[survivor]}/health/live", timeout=5)
        assert live.status_code == 200 and live.json() == {"status": "live"}
        processes[survivor].send_signal(signal.SIGTERM)
        assert processes[survivor].wait(30) == 0
        row = env.run(env.task_row(PROBE_TASK))
        assert row.lease_owner is None
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.kill()
                process.wait(10)
        for output in outputs.values():
            output.close()
