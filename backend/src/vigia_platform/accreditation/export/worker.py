"""Consumidor ``export_worker`` (TASK-317; LC-LAZ-21; PAT-LAZ-RES-01 y ESC-01; NFR-LAZ-12;
BR-LAZ-91, 100 y 101; U04-H-08; FS-LAZ-05).

El despachador de la bandeja entrega ``export_requested`` con la transacción y el contexto de la
organización del evento (BR-NUC-80). La entrega, como la de ``integrity_on_demand`` de U-02, queda
abierta mientras dura la composición; el trabajo abre sus **propias transacciones cortas** con ese
contexto (arrendamiento, progreso y cierre), independientes de la composición.

Un intento:

1. **Arrendamiento** (``lease.SqlExportLeaseStore.acquire``, una sola sentencia): si la solicitud
   ya terminó o no existe, la entrega se da por hecha (idempotente por evento, BR-NUC-76); si otro
   proceso la tiene con el arrendamiento vigente, o la organización ya tiene otra en curso,
   ``ExportBusy`` y el despachador reintenta más tarde. **Concurrencia 1 por proceso y 1 por
   organización**: el bucle del consumidor entrega una cabeza a la vez, el evento va en la
   partición de la organización (el despachador no solapa dos cabezas de una partición) y la
   adquisición exige que la organización no tenga otra en curso.
2. **Retoma** (``attempt_count`` > 1): se **aborta la subida por partes** que dejó el intento
   anterior por su ``multipart_upload_id`` guardado, y las demás sin terminar de la exportación
   (``UploadPort``, TASK-345), antes de componer de nuevo **desde cero**: la composición es
   determinista y no acumula estado parcial (NFR-LAZ-12).
3. **Primer intento** (U04-H-08): ``CheckpointPort.write_checkpoints_now`` sobre las cadenas
   cubiertas (expediente de la planta y de la organización, y auditoría; BR-LAZ-91) y
   ``generated_at``, que se **fijan** en la solicitud; una retoma los **reutiliza** y nunca vuelve
   a invocar el puerto. Así el manifiesto y el ``archive_sha256`` no cambian al retomar.
4. **Composición** (``PackageComposer.compose(job, sink)``), con un acompañante que, con el
   ``Clock`` inyectado, **renueva** el arrendamiento cada 20 s y **publica el progreso** si
   pasaron 30 s sin publicarlo (además de al terminar cada artefacto). Si una renovación o una
   escritura con valla no confirma, el arrendamiento se perdió: se cancela la composición y nada
   se cierra (``LeaseLost``).
5. **Cierre**: ``export_package`` y ``ready`` en una sola transacción con valla, con la descarga
   vigente 30 días (``expires_at``). **Nunca existe un paquete en un estado intermedio.** Un insumo
   obligatorio que falta (``ExportInputUnavailable``) deja la solicitud ``failed`` con su código y
   sin paquete (BR-LAZ-101). Cualquier otro fallo libera el arrendamiento (la solicitud sigue
   ``in_progress``) y se relanza: el despachador reintenta y el siguiente intento retoma.

**Alarma** ``export-failed`` (NFR-LAZ-42, hecho único de PAT-LAZ-MAN-01): a los **dos fallos
seguidos** de la misma solicitud, una sola vez: al adquirir el tercer intento (los dos anteriores
fallaron, por excepción o por muerte del proceso) o, si el segundo intento falla declarado y no
habrá tercero, en ese momento. Las dos transiciones son únicas (``attempt_count`` pasa de 2 a 3 en
una sola sentencia; ``failed`` es terminal). La métrica y el evento los da ``ExportAlarm``; el
nombre definitivo de la métrica lo fija el módulo de métricas de U-04 (TASK-349).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from opentelemetry import metrics as otel_metrics

from vigia_platform.accreditation.events import EXPORT_REQUESTED, ExportRequested
from vigia_platform.accreditation.export.composer_port import (
    ExportInputUnavailable,
    ExportJob,
    ExportPackage,
    PackageComposer,
    UploadPort,
)
from vigia_platform.accreditation.export.lease import (
    ExportLease,
    LeaseLost,
    SqlExportLeaseStore,
)
from vigia_platform.accreditation.export.state import DOWNLOAD_VALIDITY
from vigia_platform.accreditation.export.volumes import ExportLocation
from vigia_platform.ledger.chain.checkpoints import ChainKind, CheckpointChain, CheckpointResult
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import METER_NAME
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry
from vigia_platform.shared.signing.keys import to_millisecond
from vigia_platform.shared.worker.leases import worker_owner_id

__all__ = [
    "ALARM_AFTER_FAILURES",
    "EXPORT_FAILED_ALARM",
    "EXPORT_FAILED_METRIC",
    "EXPORT_WORKER",
    "EXPORT_WORKER_CONCURRENCY",
    "CheckpointPort",
    "ExportAlarm",
    "ExportBusy",
    "ExportWorker",
    "LoggingExportAlarm",
    "covered_chains",
    "register_export_worker",
]

EXPORT_WORKER: Final = "export_worker"
EXPORT_WORKER_CONCURRENCY: Final = 1
"""Concurrencia del mamparo de ``export_worker`` por proceso (PAT-LAZ-ESC-01)."""
ALARM_AFTER_FAILURES: Final = 2
"""Fallos seguidos de una solicitud que disparan la alarma (PAT-LAZ-RES-01, NFR-LAZ-42)."""
EXPORT_FAILED_ALARM: Final = "export-failed"
"""Nombre del evento de alarma (registro estructurado)."""
EXPORT_FAILED_METRIC: Final = "export_failed_total"
"""Métrica de la alarma; TASK-349 la lleva al módulo único de nombres de U-04."""

DEFAULT_TICK_SECONDS: Final = 1.0
"""Cada cuánto mira el acompañante el ``Clock`` (las decisiones son del reloj inyectado)."""

_log = get_logger("accreditation.export")


class ExportBusy(Exception):
    """Otro proceso tiene la solicitud (o la organización tiene otra en curso): más tarde."""

    code: Final = "export_busy"


class CheckpointPort(Protocol):
    """``CheckpointService.write_checkpoints_now`` de U-02 (BR-NUC-53)."""

    async def write_checkpoints_now(
        self, context: ScopeContext, chains: Sequence[CheckpointChain] | None = None
    ) -> tuple[CheckpointResult, ...]: ...


class ExportAlarm(Protocol):
    """La métrica y el evento de alarma ``export-failed`` (sin usuario ni texto libre)."""

    def export_failed(
        self,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        export_id: uuid.UUID,
        attempt_count: int,
    ) -> None: ...


class LoggingExportAlarm:
    """La alarma de producción: contador ``export_failed_total`` y registro ``export-failed``."""

    def __init__(self, meter: otel_metrics.Meter | None = None) -> None:
        meter = meter if meter is not None else otel_metrics.get_meter(METER_NAME)
        self._counter = meter.create_counter(
            EXPORT_FAILED_METRIC,
            unit="1",
            description="Exportaciones con dos fallos seguidos (NFR-LAZ-42)",
        )

    def export_failed(
        self,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        export_id: uuid.UUID,
        attempt_count: int,
    ) -> None:
        self._counter.add(1, {"organization_id": str(organization_id), "plant_id": str(plant_id)})
        _log.error(
            EXPORT_FAILED_ALARM,
            organization_id=str(organization_id),
            plant_id=str(plant_id),
            export_request_id=str(export_id),
            attempt_count=attempt_count,
        )


def covered_chains(plant_id: uuid.UUID) -> tuple[CheckpointChain, ...]:
    """Las cadenas que cubre una exportación de una planta (BR-LAZ-91)."""
    return (
        CheckpointChain(ChainKind.LEDGER, plant_id),
        CheckpointChain(ChainKind.LEDGER, None),
        CheckpointChain(ChainKind.AUDIT, None),
    )


type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Settings:
    tick_seconds: float = DEFAULT_TICK_SECONDS


@repository
class ExportWorker:
    """El manejador de ``export_worker`` (un intento por entrega)."""

    def __init__(
        self,
        *,
        leases: SqlExportLeaseStore,
        checkpoints: CheckpointPort,
        uploads: UploadPort,
        composer: PackageComposer,
        alarm: ExportAlarm,
        clock: Clock,
        owner: str | None = None,
        sleep: Sleep = asyncio.sleep,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        if not tick_seconds > 0:
            raise ValueError("tick_seconds debe ser positivo")
        self._leases = leases
        self._checkpoints = checkpoints
        self._uploads = uploads
        self._composer = composer
        self._alarm = alarm
        self._clock = clock
        self._owner = owner if owner is not None else worker_owner_id()
        self._sleep = sleep
        self._settings = _Settings(tick_seconds)

    @property
    def owner(self) -> str:
        return self._owner

    def _now(self) -> datetime:
        return to_millisecond(self._clock.now())

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        context = transaction.context
        # Como al publicar: la carga persistida es JSON (las listas no son tuplas en modo estricto).
        payload = ExportRequested.model_validate_json(json.dumps(dict(event.payload)), strict=True)
        export_id = uuid.UUID(str(payload.export_id))
        await self.run(context, export_id)

    async def run(self, context: ScopeContext, export_id: uuid.UUID) -> None:
        """Un intento sobre ``export_id`` con el contexto de su organización."""
        acquisition = await self._leases.acquire(context, export_id, self._owner, self._now())
        if acquisition.lease is None:
            if acquisition.settled:
                return
            raise ExportBusy()
        lease = acquisition.lease
        if lease.attempt_count == ALARM_AFTER_FAILURES + 1:
            self._raise_alarm(lease)
        try:
            lease = await self._prepare(context, lease)
            package = await _Run(self, context, lease).compose()
            now = self._now()
            await self._leases.finish_ready(context, lease, package, now, now + DOWNLOAD_VALIDITY)
        except ExportInputUnavailable as failure:
            await self._leases.finish_failed(context, lease, failure.failure_code, self._now())
            if lease.attempt_count == ALARM_AFTER_FAILURES:
                self._raise_alarm(lease)
        except LeaseLost:
            raise
        except BaseException:
            # Fallo no declarado: la solicitud sigue in_progress y se retoma desde cero.
            with contextlib.suppress(Exception):
                await self._leases.release(context, lease)
            raise

    async def _prepare(self, context: ScopeContext, lease: ExportLease) -> ExportLease:
        if lease.retaken:
            # Siempre al retomar, aunque no conste ninguna: el intento anterior pudo morir entre
            # abrir la subida y guardarla (TASK-345 barre las del prefijo de la exportación).
            location = ExportLocation(lease.organization_id, lease.plant_id, lease.export_id)
            await self._uploads.abort_previous_upload(location, lease.previous_upload_id)
            if lease.previous_upload_id is not None:
                await self._leases.record_upload(context, lease, None, self._now())
        if lease.generated_at is None:
            results = await self._checkpoints.write_checkpoints_now(
                context, covered_chains(lease.plant_id)
            )
            checkpoint_ids = tuple(
                result.checkpoint.entry_id for result in results if result.checkpoint is not None
            )
            now = self._now()
            lease = await self._leases.fix_first_attempt(context, lease, checkpoint_ids, now, now)
        return lease

    def _raise_alarm(self, lease: ExportLease) -> None:
        self._alarm.export_failed(
            organization_id=lease.organization_id,
            plant_id=lease.plant_id,
            export_id=lease.export_id,
            attempt_count=lease.attempt_count,
        )


class _Run:
    """Un intento en curso: el ``ExportSink`` del compositor y el acompañante del reloj."""

    def __init__(self, worker: ExportWorker, context: ScopeContext, lease: ExportLease) -> None:
        self._worker = worker
        self._context = context
        self._lease = lease
        self._percent = 0
        self._artifact: str | None = None
        now = worker._now()
        self._renewed_at = now
        self._published_at = now
        self._lost = False
        self._writing = asyncio.Lock()

    @property
    def _leases(self) -> SqlExportLeaseStore:
        return self._worker._leases

    async def compose(self) -> ExportPackage:
        lease = self._lease
        if lease.generated_at is None or lease.checkpoint_record_ids is None:
            raise LeaseLost()  # pragma: no cover - ``_prepare`` los fija antes de componer
        job = ExportJob(
            export_id=lease.export_id,
            organization_id=lease.organization_id,
            plant_id=lease.plant_id,
            zone_ids=lease.zone_ids,
            period_from=lease.period_from,
            period_to=lease.period_to,
            package_form=lease.package_form,
            requested_by=lease.requested_by,
            correlation_id=lease.correlation_id,
            attempt=lease.attempt_count,
            generated_at=lease.generated_at,
            checkpoint_record_ids=lease.checkpoint_record_ids,
        )
        composing = asyncio.create_task(self._worker._composer.compose(job, self))
        keeper = asyncio.create_task(self._keep_alive(composing))
        try:
            package = await composing
        except asyncio.CancelledError:
            if self._lost:
                raise LeaseLost() from None
            raise
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        if self._lost:
            raise LeaseLost()
        return package

    async def _keep_alive(self, composing: asyncio.Task[ExportPackage]) -> None:
        settings = self._leases.settings
        while not composing.done():
            await self._worker._sleep(self._worker._settings.tick_seconds)
            try:
                now = self._worker._now()
                if now - self._renewed_at >= settings.renew_every:
                    renewed = await self._leases.renew(self._context, self._lease, now)
                    if renewed is None:
                        raise LeaseLost()
                    self._lease = renewed
                    self._renewed_at = now
                if now - self._published_at >= settings.progress_every:
                    await self._publish(now)
            except LeaseLost:
                self._lost = True
                composing.cancel()
                return

    async def _publish(self, now: datetime) -> None:
        async with self._writing:
            await self._leases.publish_progress(
                self._context, self._lease, self._percent, self._artifact, now
            )
            self._published_at = now

    # --- ExportSink -----------------------------------------------------------------------------

    async def upload_started(self, multipart_upload_id: str) -> None:
        await self._leases.record_upload(
            self._context, self._lease, multipart_upload_id, self._worker._now()
        )

    async def artifact_started(self, artifact: str) -> None:
        self._artifact = artifact

    async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
        if total <= 0 or not 0 <= done <= total:
            raise ValueError("done debe ir de 0 a total, y total ser positivo")
        self._artifact = artifact
        # 100 solo cuando el paquete está cerrado (``ready``).
        self._percent = min(99, done * 100 // total)
        try:
            await self._publish(self._worker._now())
        except LeaseLost:
            self._lost = True
            raise


def register_export_worker(registry: ConsumerRegistry, worker: ExportWorker) -> Consumer:
    """Registra ``export_worker`` (U-04, sin dependencia externa) al arrancar el worker."""
    return registry.register(
        Consumer(
            consumer_name=EXPORT_WORKER,
            unit=ActorUnit.U04,
            subscribed_events=(EXPORT_REQUESTED,),
            handler=worker,
        )
    )
