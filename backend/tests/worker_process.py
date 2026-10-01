"""Un ``vigia-worker`` real para las pruebas de proceso (TASK-130, FS-NUC-08).

``python -m tests.worker_process`` corre ``shared.worker.main.serve`` (señales, arranque
supervisado, ``/health/live``, bucles de despacho, planificador y parada ordenada) con una base
PostgreSQL de verdad como ``vigia_app`` y dobles en lo que la prueba no ejercita (firma, KMS y
almacén: sus comprobaciones de arranque pasan). Arrendamiento corto para no esperar 60 s.

La tarea ``worker_probe`` anota en ``VIGIA_TEST_WORKER_LOG`` (una línea JSON por suceso, con el
proceso, la organización y la hora) el comienzo y el final de cada organización, y entre medias
espera ``VIGIA_TEST_WORKER_ORG_SECONDS`` dentro de la transacción de la organización: así la
prueba puede matar al proceso en mitad de una. El efecto en la base es el evento
``worker_probe_effect`` publicado en esa transacción.

Variables: ``VIGIA_TEST_DATABASE_URL`` (``postgresql+asyncpg://`` de ``vigia_app``),
``VIGIA_TEST_PROVIDER_ORGANIZATION``, ``VIGIA_TEST_WORKER_LOG``, ``VIGIA_TEST_WORKER_ORG_SECONDS``,
``VIGIA_TEST_LEASE_SECONDS``, ``VIGIA_TEST_RENEW_SECONDS`` y ``VIGIA_WORKER_HEALTH_PORT``.
Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from tests.worker_support import (
    ProbeTask,
    StubKms,
    StubSigning,
    StubStorage,
    synchronize,
    system_contexts,
    worker_catalog,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode, Transaction
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import Schedule
from vigia_platform.shared.worker.main import WorkerConfig, WorkerRuntime, serve

_CLOCK = SystemClock()


@dataclass
class LoggedProbe(ProbeTask):
    """``ProbeTask`` que anota comienzo y final y se demora dentro de la transacción."""

    owner: str = ""
    log_path: str = ""
    seconds: float = 0.0

    def _note(self, event: str, organization_id: uuid.UUID) -> None:
        line = json.dumps(
            {
                "event": event,
                "owner": self.owner,
                "organization_id": str(organization_id),
                "at": _CLOCK.now().isoformat(),
            }
        )
        with open(self.log_path, "a", encoding="utf-8") as log:
            log.write(line + "\n")
            log.flush()

    async def __call__(self, transaction: Transaction) -> None:
        organization_id = transaction.context.organization_id
        self._note("start", organization_id)
        await asyncio.sleep(self.seconds)
        await super().__call__(transaction)
        self._note("end", organization_id)


def _config(environ: Mapping[str, str]) -> WorkerConfig:
    lease = float(environ["VIGIA_TEST_LEASE_SECONDS"])
    renew = float(environ["VIGIA_TEST_RENEW_SECONDS"])
    return WorkerConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        health_port=int(environ["VIGIA_WORKER_HEALTH_PORT"]),
        startup_deadline_seconds=30.0,
        startup_retry_seconds=0.5,
        shutdown_grace_seconds=10.0,
        lease_seconds=lease,
        renew_seconds=renew,
        lease_margin_seconds=renew / 2,
        scheduler_poll_seconds=0.2,
        monitor_seconds=1.0,
    )


async def build_runtime(config: WorkerConfig, environ: Mapping[str, str]) -> WorkerRuntime:
    database = Database.create(
        DatabaseSettings(
            url=environ["VIGIA_TEST_DATABASE_URL"],
            process=ProcessKind.WORKER,
            sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS
            worker_pool_size=4,
        )
    )
    contexts = system_contexts(_CLOCK, uuid.UUID(environ["VIGIA_TEST_PROVIDER_ORGANIZATION"]))
    runtime_owner = f"proceso-{os.getpid()}"
    probe = LoggedProbe(
        owner=runtime_owner,
        log_path=environ["VIGIA_TEST_WORKER_LOG"],
        seconds=float(environ["VIGIA_TEST_WORKER_ORG_SECONDS"]),
    )
    catalog = worker_catalog(probe, Schedule.every(3600))
    register_alerts_consumer(catalog.consumers)
    outbox = Outbox(catalog, _CLOCK)
    probe.outbox = outbox

    async def synchronize_catalog() -> None:
        await synchronize(database, catalog, _CLOCK)

    return WorkerRuntime(
        clock=_CLOCK,
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=StubKms(),
        catalog=catalog,
        dispatcher=Dispatcher(
            database=database, catalog=catalog, outbox=outbox, contexts=contexts, clock=_CLOCK
        ),
        contexts=contexts,
        registries=(synchronize_catalog,),
        owner=runtime_owner,
    )


def run(environ: Mapping[str, str]) -> int:
    configure_logging()
    config = _config(environ)

    async def builder(built: WorkerConfig) -> WorkerRuntime:
        return await build_runtime(built, environ)

    return asyncio.run(serve(config, builder))


if __name__ == "__main__":
    sys.exit(run(os.environ))
