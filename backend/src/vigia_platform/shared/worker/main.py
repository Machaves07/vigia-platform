"""Punto de entrada ``vigia-worker`` (LC-NUC-24; PAT-NUC-ESC-04, RES-02, RES-05; FS-NUC-08).

El segundo proceso de la imagen. **Sin rutas**: solo ``/health/live`` en un puerto interno
(8001, en ``127.0.0.1``: la comprobación de salud del contenedor lo llama desde dentro). Hace:

1. **Arranque** con las mismas comprobaciones que ``vigia-api`` y en el mismo orden
   (``shared.api.app.StartupSupervisor``, PAT-NUC-RES-02): base con la seguridad a nivel de
   fila en vigor y versión mínima del esquema, claves de firma, clave de datos de KMS,
   registros (tipos, eventos, consumidores y tareas: un worker sin todos los consumidores
   registrados no arranca) y objeto centinela del almacén. Mientras tanto ``/health/live``
   responde 200. Si a los 60 s algo sigue fallando, termina con ``STARTUP_FAILURE_EXIT_CODE``.
2. **Un bucle de despacho por consumidor registrado** (U-02, U-03, U-04): ``Dispatcher.run``.
3. **El planificador** de tareas periódicas con arrendamiento (``shared.worker.scheduler``).
4. **La métrica** ``outbox_oldest_pending_age_seconds`` por consumidor (escala el servicio).

**Parada ordenada** (``SIGTERM`` o ``SIGINT``): ningún bucle empieza trabajo nuevo; cada
despacho termina la entrega en curso y el planificador termina la organización en curso y libera
su arrendamiento sin avanzar ``next_run_at`` (otro worker continúa enseguida). Se espera hasta
``shutdown_grace_seconds`` (115 s: Fargate envía ``SIGKILL`` a los 120 s, ``stopTimeout``) y lo
que quede se cancela. Una señal no capturable no libera nada: el arrendamiento vence y otro
worker retoma la tarea (FS-NUC-08).

**Composición.** ``WorkerRuntime`` trae las dependencias construidas (base con el pool del
worker y ``statement_timeout`` de 30 s, almacén, firma, KMS, catálogo de la bandeja con lo que
registró cada unidad, despachador y contextos). ``main`` lee ``WorkerConfig`` del entorno y pide
el ``WorkerRuntime`` al constructor que nombra ``VIGIA_WORKER_RUNTIME``
(``vigia_platform.<módulo>:<función>``,
asíncrono, recibe la configuración); sin él, el proceso no arranca.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import re
import signal
import sys
from collections.abc import Awaitable, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Protocol

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from vigia_platform.shared.api.app import (
    STARTUP_FAILURE_EXIT_CODE,
    AppConfig,
    AppRuntime,
    SigningRuntime,
    StartupSupervisor,
)
from vigia_platform.shared.api.declarations import UnauthenticatedRoute
from vigia_platform.shared.api.health import DatabaseHealthPort, SentinelPort
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import configure_logging, get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.secrets import KmsPort
from vigia_platform.shared.worker.leases import (
    LeaseSettings,
    SqlLeaseStore,
    TransactionSource,
    worker_owner_id,
)
from vigia_platform.shared.worker.scheduler import (
    DEFAULT_POLL_SECONDS,
    PeriodicScheduler,
    TaskContexts,
)

__all__ = [
    "HEALTH_PORT",
    "LIVE_PATH",
    "RUNTIME_VARIABLE",
    "SHUTDOWN_GRACE_SECONDS",
    "DispatchLoop",
    "OutboxAgeMonitor",
    "WorkerConfig",
    "WorkerDatabase",
    "WorkerProcess",
    "WorkerRuntime",
    "create_worker_app",
    "main",
]

_log = get_logger("shared.worker")

LIVE_PATH: Final = UnauthenticatedRoute.HEALTH_LIVE.path
"""La única ruta del worker (PAT-NUC-ESC-04)."""
HEALTH_PORT: Final = 8001
"""Puerto interno de ``/health/live`` (``infra/stacks/compute.py``, ``WORKER_HEALTH_PORT``)."""
SHUTDOWN_GRACE_SECONDS: Final = 115.0
"""Espera de la parada ordenada: 5 s menos que el ``stopTimeout`` de 120 s de Fargate, para
cancelar lo que quede y vaciar los registros antes del ``SIGKILL``."""
MONITOR_SECONDS: Final = 15.0
"""Cada cuánto se publica la antigüedad del evento pendiente más viejo ``[objetivo propio]``."""
RUNTIME_VARIABLE: Final = "VIGIA_WORKER_RUNTIME"
_RUNTIME_REFERENCE: Final = re.compile(r"vigia_platform(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
"""Solo un constructor del propio paquete: la variable no puede nombrar cualquier función."""

_ENVIRONMENT: Final = r"^(local|test|pilot|staging-[1-9][0-9]{0,2})$"
_SENTINEL_KEY: Final = r"^[A-Za-z0-9][A-Za-z0-9!_.*'()/=-]{0,511}$"
_KMS_KEY_ID: Final = r"^[A-Za-z0-9][A-Za-z0-9:/_-]{0,2047}$"
_OLDEST_PENDING: Final = text("SELECT shared.vigia_outbox_oldest_pending(:consumer) AS oldest")


class WorkerConfig(BaseModel):
    """Configuración de ``vigia-worker``: modelo estricto, inmutable, leído una vez."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    environment: str = Field(pattern=_ENVIRONMENT)
    data_key_id: str = Field(pattern=_KMS_KEY_ID)
    health_sentinel_key: str = Field(default="health/ready-sentinel", pattern=_SENTINEL_KEY)
    health_host: str = Field(default="127.0.0.1", pattern=r"^(127\.0\.0\.1|0\.0\.0\.0)$")
    health_port: int = Field(default=HEALTH_PORT, ge=1, le=65535)
    startup_deadline_seconds: float = Field(default=60.0, gt=0, le=600)
    startup_retry_seconds: float = Field(default=5.0, gt=0, le=60)
    shutdown_grace_seconds: float = Field(default=SHUTDOWN_GRACE_SECONDS, gt=0, le=120)
    lease_seconds: float = Field(default=60.0, gt=0, le=3600)
    renew_seconds: float = Field(default=20.0, gt=0, le=3600)
    lease_margin_seconds: float = Field(default=5.0, ge=0, le=3600)
    scheduler_poll_seconds: float = Field(default=DEFAULT_POLL_SECONDS, gt=0, le=3600)
    monitor_seconds: float = Field(default=MONITOR_SECONDS, gt=0, le=3600)

    @model_validator(mode="after")
    def _lease_timing(self) -> WorkerConfig:
        _ = self.lease_settings  # renovación < vigencia y margen < vigencia - renovación
        return self

    @property
    def lease_settings(self) -> LeaseSettings:
        return LeaseSettings(
            duration=timedelta(seconds=self.lease_seconds),
            renew_every=timedelta(seconds=self.renew_seconds),
            safety_margin=timedelta(seconds=self.lease_margin_seconds),
        )

    def app_config(self) -> AppConfig:
        """La configuración de las comprobaciones de arranque, la misma que la de la API."""
        return AppConfig(
            environment=self.environment,
            data_key_id=self.data_key_id,
            health_sentinel_key=self.health_sentinel_key,
            startup_deadline_seconds=self.startup_deadline_seconds,
            startup_retry_seconds=self.startup_retry_seconds,
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> WorkerConfig:
        """Lee ``VIGIA_ENVIRONMENT``, ``VIGIA_SECRETS_KEY_ARN`` y, si están,
        ``VIGIA_HEALTH_SENTINEL_KEY`` y ``VIGIA_WORKER_HEALTH_PORT``; ``ValueError`` (de
        Pydantic) si falta o no es válida."""
        values: dict[str, Any] = {
            "environment": environ.get("VIGIA_ENVIRONMENT", ""),
            "data_key_id": environ.get("VIGIA_SECRETS_KEY_ARN", ""),
        }
        sentinel = environ.get("VIGIA_HEALTH_SENTINEL_KEY")
        if sentinel is not None:
            values["health_sentinel_key"] = sentinel
        port = environ.get("VIGIA_WORKER_HEALTH_PORT")
        if port is not None:
            if not port.isascii() or not port.isdigit() or len(port) > 5:
                raise ValueError("VIGIA_WORKER_HEALTH_PORT debe ser un número de puerto")
            values["health_port"] = int(port)
        return cls(**values)


class WorkerDatabase(DatabaseHealthPort, TransactionSource, Protocol):
    """``shared.db.Database`` con el pool del worker."""

    async def dispose(self) -> None: ...


class DispatchLoop(Protocol):
    """``shared.outbox.dispatcher.Dispatcher``."""

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerRuntime:
    """Dependencias ya construidas de ``vigia-worker`` (las crea la raíz de composición)."""

    clock: Clock
    database: WorkerDatabase
    storage: SentinelPort
    signing: SigningRuntime
    kms: KmsPort
    catalog: OutboxCatalog
    """Con los tipos, consumidores y tareas que registró cada unidad; lo sella el arranque."""
    dispatcher: DispatchLoop
    contexts: TaskContexts
    registries: tuple[Callable[[], Awaitable[None]], ...] = ()
    """Sincronizadores de los registros (``OutboxCatalog.synchronize``…), como en la API."""
    metrics: PlatformMetrics | None = None
    owner: str = field(default_factory=worker_owner_id)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def create_worker_app() -> FastAPI:
    """La aplicación HTTP del worker: solo ``/health/live``, sin documentación ni esquema.

    Responde lo mismo que la de ``vigia-api`` (``{"status": "live"}``, sin caché) y no toca
    ninguna dependencia. No lleva la cadena de middleware de la API: escucha en un puerto interno
    y no atiende ninguna otra ruta.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(LIVE_PATH, include_in_schema=False)
    async def live() -> JSONResponse:
        return JSONResponse({"status": "live"}, headers={"Cache-Control": "no-store"})

    return app


class OutboxAgeMonitor:
    """Publica ``outbox_oldest_pending_age_seconds`` por consumidor (0 si no hay pendientes)."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        catalog: OutboxCatalog,
        contexts: TaskContexts,
        clock: Clock,
        metrics: PlatformMetrics,
        interval_seconds: float = MONITOR_SECONDS,
    ) -> None:
        self._database = database
        self._catalog = catalog
        self._contexts = contexts
        self._clock = clock
        self._metrics = metrics
        self._interval = interval_seconds
        # La dimensión ``consumer`` solo admite nombres registrados (NFR-NUC-41).
        for consumer in catalog.consumers.consumers():
            try:
                redaction.DEFAULT_POLICY.register("consumer", [consumer.consumer_name])
            except ValueError:
                _log.warning("nombre sin dimensión de métrica; se registra como «other»")

    async def report_once(self) -> dict[str, float]:
        ages: dict[str, float] = {}
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            for consumer in self._catalog.consumers.consumers():
                name = consumer.consumer_name
                oldest = (await tx.execute(_OLDEST_PENDING, {"consumer": name})).scalar_one()
                now = self._clock.now()
                ages[name] = 0.0 if oldest is None else max((now - oldest).total_seconds(), 0.0)
        for name, age in ages.items():
            self._metrics.outbox_oldest_pending_age_seconds.set(age, {"consumer": name})
        return ages

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.report_once()
            except Exception:
                _log.warning("no se pudo medir la antigüedad de la bandeja")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval)


class _HealthServer(uvicorn.Server):
    """uvicorn sin manejadores de señales: la parada ordenada es del worker."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class WorkerProcess:
    """Arranque, bucles y parada ordenada de ``vigia-worker``."""

    def __init__(self, config: WorkerConfig, runtime: WorkerRuntime) -> None:
        if not isinstance(config, WorkerConfig):
            raise TypeError("config debe ser WorkerConfig")
        self._config = config
        self._runtime = runtime
        self._failure: int | None = None
        metrics = runtime.metrics if runtime.metrics is not None else get_metrics()
        self._supervisor = StartupSupervisor(
            config.app_config(),
            AppRuntime(
                clock=runtime.clock,
                database=runtime.database,
                storage=runtime.storage,
                signing=runtime.signing,
                kms=runtime.kms,
                registries=runtime.registries,
                sleep=runtime.sleep,
                on_startup_failure=self._startup_failed,
                metrics=metrics,
            ),
        )
        self.scheduler = PeriodicScheduler(
            database=runtime.database,
            registry=runtime.catalog.periodic_tasks,
            leases=SqlLeaseStore(
                database=runtime.database,
                contexts=runtime.contexts,
                settings=config.lease_settings,
            ),
            contexts=runtime.contexts,
            clock=runtime.clock,
            owner=runtime.owner,
            metrics=metrics,
            poll_seconds=config.scheduler_poll_seconds,
        )
        self.monitor = OutboxAgeMonitor(
            database=runtime.database,
            catalog=runtime.catalog,
            contexts=runtime.contexts,
            clock=runtime.clock,
            metrics=metrics,
            interval_seconds=config.monitor_seconds,
        )
        self.app = create_worker_app()
        self.started = asyncio.Event()
        """Se fija cuando el arranque terminó y los bucles corren (pruebas)."""

    def _startup_failed(self, code: int) -> None:
        self._failure = code

    async def run(self, stop: asyncio.Event) -> int:
        """Hasta que ``stop`` se fije; devuelve el código de salida del proceso."""
        server = _HealthServer(
            uvicorn.Config(
                self.app,
                host=self._config.health_host,
                port=self._config.health_port,
                lifespan="off",
                log_config=None,
                access_log=False,
            )
        )
        serving = asyncio.create_task(server.serve())
        try:
            if not await self._start(stop):
                return STARTUP_FAILURE_EXIT_CODE if self._failure is not None else 0
            await self._work(stop)
            return 0
        finally:
            await self._supervisor.stop()
            server.should_exit = True
            with contextlib.suppress(Exception):
                await serving
            with contextlib.suppress(Exception):
                await self._runtime.database.dispose()

    async def _start(self, stop: asyncio.Event) -> bool:
        """El arranque supervisado; ``False`` si falló o llegó la parada antes."""
        starting = asyncio.create_task(self._supervisor.run())
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({starting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if not starting.done():
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await starting
            return False
        if not self._supervisor.started:
            return False
        if not self._runtime.catalog.sealed:
            # Los registros se sincronizan y sellan en el arranque; sin ellos no hay consumidores.
            _log.critical("el catálogo de la bandeja no quedó sellado en el arranque")
            self._failure = STARTUP_FAILURE_EXIT_CODE
            return False
        return True

    async def _work(self, stop: asyncio.Event) -> None:
        work_stop = asyncio.Event()
        consumers = [c.consumer_name for c in self._runtime.catalog.consumers.consumers()]
        loops = [
            asyncio.create_task(self._runtime.dispatcher.run(name, work_stop)) for name in consumers
        ]
        loops.append(asyncio.create_task(self.scheduler.run(work_stop)))
        loops.append(asyncio.create_task(self.monitor.run(work_stop)))
        _log.info("worker en marcha: despacho, planificador y métricas")
        self.started.set()
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({stopping, *loops}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        _log.info("parada ordenada: se termina lo que está en curso")
        work_stop.set()
        _, pending = await asyncio.wait(loops, timeout=self._config.shutdown_grace_seconds)
        for task in pending:
            task.cancel()
        for task in loops:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if pending:
            _log.warning("parada ordenada vencida: se cancela lo que quedaba")
        else:
            _log.info("parada ordenada completa")


# --- Consola ---------------------------------------------------------------------------------

type RuntimeBuilder = Callable[[WorkerConfig], Awaitable[WorkerRuntime]]


def resolve_runtime_builder(reference: str | None) -> RuntimeBuilder:
    """El constructor de ``VIGIA_WORKER_RUNTIME`` (``vigia_platform.<módulo>:<función>``)."""
    if reference is None or not _RUNTIME_REFERENCE.fullmatch(reference):
        raise ValueError(f"{RUNTIME_VARIABLE} debe ser «vigia_platform.<módulo>:<función>»")
    module_name, function_name = reference.split(":")
    builder = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(builder):
        raise ValueError(f"{RUNTIME_VARIABLE} no nombra una función")
    resolved: RuntimeBuilder = builder
    return resolved


async def serve(config: WorkerConfig, builder: RuntimeBuilder) -> int:
    """Construye el proceso, instala ``SIGTERM`` y ``SIGINT`` y corre hasta la parada."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop.set)
    try:
        runtime = await builder(config)
        return await WorkerProcess(config, runtime).run(stop)
    finally:
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(number)


def main(argv: Sequence[str] | None = None) -> int:
    """``vigia-worker``: sin argumentos; todo llega por el entorno."""

    configure_logging()
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        _log.critical("vigia-worker no admite argumentos: se configura por el entorno")
        return 2
    try:
        config = WorkerConfig.from_environ(os.environ)
        builder = resolve_runtime_builder(os.environ.get(RUNTIME_VARIABLE))
    except Exception:
        _log.exception("configuración de vigia-worker no válida: el proceso no arranca")
        return STARTUP_FAILURE_EXIT_CODE
    return asyncio.run(serve(config, builder))


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    sys.exit(main())
