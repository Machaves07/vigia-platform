"""``expire_enrollment_codes``: códigos de alta vencidos pasan a ``expired`` (LC-GOB-18).

Nota de cadencias de BL §2.6 (cierra P-GOB-9) y DE §3.2: cada 60 s, por organización (el
planificador de U-02 da a cada una su transacción y su contexto de iteración periódica; el
registro en la raíz es de ``fleet.registration``). Los códigos ``active`` con ``expires_at <= now``
pasan a ``expired`` con un ``UPDATE`` condicional a ``status = 'active'``
(``PostgresEnrollmentStore.expire_due``):

- **frente al alta** (TASK-219): el consumo es otro ``UPDATE`` condicional a ``status = 'active'``
  y ``expires_at > now``; el que llega segundo espera el candado de la fila y ya no la cumple. El
  código queda ``used`` o ``expired``, nunca las dos cosas, y un código vencido nunca deja una
  credencial (el consumo ya exige que siga vigente);
- **idempotente**: una ejecución repetida o solapada no encuentra nada que cambiar; nada se borra
  (el código vencido sigue en la tabla con su hash, P4);
- a lo sumo ``batch_size`` códigos por organización y ejecución `[objetivo propio]`: los demás, en
  la siguiente (un código vale 24 h; el lote solo acota una acumulación anómala).

El vencimiento no escribe en el expediente ni en la auditoría: ``expired`` es la consecuencia del
tiempo sobre un código cuya emisión ya está en el expediente (``enrollment_code_issued``). Ningún
paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import uuid
from typing import Final

from vigia_platform.fleet.adapters.postgres.enrollment_store import PostgresEnrollmentStore
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "EXPIRE_BATCH_SIZE",
    "EXPIRE_ENROLLMENT_CODES",
    "EXPIRE_ENROLLMENT_CODES_SCHEDULE",
    "EnrollmentCodeExpirer",
    "register_expire_enrollment_codes",
]

EXPIRE_ENROLLMENT_CODES: Final = "expire_enrollment_codes"
EXPIRE_ENROLLMENT_CODES_SCHEDULE: Final = Schedule.every(60)
"""Cada 60 s (nota de cadencias de BL §2.6; NFR-GOB-12)."""
EXPIRE_BATCH_SIZE: Final = 1000
"""Códigos vencidos por organización y ejecución `[objetivo propio]`."""


@repository
class EnrollmentCodeExpirer:
    """El manejador de ``expire_enrollment_codes`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        clock: Clock,
        store: PostgresEnrollmentStore | None = None,
        batch_size: int = EXPIRE_BATCH_SIZE,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._clock = clock
        self._store = store if store is not None else PostgresEnrollmentStore()
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "EnrollmentCodeExpirer()"

    async def expire(self, transaction: Transaction) -> tuple[uuid.UUID, ...]:
        """Los códigos de la organización de ``transaction`` que esta pasada dejó ``expired``."""
        # Sin redondear: ``expires_at`` sale del mismo reloj con toda su precisión, y redondear
        # hacia abajo dejaría vigente un código ya vencido (y al revés con el consumo).
        return await self._store.expire_due(transaction, self._clock.now(), limit=self._batch_size)


def register_expire_enrollment_codes(
    registry: PeriodicTaskRegistry, expirer: EnrollmentCodeExpirer
) -> PeriodicTask:
    """Registra ``expire_enrollment_codes`` cada 60 s, por organización (lo llama la raíz)."""

    async def handler(transaction: Transaction) -> None:
        await expirer.expire(transaction)

    return registry.register(
        EXPIRE_ENROLLMENT_CODES, EXPIRE_ENROLLMENT_CODES_SCHEDULE, handler, unit=ActorUnit.U03
    )
