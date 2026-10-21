"""Métricas vivas de U-04, muestreadas por el monitor del worker (NFR-LAZ-41 y 42).

Las lecturas de cliente se hacen por organización con RLS, nunca en la transacción de control.
Se incluyen plantas sin hallazgos y consumidores vacíos para retirar valores obsoletos. No se
derivan gauges de páginas de usuario (un filtro, una página o falta de tráfico no son el total).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Final

from sqlalchemy import text

from vigia_platform.loop.observability.metrics import LoopMetricName, LoopMetrics, get_loop_metrics
from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.outbox.dispatcher import TransactionSource
from vigia_platform.shared.outbox.registries import PeriodicTask
from vigia_platform.shared.signing.keys import to_millisecond
from vigia_platform.shared.worker.scheduler import TaskContexts

_ORGANIZATIONS: Final = text("SELECT organization_id FROM shared.vigia_active_organizations()")
_FINDINGS: Final = text(
    "SELECT p.plant_id,"
    " count(f.finding_state_id) FILTER (WHERE f.lifecycle_state IN ('received', 'in_review'))"
    " AS unclassified,"
    " min(f.occurred_at_exact) FILTER (WHERE f.lifecycle_state IN ('received', 'in_review'))"
    " AS oldest,"
    " count(f.finding_state_id) FILTER (WHERE f.lifecycle_state IN ('action_assigned', 'overdue')"
    " AND f.current_action_id IS NOT NULL AND f.due_at IS NOT NULL"
    " AND COALESCE(f.overdue_since, f.due_at) <= :now) AS overdue"
    " FROM identity.plant p LEFT JOIN loop.finding_state f"
    " ON f.organization_id = p.organization_id AND f.plant_id = p.plant_id"
    " WHERE p.organization_id = :organization_id GROUP BY p.plant_id"
)
_PENDING: Final = text(
    "SELECT d.consumer_name, count(*) AS pending, min(e.created_at) AS oldest"
    " FROM shared.outbox_delivery d JOIN shared.outbox_event e ON e.event_id = d.event_id"
    " WHERE e.organization_id = :organization_id AND d.status IN ('pending', 'retrying')"
    " GROUP BY d.consumer_name"
)
# Los fallos terminales quedan en export_failed_total: no deben enclavar el gauge de atasco.
_EXPORT: Final = text(
    "SELECT COALESCE(bool_or("
    " (state IN ('requested', 'in_progress') AND requested_at < :cutoff)"
    " OR (state = 'in_progress' AND (attempt_count >= 3 OR (attempt_count >= 2"
    " AND (lease_owner IS NULL OR lease_until < :now))))"
    "), false) AS unhealthy FROM accreditation.export_request"
    " WHERE organization_id = :organization_id"
)


class LoopWorkerTelemetry:
    """Adaptación del puerto compartido; solo los nombres registrados de U-04."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        contexts: TaskContexts,
        clock: Clock,
        task: PeriodicTask,
        metrics: LoopMetrics | None = None,
    ) -> None:
        self._database = database
        self._contexts = contexts
        self._clock = clock
        self._task = task
        self._metrics = metrics if metrics is not None else get_loop_metrics()

    def dead_letter(self, consumer: str) -> None:
        if consumer in U04_CONSUMERS:
            self._metrics.counter(LoopMetricName.LOOP_DEAD_LETTER_ENTRIES_TOTAL).add(
                1, {"consumer": consumer}
            )

    def task_duration(self, task: str, result: str, elapsed_ms: float) -> None:
        if task in U04_TASKS:
            self._metrics.histogram(LoopMetricName.LOOP_PERIODIC_TASK_DURATION_MS).record(
                elapsed_ms, {"task": task, "result": result}
            )

    def task_age(self, task: str, age: float) -> None:
        if task in U04_TASKS:
            self._metrics.gauge(LoopMetricName.LOOP_PERIODIC_TASK_LAST_SUCCESS_AGE_SECONDS).set(
                age, {"task": task}
            )

    async def report_once(self) -> None:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            organizations = (await tx.execute(_ORGANIZATIONS)).all()
        pending = dict.fromkeys(U04_CONSUMERS, 0)
        ages = dict.fromkeys(U04_CONSUMERS, 0.0)
        now = to_millisecond(self._clock.now())
        for organization in organizations:
            organization_id = uuid.UUID(str(organization.organization_id))
            context = self._contexts.context_for_organization(self._task, organization_id)
            parameters = {
                "organization_id": organization_id,
                "now": now,
                "cutoff": now - timedelta(hours=1),
            }
            async with self._database.transaction(context) as tx:
                findings = (await tx.execute(_FINDINGS, parameters)).all()
                deliveries = (await tx.execute(_PENDING, parameters)).all()
                unhealthy = (await tx.execute(_EXPORT, parameters)).scalar_one()
            for row in findings:
                attributes = {
                    "organization_id": str(organization_id),
                    "plant_id": str(row.plant_id),
                }
                self._metrics.gauge(LoopMetricName.LOOP_UNCLASSIFIED_FINDINGS).set(
                    row.unclassified, attributes
                )
                age = 0.0 if row.oldest is None else max(0.0, (now - row.oldest).total_seconds())
                self._metrics.gauge(LoopMetricName.LOOP_UNCLASSIFIED_OLDEST_AGE_SECONDS).set(
                    age, attributes
                )
                self._metrics.gauge(LoopMetricName.LOOP_OVERDUE_FINDINGS).set(
                    row.overdue, attributes
                )
            self._metrics.gauge(LoopMetricName.EXPORT_STUCK_OR_FAILED).set(
                int(unhealthy), {"organization_id": str(organization_id)}
            )
            for row in deliveries:
                if row.consumer_name in pending:
                    pending[row.consumer_name] += int(row.pending)
                    age = max(0.0, (now - row.oldest).total_seconds())
                    ages[row.consumer_name] = max(ages[row.consumer_name], age)
        # Solo publicar el total global tras leer todas las organizaciones: nunca uno parcial.
        for consumer in U04_CONSUMERS:
            attributes = {"consumer": consumer}
            self._metrics.gauge(LoopMetricName.LOOP_CONSUMER_PENDING).set(
                pending[consumer], attributes
            )
            self._metrics.gauge(LoopMetricName.LOOP_CONSUMER_OLDEST_PENDING_AGE_SECONDS).set(
                ages[consumer], attributes
            )
