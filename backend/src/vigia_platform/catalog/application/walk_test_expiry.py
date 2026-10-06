"""``expire_walk_test_sessions``: sesiones de walk-test sin actividad pasan a ``incomplete``.

BL §3.3, NFR-GOB-39 y LC-GOB-06/18 (TASK-227). Una vez al día, por organización (el planificador de
U-02 da a cada una su transacción y su contexto de iteración periódica; el registro en la raíz es
de ``fleet.registration``):

- las sesiones ``in_progress`` o ``reopened`` con ``last_activity_at`` de hace **7 días o más**
  (``INACTIVITY_LIMIT``, el mismo borde que ``expire_if_inactive`` aplica en cada operación de la
  sesión) pasan a ``incomplete`` (``PostgresWalkTestRepository.expire_inactive``): solo cambia el
  estado; matriz, pasos, pases y pruebas de oclusión se conservan y nada se borra (P4). La
  reapertura con motivo es de LC-GOB-06 (``WalkTestService.reopen``);
- **una sola vez**: la escritura es condicional al estado y a la inactividad, después de bloquear
  las filas en orden de ``session_id``. Dos ejecuciones solapadas se serializan en esos candados y
  la segunda no cambia ninguna; una operación de la sesión que llegó antes (``touch`` bajo el
  candado de la fila) la deja fuera;
- **métrica** (NFR-GOB-56): sesiones abiertas e incompletas de la organización al terminar
  (``walk_test_sessions_open`` y ``walk_test_sessions_incomplete``), dentro de la transacción de la
  organización.

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.domain.walk_test import INACTIVITY_LIMIT
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "EXPIRE_BATCH_SIZE",
    "EXPIRE_WALK_TEST_SESSIONS",
    "EXPIRE_WALK_TEST_SESSIONS_SCHEDULE",
    "WalkTestExpirer",
    "WalkTestExpiryReport",
    "register_expire_walk_test_sessions",
]

EXPIRE_WALK_TEST_SESSIONS: Final = "expire_walk_test_sessions"
EXPIRE_WALK_TEST_SESSIONS_SCHEDULE: Final = Schedule.daily(hour=5)
"""Diaria (BL §3.3; NFR-GOB-12), a las 05:00 UTC `[objetivo propio]`: fuera de las horas de las
otras tareas diarias (00:00 a 04:00)."""
EXPIRE_BATCH_SIZE: Final = 1000
"""Sesiones por organización y ejecución `[objetivo propio]`: a lo sumo una abierta por zona."""


@dataclass(frozen=True, slots=True)
class WalkTestExpiryReport:
    """Lo que hizo una pasada en una organización."""

    expired: tuple[uuid.UUID, ...]
    open_sessions: int
    incomplete_sessions: int


@repository
class WalkTestExpirer:
    """El manejador de ``expire_walk_test_sessions`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        repository: PostgresWalkTestRepository | None = None,
        batch_size: int = EXPIRE_BATCH_SIZE,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._clock = clock
        self._metrics = metrics
        self._repository = repository if repository is not None else PostgresWalkTestRepository()
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "WalkTestExpirer()"

    async def expire(self, transaction: Transaction) -> WalkTestExpiryReport:
        """Una pasada sobre las sesiones de la organización de ``transaction``."""
        # El mismo borde que ``expire_if_inactive`` (``now - last_activity_at >= 7 días``), sin
        # redondear ``now``: ``last_activity_at`` sale del mismo reloj con toda su precisión.
        expired = await self._repository.expire_inactive(
            transaction, self._clock.now() - INACTIVITY_LIMIT, limit=self._batch_size
        )
        open_sessions, incomplete = await self._repository.session_counts(transaction)
        metrics = self._metrics if self._metrics is not None else get_metrics()
        organization = {"organization_id": str(transaction.context.organization_id)}
        metrics.walk_test_sessions_open.set(open_sessions, organization)
        metrics.walk_test_sessions_incomplete.set(incomplete, organization)
        return WalkTestExpiryReport(expired, open_sessions, incomplete)


def register_expire_walk_test_sessions(
    registry: PeriodicTaskRegistry, expirer: WalkTestExpirer
) -> PeriodicTask:
    """Registra ``expire_walk_test_sessions`` una vez al día, por organización (la raíz)."""

    async def handler(transaction: Transaction) -> None:
        await expirer.expire(transaction)

    return registry.register(
        EXPIRE_WALK_TEST_SESSIONS, EXPIRE_WALK_TEST_SESSIONS_SCHEDULE, handler, unit=ActorUnit.U03
    )
