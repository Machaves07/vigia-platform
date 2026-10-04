"""Hito «U-02 terminada»: tareas periódicas y consumidores registrados al arrancar (TASK-153).

``domain-entities.md`` §4.3 y su nota del 2026-09-20 fijan las **diez** tareas periódicas de
U-02 y su cadencia; ``shared.worker.main`` exige que un worker no arranque sin todos sus
consumidores registrados. Contra PostgreSQL 16 real, como ``vigia_app``:

- **Cadencias**: el catálogo que compone la raíz de producción (``shared.runtime.units``, A-52;
  VIG-137) con las unidades registradas contiene las diez tareas de U-02 del diseño con su
  cadencia (cada 5 min, cada 15 min, diaria a las 00:00 UTC, diaria, semanal o mensual); la única
  otra tarea de U-02 es ``restore_drill_age`` (TASK-132), de modo que una tarea nueva o retirada
  se nota aquí.
- **Arranque**: un ``WorkerProcess`` con ese catálogo supera el arranque supervisado, deja en
  ``shared.periodic_task`` una fila por tarea con su horario persistido y una próxima ejecución
  sobre la rejilla del horario, en ``shared.consumer`` sus dos consumidores, abre un bucle de
  despacho por consumidor registrado, responde ``/health/live`` y se para en orden con 0.
- **Fallo cerrado**: con la base ya sincronizada, un worker al que le falta un consumidor o una
  tarea registrados en la base no arranca (``STARTUP_FAILURE_EXIT_CODE``) y no abre ningún bucle.

El reloj es simulado: ninguna tarea vence durante la prueba (sus manejadores no corren) y el
plazo de arranque avanza con el reloj, no con la pared. Firma, KMS y almacén son dobles que
superan su comprobación (``tests/worker_support.py``). Solo datos generados.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, cast

import httpx
import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.worker_support import (
    StubKms,
    StubSigning,
    StubStorage,
    WorkerEnvironment,
    synchronize,
    worker_environment,
)
from vigia_platform.ledger.application.integrity_requests import (
    INTEGRITY_ON_DEMAND_CONSUMER,
)
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.alerts_consumer import (
    ALERTS_CONSUMER,
)
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog, Schedule, ScheduleKind
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.runtime.units import UnitServices, outbox_catalog, registered_units
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.worker.main import WorkerConfig, WorkerProcess, WorkerRuntime

pytestmark = pytest.mark.integration

_MINUTE: Final = 60

DESIGN_TASKS: Final[dict[str, tuple[ScheduleKind, int | None]]] = {
    # domain-entities.md §4.3: (tipo de horario, intervalo o desfase exigido; None = cualquiera)
    "expire_concessions": (ScheduleKind.EVERY, 5 * _MINUTE),
    "expire_sessions": (ScheduleKind.EVERY, 5 * _MINUTE),
    "write_checkpoints": (ScheduleKind.DAILY, 0),  # 00:00 UTC
    "verify_chains_incremental": (ScheduleKind.DAILY, None),
    "verify_chains_full": (ScheduleKind.MONTHLY, None),
    "key_rotation_reminder": (ScheduleKind.DAILY, None),
    "throttle_window_cleanup": (ScheduleKind.EVERY, 15 * _MINUTE),
    # Nota fechada del 2026-09-20 (NFR Design de U-02, respuesta 12)
    "create_partitions": (ScheduleKind.WEEKLY, None),
    "evidence_sample": (ScheduleKind.DAILY, None),
    "archive_audit_partitions": (ScheduleKind.MONTHLY, None),
}
"""Las diez tareas periódicas de U-02 del diseño y su cadencia."""

OTHER_U02_TASKS: Final[dict[str, tuple[ScheduleKind, int | None]]] = {
    "restore_drill_age": (ScheduleKind.DAILY, None),  # TASK-132: métrica de la alarma diaria
}
"""Tareas de U-02 que añadieron las tareas del plan, fuera de la lista del diseño."""

U02_CONSUMERS: Final = (ALERTS_CONSUMER, INTEGRITY_ON_DEMAND_CONSUMER)


class _NotInvoked:
    """Dependencia de un manejador que la prueba nunca ejecuta (ninguna tarea vence)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"el manejador no debía ejecutarse (usó «{name}»)")


def u02_catalog(environment: WorkerEnvironment) -> OutboxCatalog:
    """El catálogo de la raíz de producción: el que compone ``build_worker_runtime`` con las
    unidades del registro (``shared.runtime.units``). Ningún manejador se ejecuta: sus
    dependencias, salvo la base y los contextos que se consultan al construir, son
    ``_NotInvoked``."""
    unused: Any = _NotInvoked()
    services = UnitServices(
        clock=environment.clock,
        metrics=get_metrics(),
        provider_organization_id=environment.provider_organization_id,
        database=environment.database,
        contexts=environment.contexts,
        authorizer=unused,
        audit=unused,
        outbox=unused,
        writer=unused,
        signing=unused,
        checkpoints=unused,
        kms=unused,
    )
    return outbox_catalog(registered_units(), services)


def u02_tasks(catalog: OutboxCatalog) -> dict[str, Any]:
    """Las tareas de U-02 del catálogo (U-03 y U-04 añaden las suyas al mismo registro)."""
    return {
        task.task_name: task
        for task in catalog.periodic_tasks.tasks()
        if task.unit is ActorUnit.U02
    }


@dataclass
class RecordingDispatcher:
    """El despachador real, anotando para qué consumidores abre el worker un bucle."""

    inner: Dispatcher
    started: list[str] = field(default_factory=list)

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None:
        self.started.append(consumer_name)
        await self.inner.run(consumer_name, stop)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _worker(
    environment: WorkerEnvironment, catalog: OutboxCatalog
) -> tuple[WorkerProcess, RecordingDispatcher, int]:
    database = environment.new_database()  # el proceso lo cierra al terminar
    clock = environment.clock
    dispatcher = RecordingDispatcher(
        Dispatcher(
            database=database,
            catalog=catalog,
            outbox=Outbox(catalog, clock),
            contexts=environment.contexts,
            clock=clock,
        )
    )

    async def synchronize_catalog() -> None:
        await synchronize(database, catalog, clock)

    async def simulated_sleep(seconds: float) -> None:
        # El plazo de arranque corre con el reloj monótono simulado; la hora de pared no se
        # mueve, para que ninguna tarea venza en las pruebas que vienen después.
        wall = clock.now()
        clock.advance(seconds)
        clock.set(wall)
        await asyncio.sleep(0)

    port = _free_port()
    config = WorkerConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        health_port=port,
        startup_deadline_seconds=60.0,
        startup_retry_seconds=5.0,
        shutdown_grace_seconds=30.0,
        scheduler_poll_seconds=1.0,
        monitor_seconds=15.0,
    )
    runtime = WorkerRuntime(
        clock=clock,
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=cast(KmsPort, StubKms()),  # solo la comprobación ``data_key`` del arranque
        catalog=catalog,
        dispatcher=dispatcher,
        contexts=environment.contexts,
        registries=(synchronize_catalog,),
        owner=f"prueba-{uuid.uuid4().hex[:8]}",
        sleep=simulated_sleep,
    )
    return WorkerProcess(config, runtime), dispatcher, port


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WorkerEnvironment]:
    with worker_environment(postgres_endpoint, "registered_tasks") as env:
        yield env


def _check_cadence(name: str, schedule: Schedule, kind: ScheduleKind, offset: int | None) -> None:
    assert schedule.kind is kind, f"{name}: {schedule.kind} en lugar de {kind}"
    if offset is not None:
        assert schedule.offset_seconds == offset, f"{name}: {schedule.text}"


def test_the_ten_design_tasks_are_registered_with_their_cadence(
    environment: WorkerEnvironment,
) -> None:
    registered = u02_tasks(u02_catalog(environment))
    assert len(DESIGN_TASKS) == 10
    assert set(registered) == set(DESIGN_TASKS) | set(OTHER_U02_TASKS)
    for name, (kind, offset) in {**DESIGN_TASKS, **OTHER_U02_TASKS}.items():
        task = registered[name]
        assert task.unit is ActorUnit.U02
        _check_cadence(name, task.schedule, kind, offset)


@pytest.mark.parametrize(
    ("name", "schedule"),
    [
        ("expire_sessions", Schedule.every(10 * _MINUTE)),
        ("throttle_window_cleanup", Schedule.every(5 * _MINUTE)),
        ("write_checkpoints", Schedule.daily(hour=1)),
        ("write_checkpoints", Schedule.every(24 * 60 * _MINUTE)),
        ("verify_chains_full", Schedule.weekly()),
        ("create_partitions", Schedule.daily()),
    ],
)
def test_a_cadence_other_than_the_design_is_detected(name: str, schedule: Schedule) -> None:
    kind, offset = DESIGN_TASKS[name]
    with pytest.raises(AssertionError, match=name):
        _check_cadence(name, schedule, kind, offset)


async def _boot(process: WorkerProcess, stop: asyncio.Event) -> asyncio.Task[int]:
    """Lanza el proceso y espera a que arranque o termine (fallo de arranque)."""
    running = asyncio.create_task(process.run(stop))
    started = asyncio.create_task(process.started.wait())
    async with asyncio.timeout(120):
        await asyncio.wait({running, started}, return_when=asyncio.FIRST_COMPLETED)
    started.cancel()
    return running


def test_worker_boots_with_every_task_and_consumer_registered(
    environment: WorkerEnvironment,
) -> None:
    catalog = u02_catalog(environment)
    expected = {task.task_name: task.schedule for task in catalog.periodic_tasks.tasks()}
    process, dispatcher, port = _worker(environment, catalog)
    booted_at = environment.clock.now()

    async def scenario() -> tuple[int, list[Any], list[Any], int]:
        stop = asyncio.Event()
        running = await _boot(process, stop)
        assert not running.done(), f"el worker no arrancó: salió con {running.result()}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            live = await client.get(f"http://127.0.0.1:{port}/health/live")
        tasks = await environment.fetch(
            "SELECT task_name, unit, schedule, next_run_at, lease_owner"
            " FROM shared.periodic_task ORDER BY task_name"
        )
        consumers = await environment.fetch(
            "SELECT consumer_name, unit FROM shared.consumer ORDER BY consumer_name"
        )
        stop.set()
        async with asyncio.timeout(120):
            code = await running
        return code, tasks, consumers, live.status_code

    code, tasks, consumers, live = environment.run(scenario())

    assert catalog.sealed
    assert live == 200
    assert sorted(dispatcher.started) == sorted(U02_CONSUMERS)
    assert [(row["consumer_name"], row["unit"]) for row in consumers] == [
        (name, "U-02") for name in sorted(U02_CONSUMERS)
    ]
    assert [row["task_name"] for row in tasks] == sorted(expected)
    for row in tasks:
        schedule = expected[row["task_name"]]
        assert row["unit"] == "U-02"
        assert row["schedule"] == schedule.text
        assert row["lease_owner"] is None  # nada venció: nadie tomó la tarea
        next_run = row["next_run_at"]
        assert next_run > booted_at
        assert schedule.next_after(next_run - timedelta(microseconds=1)) == next_run
    assert {row["schedule"] for row in tasks if row["task_name"] in DESIGN_TASKS} >= {
        "every:300s",
        "every:900s",
        "daily:0s",
    }
    assert code == 0


def _without(full: OutboxCatalog, name: str) -> OutboxCatalog:
    """El catálogo de U-02 sin el consumidor o la tarea ``name``."""
    reduced = OutboxCatalog()
    register_u02_event_types(reduced.event_types)
    for consumer in full.consumers.consumers():
        if consumer.consumer_name != name:
            reduced.consumers.register(consumer)
    for task in full.periodic_tasks.tasks():
        if task.task_name != name:
            reduced.periodic_tasks.register(
                task.task_name, task.schedule, task.handler, unit=task.unit
            )
    return reduced


@pytest.mark.parametrize("missing", [INTEGRITY_ON_DEMAND_CONSUMER, ALERTS_CONSUMER, *DESIGN_TASKS])
def test_worker_missing_a_registration_does_not_start(
    environment: WorkerEnvironment, missing: str
) -> None:
    full = u02_catalog(environment)
    environment.run(synchronize(environment.database, full, environment.clock))
    reduced = _without(u02_catalog(environment), missing)
    process, dispatcher, _ = _worker(environment, reduced)

    async def scenario() -> int:
        stop = asyncio.Event()
        running = await _boot(process, stop)
        stop.set()  # si llegó a arrancar (el defecto), se para en orden y sale con 0
        async with asyncio.timeout(120):
            return await running

    code = environment.run(scenario())

    assert code == STARTUP_FAILURE_EXIT_CODE
    assert not reduced.sealed
    assert not process.started.is_set()
    assert dispatcher.started == []
