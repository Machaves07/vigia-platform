"""Tarea periódica ``write_checkpoints`` de U-02 (LC-NUC-14; BR-NUC-53).

Diaria a las 00:00 UTC. El planificador (TASK-130) la invoca **una vez por organización**, cada
una en su transacción con su contexto de iteración periódica, y lleva el avance por organización
con su arrendamiento (PAT-NUC-RES-05): si el proceso cae a mitad, la siguiente pasada retoma las
organizaciones pendientes. Por cada organización se anexa un punto de control a cada cadena de
expediente (planta y organización) y a la de auditoría; ``CheckpointService`` no escribe otro
sobre la misma cabeza, así que repetir la invocación no cambia nada.

La transacción del planificador no se usa para escribir: cada punto de control va en la suya
(por ``EscritorExpediente`` o ``AuditWriter``), para que un conflicto con la cabeza se reintente
solo en esa cadena.
"""

from __future__ import annotations

from typing import Final

from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)

__all__ = [
    "WRITE_CHECKPOINTS",
    "WRITE_CHECKPOINTS_SCHEDULE",
    "register_write_checkpoints",
    "write_checkpoints_handler",
]

WRITE_CHECKPOINTS: Final = "write_checkpoints"
WRITE_CHECKPOINTS_SCHEDULE: Final = Schedule.daily(hour=0)
"""Diaria a las 00:00 UTC (BR-NUC-53)."""


def write_checkpoints_handler(service: CheckpointService) -> PeriodicHandler:
    """Manejador de ``write_checkpoints`` para ``PeriodicTaskRegistry``."""

    async def handler(transaction: Transaction) -> None:
        await service.write_checkpoints_now(transaction.context)

    return handler


def register_write_checkpoints(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea diaria ``write_checkpoints`` de U-02 (``domain-entities.md`` §4.3)."""
    return registry.register(
        WRITE_CHECKPOINTS, WRITE_CHECKPOINTS_SCHEDULE, handler, unit=ActorUnit.U02
    )
