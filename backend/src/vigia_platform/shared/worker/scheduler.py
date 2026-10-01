"""Planificador de tareas periódicas de ``vigia-worker`` (LC-NUC-24; PAT-NUC-RES-05; BR-NUC-81).

Las unidades registran sus tareas en ``PeriodicTaskRegistry`` (``OutboxCatalog.periodic_tasks``)
y el arranque las sincroniza con ``shared.periodic_task``. ``PeriodicScheduler.run(stop)``
consulta cada ``poll_seconds`` qué tareas vencieron y ejecuta cada una con arrendamiento
(``shared.worker.leases``):

1. ``acquire``: si otro proceso la tiene, la salta. Mientras corre, una tarea en segundo plano
   la renueva cada 20 s; si una renovación no se confirma, el proceso deja de empezar
   organizaciones (también si se acerca el vencimiento sin haber podido renovar).
2. Lista las organizaciones activas (``shared.vigia_active_organizations``: solo
   identificadores, en orden) y las recorre **una a una**, cada una en **su transacción** con
   **su contexto** de iteración periódica (``context_for_organization``, BR-NUC-03). Si la
   ejecución ya tenía avance (otro proceso murió a mitad), empieza por la organización
   siguiente a la última terminada.
3. En la transacción de cada organización: comprueba el arrendamiento, invoca al manejador
   dentro de un ``SAVEPOINT`` y avanza el cursor (la valla de ``SqlLeaseStore.advance``). Si el
   manejador falla, se deshace lo suyo, el fallo **se registra** (cursor con el fallo sumado,
   registro de error con la tarea, la organización y el código, y la métrica con resultado
   ``failed``) y la tarea **sigue con las demás** (BR-NUC-81). Si la valla no se cumple, la
   transacción se deshace entera y el proceso abandona la tarea sin liberarla: ya es de otro.
4. ``release`` con ``next_run_at`` del horario y el resultado (``succeeded`` o
   ``partial_failure``). Una parada ordenada o la base caída a mitad liberan **sin** avanzar
   ``next_run_at`` (``interrupted``): otro proceso la toma enseguida y continúa por el cursor.

Si el proceso muere (señal no capturable), nada se libera: el arrendamiento vence a los 60 s y
otro proceso retoma la tarea donde quedó; lo confirmado por organización no se repite (FS-NUC-08).
Lo que el manejador haga fuera de la transacción de la organización debe ser idempotente por
organización y ejecución (PAT-NUC-RES-05).

Métricas: ``periodic_task_duration_ms`` por tarea, resultado y organización, y
``periodic_task_last_success_age_seconds`` por tarea (desde ``last_success_at``).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction, TransientDatabaseError
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.dispatcher import error_code
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry
from vigia_platform.shared.worker.leases import (
    Lease,
    LeaseLost,
    LeaseSettings,
    SqlLeaseStore,
    TaskOutcome,
    TransactionSource,
)

__all__ = [
    "DEFAULT_POLL_SECONDS",
    "OrganizationOutcome",
    "OrganizationResult",
    "PeriodicScheduler",
    "TaskContexts",
    "TaskRunReport",
]

_log = get_logger("shared.worker")

DEFAULT_POLL_SECONDS: Final = 5.0
"""Cada cuánto se consultan las tareas vencidas ``[objetivo propio]``: la más frecuente de U-03
es de 60 s."""

_ACTIVE_ORGANIZATIONS: Final = text(
    "SELECT organization_id FROM shared.vigia_active_organizations()"
)
_DUE_TASKS: Final = text(
    "SELECT task_name FROM shared.periodic_task"
    " WHERE next_run_at <= :now AND (lease_until IS NULL OR lease_until < :now)"
    " ORDER BY next_run_at, task_name"
)
_LAST_SUCCESS: Final = text(
    "SELECT task_name, last_success_at FROM shared.periodic_task WHERE last_success_at IS NOT NULL"
)


class TaskContexts(Protocol):
    """Los constructores que usa el planificador (``identity.authz.ScopeContexts``)."""

    def provider_audit_context(self) -> ScopeContext:
        """Lecturas globales: tareas vencidas y organizaciones activas (actor ``system``)."""
        ...

    def context_for_organization(
        self, task: PeriodicTask, organization_id: uuid.UUID
    ) -> ScopeContext:
        """Una organización por iteración, con el actor del sistema (BR-NUC-03)."""
        ...


class OrganizationOutcome(enum.StrEnum):
    """Resultado de una tarea en una organización (atributo ``result`` de la métrica)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OrganizationResult:
    organization_id: uuid.UUID
    outcome: OrganizationOutcome
    error_code: str | None = None


@dataclass(slots=True)
class TaskRunReport:
    """Lo que hizo este proceso con una ejecución de una tarea."""

    task_name: str
    run_at: datetime
    resumed_after: uuid.UUID | None
    results: list[OrganizationResult] = field(default_factory=list)
    outcome: TaskOutcome | None = None
    """Con qué se liberó; ``None`` si no se liberó (arrendamiento perdido)."""
    lease_lost: bool = False

    def count(self, outcome: OrganizationOutcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)


class _Holder:
    """El arrendamiento en curso, que la renovación en segundo plano mantiene al día."""

    def __init__(self, lease: Lease) -> None:
        self.lease = lease
        self.lost = False

    def usable(self, now: datetime, settings: LeaseSettings) -> bool:
        return not self.lost and now <= self.lease.until - settings.safety_margin


class _Interrupted(Exception):
    """La ejecución se corta sin perder el arrendamiento (parada ordenada, base caída)."""


class PeriodicScheduler:
    """Ejecuta con arrendamiento las tareas de ``registry`` que vencen."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        registry: PeriodicTaskRegistry,
        leases: SqlLeaseStore,
        contexts: TaskContexts,
        clock: Clock,
        owner: str,
        metrics: PlatformMetrics | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        on_renewal: Callable[[Lease], None] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds debe ser positivo")
        self._database = database
        self._registry = registry
        self._leases = leases
        self._settings = leases.settings
        self._contexts = contexts
        self._clock = clock
        self._owner = owner
        self._metrics = metrics if metrics is not None else get_metrics()
        self._poll_seconds = poll_seconds
        self._on_renewal = on_renewal
        # Las métricas solo admiten valores registrados (NFR-NUC-41), como en el despachador.
        for key, values in (
            ("task", [task.task_name for task in registry.tasks()]),
            ("result", [outcome.value for outcome in OrganizationOutcome]),
            ("reason", [outcome.value for outcome in TaskOutcome]),
        ):
            for value in values:
                try:
                    redaction.DEFAULT_POLICY.register(key, [value])
                except ValueError:
                    _log.warning("nombre sin dimensión de métrica; se registra como «other»")

    @property
    def owner(self) -> str:
        return self._owner

    # --- Bucle ------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Rondas hasta que ``stop`` se fije; una tarea en curso libera su arrendamiento."""
        while not stop.is_set():
            try:
                await self.run_pending(stop)
                await self._report_last_success()
            except TransientDatabaseError:
                _log.warning("planificador en pausa: la base no responde")
            except Exception:
                _log.exception("fallo inesperado del planificador")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

    async def run_pending(self, stop: asyncio.Event | None = None) -> list[TaskRunReport]:
        """Una ronda: cada tarea registrada que vence y nadie tiene."""
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            rows = (await tx.execute(_DUE_TASKS, {"now": self._clock.now()})).all()
        reports: list[TaskRunReport] = []
        for row in rows:
            if stop is not None and stop.is_set():
                break
            if self._registry.get(row.task_name) is None:
                continue
            report = await self.run_due(row.task_name, stop)
            if report is not None:
                reports.append(report)
        return reports

    async def _report_last_success(self) -> None:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            rows = (await tx.execute(_LAST_SUCCESS)).all()
        now = self._clock.now()
        for row in rows:
            if self._registry.get(row.task_name) is not None:
                age = max((now - row.last_success_at).total_seconds(), 0.0)
                self._metrics.periodic_task_last_success_age_seconds.set(
                    age, {"task": row.task_name}
                )

    # --- Una ejecución ----------------------------------------------------------------------

    async def run_due(
        self, task_name: str, stop: asyncio.Event | None = None
    ) -> TaskRunReport | None:
        """Ejecuta ``task_name`` si vence y nadie la tiene; ``None`` si no la tomó."""
        task = self._registry.get(task_name)
        if task is None:
            raise LookupError(f"tarea no registrada: {task_name!r}")
        lease = await self._leases.acquire(task_name, self._owner, self._clock.now())
        if lease is None:
            return None
        holder = _Holder(lease)
        report = TaskRunReport(task_name, lease.run_at, lease.resume_after)
        _log.info("tarea periódica tomada", task=task_name)
        renewal = asyncio.create_task(self._keep_renewed(holder))
        try:
            await self._iterate(task, holder, report, stop)
        except LeaseLost:
            report.lease_lost = True
            _log.warning("tarea periódica abandonada: el arrendamiento ya no es del proceso")
            return report
        except _Interrupted:
            report.outcome = TaskOutcome.INTERRUPTED
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
        outcome = report.outcome
        if outcome is None:
            outcome = (
                TaskOutcome.SUCCEEDED if holder.lease.failures == 0 else TaskOutcome.PARTIAL_FAILURE
            )
            report.outcome = outcome
        await self._release(task, holder.lease, outcome, report)
        return report

    async def _release(
        self, task: PeriodicTask, lease: Lease, outcome: TaskOutcome, report: TaskRunReport
    ) -> None:
        now = self._clock.now()
        next_run_at = (
            lease.run_at if outcome is TaskOutcome.INTERRUPTED else task.schedule.next_after(now)
        )
        try:
            released = await self._leases.release(
                lease, now=now, next_run_at=next_run_at, outcome=outcome
            )
        except TransientDatabaseError:
            # El arrendamiento vence solo y otro proceso continúa por el cursor.
            _log.warning("no se pudo liberar la tarea: la base no responde", task=task.task_name)
            return
        if not released:
            report.lease_lost = True
            _log.warning("tarea periódica terminada sin arrendamiento", task=task.task_name)
            return
        _log.info("tarea periódica liberada", task=task.task_name, reason=outcome.value)

    async def _keep_renewed(self, holder: _Holder) -> None:
        interval = self._settings.renew_every.total_seconds()
        while not holder.lost:
            await asyncio.sleep(interval)
            try:
                renewed = await self._leases.renew(holder.lease, self._clock.now())
            except TransientDatabaseError:
                # Sin confirmación no hay renovación: ``usable`` corta al acercarse el vencimiento.
                _log.warning("no se pudo renovar el arrendamiento: la base no responde")
                continue
            if renewed is None:
                holder.lost = True
                _log.warning("arrendamiento no renovado: la tarea ya no es del proceso")
                return
            holder.lease = renewed
            if self._on_renewal is not None:
                self._on_renewal(renewed)

    async def _organizations(self) -> list[uuid.UUID]:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            rows = (await tx.execute(_ACTIVE_ORGANIZATIONS)).all()
        return [uuid.UUID(str(row.organization_id)) for row in rows]

    async def _iterate(
        self,
        task: PeriodicTask,
        holder: _Holder,
        report: TaskRunReport,
        stop: asyncio.Event | None,
    ) -> None:
        try:
            organizations = await self._organizations()
        except TransientDatabaseError:
            _log.warning("no se pudieron listar las organizaciones", task=task.task_name)
            raise _Interrupted() from None
        resume_after = holder.lease.resume_after
        for organization_id in organizations:
            if resume_after is not None and organization_id <= resume_after:
                continue
            if stop is not None and stop.is_set():
                _log.info("tarea periódica interrumpida por la parada", task=task.task_name)
                raise _Interrupted()
            if holder.lost:
                raise LeaseLost()
            if not holder.usable(self._clock.now(), self._settings):
                # Sin renovación confirmada no se empieza otra organización.
                _log.warning("arrendamiento a punto de vencer sin renovar", task=task.task_name)
                raise _Interrupted()
            try:
                result = await self._run_for(task, holder, organization_id)
            except TransientDatabaseError:
                _log.warning(
                    "tarea periódica interrumpida: la base no responde",
                    task=task.task_name,
                    organization_id=str(organization_id),
                )
                raise _Interrupted() from None
            report.results.append(result)

    async def _run_for(
        self, task: PeriodicTask, holder: _Holder, organization_id: uuid.UUID
    ) -> OrganizationResult:
        context = self._contexts.context_for_organization(task, organization_id)
        started = self._clock.monotonic()
        failure: Exception | None = None
        async with self._database.transaction(context) as tx:
            if not await self._leases.holds(tx, holder.lease, self._clock.now()):
                raise LeaseLost()
            failure = await self._invoke(task, tx)
            advanced = await self._leases.advance(
                tx, holder.lease, organization_id, failed=failure is not None, now=self._clock.now()
            )
            if advanced is None:
                # La valla: se deshace la transacción entera, con lo que hizo el manejador.
                raise LeaseLost()
        # Confirmado: el avance y lo que hizo el manejador en la transacción.
        holder.lease = advanced
        elapsed_ms = max((self._clock.monotonic() - started) * 1000, 0.0)
        outcome = OrganizationOutcome.SUCCEEDED if failure is None else OrganizationOutcome.FAILED
        self._metrics.periodic_task_duration_ms.record(
            elapsed_ms,
            {
                "task": task.task_name,
                "result": outcome.value,
                "organization_id": str(organization_id),
            },
        )
        if failure is None:
            return OrganizationResult(organization_id, outcome)
        code = error_code(failure)
        # El código sale de una constante o del nombre de una clase, nunca de datos.
        with contextlib.suppress(ValueError):
            redaction.DEFAULT_POLICY.register("code", [code])
        _log.error(
            "tarea periódica fallida en una organización; sigue con las demás",
            task=task.task_name,
            organization_id=str(organization_id),
            code=code,
        )
        return OrganizationResult(organization_id, outcome, code)

    @staticmethod
    async def _invoke(task: PeriodicTask, transaction: Transaction) -> Exception | None:
        """El manejador dentro de un ``SAVEPOINT``: si falla, se deshace solo lo suyo."""
        try:
            async with transaction.savepoint():
                await task.handler(transaction)
        except Exception as error:
            if transaction.failed:
                # Conexión rota o paso abandonado: no hay transacción en la que registrarlo.
                raise
            return error
        return None
