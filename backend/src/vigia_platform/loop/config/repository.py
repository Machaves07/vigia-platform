"""Configuración del lazo sobre PostgreSQL (``loop.plant_loop_configuration``, DE §2.13).

Una fila por planta: la proyección de sus registros ``loop_configuration_changed``. Solo
``INSERT`` (la versión 1, con los valores por defecto) y ``UPDATE`` de las columnas que la migración
concede a ``vigia_app``; nunca ``DELETE``.

**Exclusión** (``lock_plant``): ``pg_advisory_xact_lock`` sobre ``loop_configuration|<planta>``
antes de leer la versión vigente, en la creación de los valores por defecto y en cada cambio. Dos
cambios simultáneos de una planta se ordenan: el segundo lee la versión que dejó el primero y
escribe la siguiente. El respaldo es el ``UPDATE`` condicional (``WHERE version = :expected``):
si la fila ya no tiene la versión leída no cambia nada y ``update`` devuelve ``False``, así que dos
filas o dos registros con la misma ``version`` nunca se confirman.

Toda sentencia nombra la organización del contexto y la planta (defensa en profundidad sobre la
RLS).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import bindparam, insert, select, text, update
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.adapters.postgres.tables import PLANT_LOOP_CONFIGURATION as _TABLE
from vigia_platform.loop.calendar import BusinessCalendar
from vigia_platform.loop.config.parameters import (
    INTEGER_PARAMETERS,
    LoopSettings,
    PlantLoopConfiguration,
)
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresLoopConfigurationRepository"]

_INTEGERS: Final = tuple(INTEGER_PARAMETERS)
_UPDATED: Final = (
    *_INTEGERS,
    "business_calendar",
    "version",
    "changed_at",
    "changed_by",
    "change_reason",
)
"""Lo que cambia una versión nueva (nunca la planta, la organización ni ``timezone``)."""

_PLANT_TIMEZONE: Final = text(
    "SELECT timezone FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_LOCK_PLANT: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('loop_configuration|' || :plant_id, 0))"
)
_OF_PLANT: Final = (
    _TABLE.c.organization_id == bindparam("organization_id"),
    _TABLE.c.plant_id == bindparam("plant_id"),
)
_SELECT: Final = select(_TABLE).where(*_OF_PLANT)
_INSERT: Final = insert(_TABLE)
_UPDATE: Final = (
    update(_TABLE)
    .where(
        _TABLE.c.organization_id == bindparam("key_organization_id"),
        _TABLE.c.plant_id == bindparam("key_plant_id"),
        _TABLE.c.version == bindparam("expected_version"),
    )
    .values({column: bindparam(f"new_{column}") for column in _UPDATED})
)


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _configuration(row: Row[Any]) -> PlantLoopConfiguration:
    return PlantLoopConfiguration(
        plant_id=uuid.UUID(str(row.plant_id)),
        organization_id=uuid.UUID(str(row.organization_id)),
        settings=LoopSettings(
            integers={name: int(getattr(row, name)) for name in _INTEGERS},
            business_calendar=BusinessCalendar.from_document(_json(row.business_calendar)),
        ),
        timezone=str(row.timezone),
        version=int(row.version),
        changed_at=row.changed_at,
        changed_by=dict(_json(row.changed_by)),
        change_reason=str(row.change_reason),
    )


def _parameters(configuration: PlantLoopConfiguration) -> dict[str, Any]:
    return {
        "plant_id": configuration.plant_id,
        "organization_id": configuration.organization_id,
        **configuration.settings.integers,
        "business_calendar": configuration.business_calendar.document(),
        "timezone": configuration.timezone,
        "version": configuration.version,
        "changed_at": configuration.changed_at,
        "changed_by": dict(configuration.changed_by),
        "change_reason": configuration.change_reason,
    }


def _plant_key(context: ScopeContext, plant_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "plant_id": plant_id}


def _same_organization(transaction: Transaction, configuration: PlantLoopConfiguration) -> None:
    if configuration.organization_id != transaction.context.organization_id:
        raise ValueError("la configuración es de otra organización que la transacción")


@repository
class PostgresLoopConfigurationRepository:
    """La fila de configuración del lazo de cada planta."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def plant_timezone(self, context: ScopeContext, plant_id: uuid.UUID) -> str | None:
        """La zona de la planta si existe en la organización del contexto (y la RLS la deja
        ver); ``None`` si no."""
        rows = await self._database.read(context, _PLANT_TIMEZONE, _plant_key(context, plant_id))
        return str(rows[0].timezone) if rows else None

    async def read(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> PlantLoopConfiguration | None:
        """La fila vigente, fuera de una transacción de escritura."""
        rows = await self._database.read(context, _SELECT, _plant_key(context, plant_id))
        return _configuration(rows[0]) if rows else None

    async def lock_plant(self, transaction: Transaction, plant_id: uuid.UUID) -> None:
        """Exclusión entre cambios de configuración de la planta hasta el fin de la transacción."""
        await transaction.execute(_LOCK_PLANT, {"plant_id": str(plant_id)})

    async def current(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> PlantLoopConfiguration | None:
        """La fila vigente dentro de la transacción."""
        result = await transaction.execute(_SELECT, _plant_key(transaction.context, plant_id))
        row = result.first()
        return None if row is None else _configuration(row)

    async def settings_in(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> tuple[LoopSettings, int | None]:
        """Los parámetros vigentes de la planta y su versión, en la transacción de otro módulo.

        Es la interfaz que usan los módulos que **fijan** un plazo (cola, acciones, incerrables):
        lo calculan con lo que devuelve y lo guardan; un cambio posterior de configuración no lo
        mueve (BR-LAZ-145). Sin fila, los valores por defecto y versión ``None`` (no escribe).
        """
        current = await self.current(transaction, plant_id)
        if current is None:
            return LoopSettings.defaults(), None
        return current.settings, current.version

    async def insert(self, transaction: Transaction, configuration: PlantLoopConfiguration) -> None:
        """Crea la fila (la versión 1); una planta que ya la tiene sale como ``IntegrityError``."""
        _same_organization(transaction, configuration)
        await transaction.execute(_INSERT, _parameters(configuration))

    async def update(self, transaction: Transaction, configuration: PlantLoopConfiguration) -> bool:
        """Pasa la fila a ``configuration`` si sigue en la versión anterior; ``False`` si no."""
        _same_organization(transaction, configuration)
        values = _parameters(configuration)
        parameters = {
            "key_organization_id": transaction.context.organization_id,
            "key_plant_id": configuration.plant_id,
            **{f"new_{column}": values[column] for column in _UPDATED},
            "expected_version": configuration.version - 1,
        }
        result = await transaction.execute(_UPDATE, parameters)
        return int(getattr(result, "rowcount", 0) or 0) == 1
