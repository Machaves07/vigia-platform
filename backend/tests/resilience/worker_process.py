"""``vigia-worker`` real del arnés de resiliencia (LC-NUC-34; FS-NUC-08; NFR-NUC-06).

``python -m tests.resilience.worker_process``: lo mismo que ``tests/worker_process.py``
(``shared.worker.main.serve`` con señales, arranque supervisado, ``/health/live``, bucles de
despacho, planificador con arrendamiento y parada ordenada; base PostgreSQL de verdad como
``vigia_app``; dobles en firma, KMS y almacén) con el catálogo del arnés
(``processes.resilience_catalog``): además de la tarea ``worker_probe``, el consumidor
``resilience_effect`` con su efecto externo idempotente y el gancho de terminación que solo
activa ``VIGIA_TEST_KILL_AFTER_EFFECT``.

Variables: las de ``tests/worker_process.py`` y, para el consumidor, ``VIGIA_TEST_EFFECT_LOG``,
``VIGIA_TEST_DELIVERY_LOG``, ``VIGIA_TEST_KILL_AFTER_EFFECT`` y ``VIGIA_TEST_KILL_MARK``.
Solo datos generados.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Mapping

from tests.resilience.processes import EffectHandler, resilience_catalog
from tests.worker_process import LoggedProbe, _config
from tests.worker_support import StubKms, StubSigning, StubStorage, synchronize, system_contexts
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import Schedule
from vigia_platform.shared.worker.main import WorkerConfig, WorkerRuntime, serve

_CLOCK = SystemClock()


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
    owner = f"proceso-{os.getpid()}"
    probe = LoggedProbe(
        owner=owner,
        log_path=environ["VIGIA_TEST_WORKER_LOG"],
        seconds=float(environ["VIGIA_TEST_WORKER_ORG_SECONDS"]),
    )
    catalog = resilience_catalog(probe, EffectHandler(owner, environ), Schedule.every(3600))
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
            database=database,
            catalog=catalog,
            outbox=outbox,
            contexts=contexts,
            clock=_CLOCK,
            idle_seconds=0.2,
        ),
        contexts=contexts,
        registries=(synchronize_catalog,),
        owner=owner,
    )


def run(environ: Mapping[str, str]) -> int:
    configure_logging()
    config = _config(environ)

    async def builder(built: WorkerConfig) -> WorkerRuntime:
        return await build_runtime(built, environ)

    return asyncio.run(serve(config, builder))


if __name__ == "__main__":
    sys.exit(run(os.environ))
