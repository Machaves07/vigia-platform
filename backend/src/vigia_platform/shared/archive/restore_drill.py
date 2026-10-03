"""Ensayo de restauración: registro y antigüedad (deployment-architecture §6.1; RESILIENCY-12).

El ensayo trimestral de restauración (runbook 6.1, DR-NUC-01 a 03) lo hace el dueño fuera de la
plataforma. Lo que la plataforma sabe de él:

- ``RestoreDrills.record``: ``vigia-admin record-restore-drill --result ok|failed`` escribe al
  terminar el runbook la entrada de auditoría ``restore_drill_recorded`` en la cadena de la
  organización proveedora, con ``outcome`` ``success`` u ``error`` y el filtro ``result``. Solo
  con una orden administrativa de un ``platform_operator`` vigente (``context_from_operator``).
- La tarea periódica ``restore_drill_age`` (diaria a las 04:00 UTC, con arrendamiento) publica
  ``restore_drill_age_days``: los días enteros desde el último ensayo **correcto**. Un ensayo
  fallido no la reinicia (la validación posterior del runbook exige cadenas ``intact``). Sin
  ningún ensayo correcto en la auditoría en línea, cuenta desde el alta de la organización
  proveedora: una plataforma de más de 100 días sin ensayo también alerta.

La alarma ``restore-drill-overdue`` (``infrastructure-design.md`` §9.4, nota U02-H-14) salta con
``restore_drill_age_days`` > ``RESTORE_DRILL_OVERDUE_DAYS``; la crea ``vigia-observability``.
El planificador invoca el manejador una vez por organización y solo actúa en la proveedora. No
lee la hora del sistema.
"""

from __future__ import annotations

import contextlib
import enum
import uuid
from datetime import datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditReceipt,
    AuditWriter,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    Role,
    ScopeContext,
    repository,
)
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)

__all__ = [
    "RESTORE_DRILL_AGE",
    "RESTORE_DRILL_AGE_SCHEDULE",
    "RESTORE_DRILL_OVERDUE_DAYS",
    "DrillResult",
    "RestoreDrills",
    "register_restore_drill_age",
    "restore_drill_age_handler",
]

RESTORE_DRILL_AGE: Final = "restore_drill_age"
RESTORE_DRILL_AGE_SCHEDULE: Final = Schedule.daily(hour=4)
"""Diaria a las 04:00 UTC ``[objetivo propio]``, fuera de las demás tareas nocturnas."""
RESTORE_DRILL_OVERDUE_DAYS: Final = 100
"""Umbral de la alarma ``restore-drill-overdue`` (nota U02-H-14 de §9.4)."""
_DAY: Final = timedelta(days=1)

_LAST_SUCCESS: Final = text(
    "SELECT max(occurred_at) AS last_success FROM shared.audit_entry"
    " WHERE operation = :operation AND outcome = :outcome"
)
_PROVIDER_CREATED: Final = text(
    "SELECT created_at FROM identity.organization WHERE organization_id = :organization_id"
)


class DrillResult(enum.StrEnum):
    """``--result`` de ``record-restore-drill``."""

    OK = "ok"
    FAILED = "failed"


class DrillDatabase(Protocol):
    """``shared.db.Database``."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


def _require_operator_command(context: ScopeContext, provider_organization_id: uuid.UUID) -> None:
    """Una orden administrativa de un ``platform_operator`` de la proveedora."""
    if (
        not isinstance(context, ScopeContext)
        or context.origin is not ContextOrigin.ADMIN_COMMAND
        or context.actor.kind is not ActorKind.OPERATOR
        or context.organization_id != provider_organization_id
        or not any(
            scope.role is Role.PLATFORM_OPERATOR and scope.covers(provider_organization_id)
            for scope in context.allowed_scopes
        )
    ):
        raise PermissionError("el ensayo lo registra un platform_operator con vigia-admin")


@repository
class RestoreDrills:
    """``restore_drill_recorded`` y ``restore_drill_age_days``."""

    def __init__(
        self,
        *,
        database: DrillDatabase,
        audit: AuditWriter,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()

    def __repr__(self) -> str:
        return "RestoreDrills()"

    async def record(self, operator_context: ScopeContext, result: DrillResult) -> AuditReceipt:
        """La entrada ``restore_drill_recorded`` del ensayo que acaba de terminar."""
        result = DrillResult(result)
        _require_operator_command(operator_context, self._audit.provider_organization_id)
        return await self._audit.append(
            operator_context,
            AuditOperation.RESTORE_DRILL_RECORDED,
            outcome=AuditOutcome.SUCCESS if result is DrillResult.OK else AuditOutcome.ERROR,
            filters={"result": result.value},
        )

    async def age_days(self, transaction: Transaction) -> int:
        """Días enteros desde el último ensayo correcto (o desde el alta de la proveedora)."""
        context = transaction.context
        if context.organization_id != self._audit.provider_organization_id:
            raise PermissionError("la antigüedad del ensayo se mide en la proveedora")
        row = (
            await transaction.execute(
                _LAST_SUCCESS,
                {
                    "operation": AuditOperation.RESTORE_DRILL_RECORDED.value,
                    "outcome": AuditOutcome.SUCCESS.value,
                },
            )
        ).one()
        since: datetime | None = row.last_success
        if since is None:
            since = (
                await transaction.execute(
                    _PROVIDER_CREATED, {"organization_id": context.organization_id}
                )
            ).scalar_one()
        return max((self._clock.now() - since) // _DAY, 0)

    async def report(self, transaction: Transaction) -> int:
        """Publica ``restore_drill_age_days`` y devuelve el valor."""
        days = await self.age_days(transaction)
        self._metrics.restore_drill_age_days.set(days)
        return days


def restore_drill_age_handler(
    drills: RestoreDrills, provider_organization_id: uuid.UUID
) -> PeriodicHandler:
    """Manejador de ``restore_drill_age``: actúa solo en la iteración de la proveedora."""
    if type(provider_organization_id) is not uuid.UUID:
        raise TypeError("provider_organization_id debe ser uuid.UUID")

    async def handler(transaction: Transaction) -> None:
        if transaction.context.organization_id != provider_organization_id:
            return
        await drills.report(transaction)

    return handler


def register_restore_drill_age(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea diaria ``restore_drill_age`` de U-02."""
    return registry.register(
        RESTORE_DRILL_AGE, RESTORE_DRILL_AGE_SCHEDULE, handler, unit=ActorUnit.U02
    )
