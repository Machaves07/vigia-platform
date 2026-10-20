"""Tarea periódica ``monthly_package`` (TASK-342; LC-LAZ-24; PAT-LAZ-RES-05; BR-LAZ-102 y 141).

Una vez al día y por organización (la raíz entrega la transacción de cada organización, barata y
de solo lectura aquí), recorre sus zonas en orden de planta y de zona y, para cada una, los
periodos («AAAA-MM») que le faltan: ya vencidos según ``monthly_package_generation_day`` de la
configuración de su planta, en su zona horaria local, y todavía no sellados. Los procesa **uno a
uno, del más antiguo al más reciente** (PAT-LAZ-RES-05: «procesa los periodos que le faltan, uno
por uno»), con su propia composición (``monthly.package.MonthlyPackageComposer``), que abre sus
propias transacciones cortas de reclamo y de sellado —independientes de la transacción barata de
esta tarea.

``BR-LAZ-141`` («el fallo en una no detiene a las demás»): si compone un periodo y falla, se
registra el fallo y se abandona el resto de periodos **de esa zona** en este ciclo (se reintentan
al día siguiente, porque siguen sin sellar); el ciclo sigue con la siguiente zona, y una zona sin
configuración de lazo todavía (``plant_config is None``) simplemente se omite, sin romper el
ciclo de la organización.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

from vigia_platform.accreditation.monthly.package import MonthlyPackageComposer
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository, ZoneRef
from vigia_platform.loop.calendar import today
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "MAX_CATCHUP_MONTHS",
    "MONTHLY_PACKAGE",
    "MONTHLY_PACKAGE_SCHEDULE",
    "MonthlyPackageTask",
    "register_monthly_package",
]

MONTHLY_PACKAGE: Final = "monthly_package"
MONTHLY_PACKAGE_SCHEDULE: Final = Schedule.daily(hour=4)
"""Diaria a las 04:00 UTC ``[estimación propia]``: solo arranca el ciclo. El día real en el que
se genera el paquete de cada planta lo decide, en su zona horaria local y con el ``Clock``
inyectado, ``monthly_package_generation_day`` de su configuración del lazo (``today``), no esta
hora de arranque."""

MAX_CATCHUP_MONTHS: Final = 36
"""Meses hacia atrás que una recuperación revisa tras una interrupción larga ``[estimación
propia]``: diez años de catch-up no tienen sentido operativo y acotan el trabajo de un ciclo."""

_log = get_logger("accreditation.monthly")


def _period_of_month(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """``(year, month)`` desplazado ``delta`` meses (puede ser negativo)."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _due_date(period_year: int, period_month: int, generation_day: int) -> date:
    """El día en que el paquete del mes ``period_year-period_month`` queda vencido: el
    ``generation_day`` del mes siguiente, en el calendario local."""
    next_year, next_month = _add_months(period_year, period_month, 1)
    return date(next_year, next_month, 1) + timedelta(days=generation_day - 1)


def _due_periods(
    today_local: date, generation_day: int, already_sealed: frozenset[str], max_months: int
) -> list[str]:
    """Los periodos (``"AAAA-MM"``) que le faltan a una zona, del más antiguo al más reciente.

    Función **pura**: solo depende de la fecha local de hoy, el día de generación configurado,
    los periodos ya sellados y el tope de recuperación. No intenta detectar «huecos»: calcula,
    dentro de ``max_months`` hacia atrás, qué meses ya están vencidos y no sellados, y los
    devuelve en orden cronológico (PAT-LAZ-RES-05).
    """
    due: list[str] = []
    for offset in range(0, max_months + 1):
        year, month = _add_months(today_local.year, today_local.month, -offset)
        period = _period_of_month(year, month)
        if period in already_sealed:
            continue
        if _due_date(year, month, generation_day) <= today_local:
            due.append(period)
    return sorted(due)


@repository
class MonthlyPackageTask:
    """El manejador de ``monthly_package`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        config: PostgresLoopConfigurationRepository,
        repository: MonthlyPackageRepository,
        composer: MonthlyPackageComposer,
        clock: Clock,
        max_catchup_months: int = MAX_CATCHUP_MONTHS,
    ) -> None:
        if type(max_catchup_months) is not int or max_catchup_months < 1:
            raise ValueError("max_catchup_months debe ser al menos 1")
        self._config = config
        self._repository = repository
        self._composer = composer
        self._clock = clock
        self._max_catchup_months = max_catchup_months

    def __repr__(self) -> str:
        return "MonthlyPackageTask()"

    async def __call__(self, transaction: Transaction) -> None:
        """Delegado sin guarda (``@repository`` no vigila ``__call__``): ver ``run``."""
        await self.run(transaction)

    async def run(self, transaction: Transaction) -> None:
        """Una pasada sobre todas las zonas de la organización de ``transaction``."""
        zones = await self._repository.zones_of_organization(transaction)
        for zone in zones:
            await self._process_zone(transaction, zone)

    async def _process_zone(self, transaction: Transaction, zone: ZoneRef) -> None:
        context = transaction.context
        plant_config = await self._config.current(transaction, zone.plant_id)
        if plant_config is None:
            # Sin configuración del lazo todavía: nada que generar, y no rompe el ciclo.
            return
        today_local = today(self._clock, plant_config.timezone)
        generation_day = plant_config.value("monthly_package_generation_day")
        already_sealed = await self._repository.sealed_periods(transaction, zone.zone_id)
        due = _due_periods(today_local, generation_day, already_sealed, self._max_catchup_months)
        if not due:
            return
        sample_percent = plant_config.value("sample_percent")
        sample_min = plant_config.value("sample_min")
        sample_max = plant_config.value("sample_max")
        for period in due:
            try:
                await self._composer.compose_and_seal(
                    context,
                    zone,
                    period,
                    plant_config.timezone,
                    sample_percent=sample_percent,
                    sample_min=sample_min,
                    sample_max=sample_max,
                )
            except Exception:
                # BR-LAZ-141: el fallo en un periodo no detiene a las demás zonas; esta zona
                # reintenta el resto de sus periodos pendientes en el próximo ciclo (siguen sin
                # sellar), así que basta con abandonar el resto de esta zona en este ciclo.
                _log.exception(
                    "fallo al componer el paquete mensual: se sigue con la siguiente zona",
                    zone_id=str(zone.zone_id),
                    period=period,
                )
                return


def register_monthly_package(
    registry: PeriodicTaskRegistry, task: MonthlyPackageTask
) -> PeriodicTask:
    """Registra ``monthly_package`` una vez al día, por organización (la raíz)."""

    async def handler(transaction: Transaction) -> None:
        await task(transaction)

    return registry.register(MONTHLY_PACKAGE, MONTHLY_PACKAGE_SCHEDULE, handler, unit=ActorUnit.U04)
