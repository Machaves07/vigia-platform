"""Materialización diaria de meses cerrados, en el calendario local de cada zona.

Recupera desde el alta, de más antiguo a más reciente. Cada instantánea confirma su propia
transacción; tras una caída, se conserva todo definitivo ya confirmado, aunque avance la huella.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text

from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.loop.calendar import period_of
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule

METRICS_MATERIALIZE = "metrics_materialize"
METRICS_SCHEDULE = Schedule.daily(hour=3)
_ZONES = text(
    "SELECT z.zone_id, z.created_at, p.timezone FROM identity.zone z JOIN identity.plant p"
    " ON p.organization_id=z.organization_id AND p.plant_id=z.plant_id"
    " WHERE z.organization_id=:organization_id ORDER BY z.plant_id,z.zone_id"
)


def closed_periods(created_at: datetime, now: datetime, timezone: str) -> tuple[str, ...]:
    first = period_of(created_at, timezone)
    current = period_of(now, timezone)
    year, month = map(int, first.split("-"))
    periods: list[str] = []
    while (period := f"{year:04d}-{month:02d}") < current:
        periods.append(period)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(periods)


@repository
class MetricsMaterialize:
    def __init__(self, *, service: ProgramMetricsService, clock: Clock) -> None:
        self._service = service
        self._clock = clock

    async def run(self, transaction: Transaction) -> None:
        rows = (
            await transaction.execute(
                _ZONES,
                {
                    "organization_id": transaction.context.organization_id,
                },
            )
        ).all()
        for row in rows:
            for period in closed_periods(row.created_at, self._clock.now(), str(row.timezone)):
                await self._service.snapshot(
                    transaction.context, uuid.UUID(str(row.zone_id)), period
                )


def register_metrics_materialize(registry: PeriodicTaskRegistry, task: MetricsMaterialize) -> None:
    registry.register(METRICS_MATERIALIZE, METRICS_SCHEDULE, task.run, unit=ActorUnit.U04)
