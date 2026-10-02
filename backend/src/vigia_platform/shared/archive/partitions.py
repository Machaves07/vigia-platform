"""Tarea ``create_partitions``: particiones mensuales por adelantado (LC-NUC-33; PAT-NUC-ESC-01).

``LedgerRecord``, ``Evidence`` y ``AuditEntry`` están particionadas por **rango mensual** en UTC
(``ledger.ledger_record`` por ``received_at``, ``ledger.evidence`` por ``verified_at`` y
``shared.audit_entry`` por ``occurred_at``; nuc_0002 y nuc_0003), con una partición por defecto
que recibe cualquier fila fuera de rango sin rechazarla. La migración dejó el mes en curso y los
tres siguientes; esta tarea, **semanal** (lunes a las 01:00 UTC) y con arrendamiento, mantiene
siempre creados el mes en curso y los ``PARTITION_MONTHS_AHEAD`` siguientes según el reloj
inyectado, y publica ``default_partition_rows`` por tabla: una sola fila en una partición por
defecto dispara la alarma (señal de que la tarea falló).

La creación la hace ``shared.vigia_create_month_partitions`` (nuc_0016), que es **idempotente**:
un mes que ya tiene partición no se toca, así que ejecutar la tarea dos veces no falla ni duplica
nada. Si la partición por defecto ya tiene filas de un mes, ese mes no se puede crear (PostgreSQL
lo impide y las filas no se pueden mover: la tabla es de solo anexar); la función lo devuelve como
``blocked``, la tarea lo registra como error y la alarma de ``default_partition_rows`` sigue
encendida hasta que un operador lo resuelva.

El planificador (TASK-130) invoca el manejador **una vez por organización**; las particiones son
globales, así que solo actúa en la iteración de la organización proveedora y en las demás no hace
nada. ``vigia-admin create-partitions --until`` (TASK-132) usa ``PartitionMaintenance.create`` con
un contexto de orden administrativa.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)

__all__ = [
    "CREATE_PARTITIONS",
    "CREATE_PARTITIONS_SCHEDULE",
    "MAX_MONTHS_PER_CALL",
    "PARTITION_MONTHS_AHEAD",
    "PartitionMaintenance",
    "PartitionReport",
    "PartitionResult",
    "PartitionedTable",
    "add_months",
    "create_partitions_handler",
    "month_of",
    "register_create_partitions",
]

_log = get_logger("shared.archive")

CREATE_PARTITIONS: Final = "create_partitions"
CREATE_PARTITIONS_SCHEDULE: Final = Schedule.weekly(weekday=0, hour=1)
"""Semanal, los lunes a las 01:00 UTC ``[objetivo propio]``: con tres meses de margen, una semana
sin ejecutarse no deja ningún mes sin partición."""
PARTITION_MONTHS_AHEAD: Final = 3
"""Meses siguientes al actual que siempre tienen partición (PAT-NUC-ESC-01)."""
MAX_MONTHS_PER_CALL: Final = 120
"""Tope de meses por llamada de ``shared.vigia_create_month_partitions`` (nuc_0016)."""

_CREATE: Final = text(
    "SELECT parent, partition, month, created, blocked"
    " FROM shared.vigia_create_month_partitions(:first_month, :last_month)"
)
_DEFAULT_ROWS: Final = text("SELECT parent, row_count FROM shared.vigia_default_partition_rows()")


class PartitionedTable(enum.StrEnum):
    """Las tablas particionadas por mes; el valor es el atributo ``table`` de la métrica."""

    LEDGER_RECORD = "ledger.ledger_record"
    EVIDENCE = "ledger.evidence"
    AUDIT_ENTRY = "shared.audit_entry"


@dataclass(frozen=True, slots=True)
class PartitionResult:
    """Un mes de una tabla: si se creó ahora o si la partición por defecto lo impide."""

    table: PartitionedTable
    partition: str
    month: date
    created: bool
    blocked: bool


@dataclass(frozen=True, slots=True)
class PartitionReport:
    """Lo que hizo una llamada: un resultado por tabla y mes, y las filas de cada partición por
    defecto."""

    first_month: date
    last_month: date
    results: tuple[PartitionResult, ...]
    default_rows: dict[PartitionedTable, int]

    @property
    def created(self) -> tuple[PartitionResult, ...]:
        return tuple(result for result in self.results if result.created)

    @property
    def blocked(self) -> tuple[PartitionResult, ...]:
        return tuple(result for result in self.results if result.blocked)


def month_of(instant: datetime) -> date:
    """El primer día del mes (UTC) de ``instant``."""
    if instant.tzinfo is None:
        raise ValueError("instant debe llevar zona horaria")
    moment = instant.astimezone(UTC)
    return date(moment.year, moment.month, 1)


def add_months(month: date, months: int) -> date:
    """El primer día del mes ``months`` después (o antes, si es negativo) de ``month``."""
    index = month.year * 12 + (month.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


@repository
class PartitionMaintenance:
    """Crea las particiones mensuales que falten y publica ``default_partition_rows``."""

    def __init__(
        self,
        *,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        months_ahead: int = PARTITION_MONTHS_AHEAD,
    ) -> None:
        if isinstance(months_ahead, bool) or not 0 <= months_ahead < MAX_MONTHS_PER_CALL:
            raise ValueError(f"months_ahead debe estar entre 0 y {MAX_MONTHS_PER_CALL - 1}")
        self._clock = clock
        self._metrics = metrics if metrics is not None else get_metrics()
        self._months_ahead = months_ahead
        try:
            redaction.DEFAULT_POLICY.register("table", [table.value for table in PartitionedTable])
        except ValueError:  # pragma: no cover - valores constantes del código
            _log.warning("nombre sin dimensión de métrica; se registra como «other»")

    @property
    def months_ahead(self) -> int:
        return self._months_ahead

    async def create(
        self, transaction: Transaction, *, until: date | None = None
    ) -> PartitionReport:
        """Particiones del mes en curso hasta ``until`` (por omisión, ``months_ahead`` después).

        Corre en ``transaction``, cuyo contexto debe ser del sistema (tarea) o de un operador
        (orden administrativa): la función de la base rechaza cualquier otro actor.
        """
        first = month_of(self._clock.now())
        last = add_months(first, self._months_ahead) if until is None else month_of_date(until)
        if last < first:
            raise ValueError("until no puede ser anterior al mes en curso")
        if add_months(first, MAX_MONTHS_PER_CALL - 1) < last:
            raise ValueError(f"a lo sumo {MAX_MONTHS_PER_CALL} meses por llamada")
        rows = (
            await transaction.execute(_CREATE, {"first_month": first, "last_month": last})
        ).all()
        results = tuple(
            PartitionResult(
                table=PartitionedTable(row.parent),
                partition=str(row.partition),
                month=row.month,
                created=bool(row.created),
                blocked=bool(row.blocked),
            )
            for row in rows
        )
        default_rows = await self.report_default_rows(transaction)
        report = PartitionReport(first, last, results, default_rows)
        for result in report.created:
            _log.info("partición creada", partition=result.partition)
        for result in report.blocked:
            # La alarma de default_partition_rows ya está encendida: hace falta un operador.
            _log.error(
                "partición sin crear: la partición por defecto ya tiene filas de ese mes",
                partition=result.partition,
            )
        return report

    async def report_default_rows(self, transaction: Transaction) -> dict[PartitionedTable, int]:
        """Filas de cada partición por defecto, publicadas en ``default_partition_rows``."""
        rows = (await transaction.execute(_DEFAULT_ROWS)).all()
        counts = {PartitionedTable(row.parent): int(row.row_count) for row in rows}
        for table, count in counts.items():
            self._metrics.default_partition_rows.set(count, {"table": table.value})
            if count > 0:
                _log.error("la partición por defecto tiene filas", table=table.value, rows=count)
        return counts


def month_of_date(value: date) -> date:
    """El primer día del mes de ``value``."""
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("until debe ser una fecha")
    return date(value.year, value.month, 1)


# --- Registro de la tarea -----------------------------------------------------------------------


def create_partitions_handler(
    maintenance: PartitionMaintenance, provider_organization_id: uuid.UUID
) -> PeriodicHandler:
    """Manejador de ``create_partitions``: actúa solo en la iteración de la proveedora."""
    if type(provider_organization_id) is not uuid.UUID:
        raise TypeError("provider_organization_id debe ser uuid.UUID")

    async def handler(transaction: Transaction) -> None:
        if transaction.context.organization_id != provider_organization_id:
            return
        await maintenance.create(transaction)

    return handler


def register_create_partitions(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea semanal ``create_partitions`` de U-02 (``domain-entities.md`` §4.3)."""
    return registry.register(
        CREATE_PARTITIONS, CREATE_PARTITIONS_SCHEDULE, handler, unit=ActorUnit.U02
    )
