"""Constructor de producción de ``vigia-worker`` (A-52; LC-NUC-24; PAT-NUC-RES-02).

``VIGIA_WORKER_RUNTIME=vigia_platform.shared.runtime.worker:build_worker_runtime``. Lee
``RuntimeConfig`` una vez del entorno y devuelve el ``WorkerRuntime``:

- ``Database`` de ``vigia_app`` con el pool del worker (20 conexiones) y ``statement_timeout`` de
  ``VIGIA_DB_STATEMENT_TIMEOUT_MS`` (30 s por defecto, NFR-NUC-36), con la credencial rotable de
  ``VIGIA_DB_APP_SECRET`` (runbook 6.6);
- los depósitos de evidencias (``VIGIA_EVIDENCE_BUCKET``: centinela y muestra diaria) y de
  archivo (``VIGIA_ARCHIVE_BUCKET``: ``archive_audit_partitions``), KMS, ``SigningService`` con
  ``LedgerRotationRecorder`` y la infraestructura común (``shared.runtime.core``);
- el ``OutboxCatalog`` con lo que registra cada unidad (``shared.runtime.units``): en U-02, sus
  11 tareas periódicas y sus 2 consumidores; el arranque lo sincroniza y lo sella;
- el ``Dispatcher`` y los ``TaskContexts`` (``ScopeContexts``).

Sin una variable obligatoria, o con un secreto de la base que no existe o no tiene la forma de
RDS, la construcción falla y ``vigia-worker`` sale con ``STARTUP_FAILURE_EXIT_CODE``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.db import ProcessKind
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.core import (
    build_core,
    load_credentials,
    open_database,
    s3_storage,
)
from vigia_platform.shared.runtime.db_credentials import SecretStringReader
from vigia_platform.shared.runtime.units import PlatformUnit, registered_units
from vigia_platform.shared.worker.main import WorkerConfig, WorkerRuntime

__all__ = ["build_worker_runtime", "compose_worker_runtime"]


async def build_worker_runtime(config: WorkerConfig) -> WorkerRuntime:
    """El constructor de ``VIGIA_WORKER_RUNTIME``: ``RuntimeConfig`` del entorno del proceso."""
    return await compose_worker_runtime(config, RuntimeConfig.from_environ(os.environ))


async def compose_worker_runtime(
    config: WorkerConfig,
    runtime: RuntimeConfig,
    *,
    clock: Clock | None = None,
    metrics: PlatformMetrics | None = None,
    units: Sequence[PlatformUnit] | None = None,
    reader: SecretStringReader | None = None,
) -> WorkerRuntime:
    """El ``WorkerRuntime`` de ``config`` y ``runtime`` (las pruebas inyectan reloj y lector)."""
    clock = clock if clock is not None else SystemClock()
    metrics = metrics if metrics is not None else get_metrics()
    selected = tuple(units) if units is not None else registered_units()
    provider = runtime.require("provider_organization_id")
    evidence_bucket: str = runtime.require("evidence_bucket")
    archive_bucket: str = runtime.require("archive_bucket")
    credentials = await load_credentials(runtime, reader)
    database = open_database(runtime, ProcessKind.WORKER, credentials, metrics)
    evidence = s3_storage(runtime, evidence_bucket, clock)
    core = build_core(
        runtime,
        database=database,
        clock=clock,
        metrics=metrics,
        provider_organization_id=provider,
        units=selected,
        evidence=evidence,
        archive=s3_storage(runtime, archive_bucket, clock),
    )
    services = core.services
    return WorkerRuntime(
        clock=clock,
        database=database,
        storage=evidence,
        signing=services.signing,
        kms=core.kms,
        catalog=core.catalog,
        dispatcher=Dispatcher(
            database=database,
            catalog=core.catalog,
            outbox=services.outbox,
            contexts=services.contexts,
            clock=clock,
            metrics=metrics,
        ),
        contexts=services.contexts,
        registries=core.synchronizers,
        metrics=metrics,
    )
