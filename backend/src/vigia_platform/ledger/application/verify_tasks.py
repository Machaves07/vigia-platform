"""Tareas periódicas ``verify_chains_incremental`` y ``verify_chains_full`` (LC-NUC-13; BR-NUC-56).

- ``verify_chains_incremental``: diaria a la 01:00 UTC ``[objetivo propio]``, una hora después de
  ``write_checkpoints`` (00:00), para que la verificación del día cubra el punto de control recién
  escrito. Cada cadena parte del último registro verificado íntegro.
- ``verify_chains_full``: mensual, el día 1 a las 02:00 UTC ``[objetivo propio]``, desde la génesis
  y con la forma canónica del 100 %.

El planificador (TASK-130) invoca cada tarea **una vez por organización**, con su contexto de
iteración periódica y su arrendamiento (PAT-NUC-RES-05), en el worker: allí ``statement_timeout``
es de 30 s. El manejador no escribe en la transacción del planificador: cada resultado se audita
en la suya (``IntegrityStore.record``). Una cadena rota no detiene a las demás; una base caída sí
(sin resultado no se audita nada y la pasada siguiente lo reintenta).
"""

from __future__ import annotations

from typing import Final

from vigia_platform.ledger.chain.verify import IntegrityService, VerificationMode
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)

__all__ = [
    "VERIFY_CHAINS_FULL",
    "VERIFY_CHAINS_FULL_SCHEDULE",
    "VERIFY_CHAINS_INCREMENTAL",
    "VERIFY_CHAINS_INCREMENTAL_SCHEDULE",
    "register_verify_chains",
    "verify_chains_handler",
]

VERIFY_CHAINS_INCREMENTAL: Final = "verify_chains_incremental"
VERIFY_CHAINS_INCREMENTAL_SCHEDULE: Final = Schedule.daily(hour=1)
VERIFY_CHAINS_FULL: Final = "verify_chains_full"
VERIFY_CHAINS_FULL_SCHEDULE: Final = Schedule.monthly(day=1, hour=2)


def verify_chains_handler(service: IntegrityService, mode: VerificationMode) -> PeriodicHandler:
    """Manejador que verifica todas las cadenas de la organización en ``mode``."""

    async def handler(transaction: Transaction) -> None:
        await service.verify_all(transaction.context, mode)

    return handler


def register_verify_chains(
    registry: PeriodicTaskRegistry, service: IntegrityService
) -> tuple[PeriodicTask, PeriodicTask]:
    """Registra las dos tareas de verificación de U-02 (``domain-entities.md`` §4.3)."""
    incremental = registry.register(
        VERIFY_CHAINS_INCREMENTAL,
        VERIFY_CHAINS_INCREMENTAL_SCHEDULE,
        verify_chains_handler(service, VerificationMode.INCREMENTAL),
        unit=ActorUnit.U02,
    )
    full = registry.register(
        VERIFY_CHAINS_FULL,
        VERIFY_CHAINS_FULL_SCHEDULE,
        verify_chains_handler(service, VerificationMode.FULL),
        unit=ActorUnit.U02,
    )
    return incremental, full
