"""``vigia-worker`` real del arnés de resiliencia (LC-NUC-34; FS-NUC-08, FS-GOB-08; NFR-NUC-06).

``python -m tests.resilience.worker_process``: lo mismo que ``tests/worker_process.py``
(``shared.worker.main.serve`` con señales, arranque supervisado, ``/health/live``, bucles de
despacho, planificador con arrendamiento y parada ordenada; base PostgreSQL de verdad como
``vigia_app``; dobles en firma, KMS y almacén) con el catálogo del arnés
(``processes.resilience_catalog``): además de la tarea ``worker_probe``, el consumidor
``resilience_effect`` con su efecto externo idempotente y el gancho de terminación que solo
activa ``VIGIA_TEST_KILL_AFTER_EFFECT``.

**U-03 cargado** (TASK-232): los tipos de registro de todas las unidades (los sincroniza el
arranque) y los eventos y las **siete tareas de U-03 con sus manejadores reales**
(``fleet_tasks`` y ``catalog_tasks`` sobre ``gob_support.process_units``), como ``vigia-worker``.

**Gancho de tarea lenta** (FS-GOB-08), solo en este proceso de prueba y solo con
``VIGIA_TEST_SLOW_TASK``: el manejador real de esa tarea anota en ``VIGIA_TEST_TASK_LOG`` (una línea
JSON por suceso, con el proceso, la organización y la hora) el comienzo y el final de cada
organización y, entre medias, después del manejador real, espera ``VIGIA_TEST_TASK_ORG_SECONDS``
dentro de la transacción de la organización, con lo escrito aún sin confirmar: así la prueba
puede matar al proceso en mitad de una organización. El código de la plataforma no tiene ningún
gancho.

Variables: las de ``tests/worker_process.py``, para el consumidor ``VIGIA_TEST_EFFECT_LOG``,
``VIGIA_TEST_DELIVERY_LOG``, ``VIGIA_TEST_KILL_AFTER_EFFECT`` y ``VIGIA_TEST_KILL_MARK`` y, para el
gancho de tarea, ``VIGIA_TEST_SLOW_TASK``, ``VIGIA_TEST_TASK_LOG`` y
``VIGIA_TEST_TASK_ORG_SECONDS``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping
from typing import Any, cast

from tests.resilience.gob_support import TaskWrapper, add_u03, process_units
from tests.resilience.processes import EffectHandler, u02_catalog
from tests.worker_process import LoggedProbe, _config
from tests.worker_support import StubKms, StubSigning, StubStorage, synchronize, system_contexts
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode, Transaction
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import Schedule
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.worker.main import WorkerConfig, WorkerRuntime, serve

_CLOCK = SystemClock()
SLOW_TASK_VARIABLE = "VIGIA_TEST_SLOW_TASK"
TASK_LOG_VARIABLE = "VIGIA_TEST_TASK_LOG"
TASK_SECONDS_VARIABLE = "VIGIA_TEST_TASK_ORG_SECONDS"


def _slow_task(environ: Mapping[str, str], owner: str) -> TaskWrapper | None:
    """El gancho de tarea lenta de ``VIGIA_TEST_SLOW_TASK`` (solo en este proceso de prueba)."""
    chosen = environ.get(SLOW_TASK_VARIABLE)
    if not chosen:
        return None
    log_path = environ[TASK_LOG_VARIABLE]
    seconds = float(environ[TASK_SECONDS_VARIABLE])

    def note(event: str, organization_id: uuid.UUID) -> None:
        line = {
            "event": event,
            "owner": owner,
            "organization_id": str(organization_id),
            "at": _CLOCK.now().isoformat(),
        }
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(line) + "\n")
            log.flush()
            os.fsync(log.fileno())

    def wrap(name: str, handler: Any) -> Any:
        if name != chosen:
            return handler

        async def slow(transaction: Transaction) -> None:
            organization_id = transaction.context.organization_id
            note("start", organization_id)
            await handler(transaction)
            await asyncio.sleep(seconds)  # lo escrito sigue sin confirmar
            note("end", organization_id)

        return slow

    return wrap


async def build_runtime(config: WorkerConfig, environ: Mapping[str, str]) -> WorkerRuntime:
    database = Database.create(
        DatabaseSettings(
            url=environ["VIGIA_TEST_DATABASE_URL"],
            process=ProcessKind.WORKER,
            sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS
            worker_pool_size=4,
        )
    )
    provider = uuid.UUID(environ["VIGIA_TEST_PROVIDER_ORGANIZATION"])
    contexts = system_contexts(_CLOCK, provider)
    owner = f"proceso-{os.getpid()}"
    probe = LoggedProbe(
        owner=owner,
        log_path=environ["VIGIA_TEST_WORKER_LOG"],
        seconds=float(environ["VIGIA_TEST_WORKER_ORG_SECONDS"]),
    )
    catalog = u02_catalog(probe, EffectHandler(owner, environ), Schedule.every(3600))
    outbox = Outbox(catalog, _CLOCK)
    probe.outbox = outbox
    signing, kms, storage = StubSigning(), StubKms(), StubStorage()
    units = process_units(
        database=database,
        clock=_CLOCK,
        provider_organization_id=provider,
        contexts=contexts,
        outbox=outbox,
        signing=signing,
        kms=kms,
        storage=storage,
    )
    # U-03 en el mismo catálogo que ya usa la ``Outbox`` (las tareas publican con ella).
    built = add_u03(catalog, units.services, wrap=_slow_task(environ, owner))

    async def synchronize_catalog() -> None:
        await units.synchronize_record_types(contexts.provider_audit_context())
        await synchronize(database, built, _CLOCK)

    return WorkerRuntime(
        clock=_CLOCK,
        database=database,
        storage=storage,
        signing=signing,
        kms=cast(KmsPort, kms),  # el arranque solo pide y descifra la clave de datos
        catalog=built,
        dispatcher=Dispatcher(
            database=database,
            catalog=built,
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
