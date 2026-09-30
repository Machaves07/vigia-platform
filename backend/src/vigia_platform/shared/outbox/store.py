"""Adaptador PostgreSQL de ``OutboxCatalogStore`` (tablas globales del esquema ``shared``).

``shared.event_type``, ``shared.consumer`` y ``shared.periodic_task`` son registros globales de la
plataforma (domain-entities §6): no llevan organización ni seguridad a nivel de fila. Aun así se
leen y escriben dentro de una ``Transaction`` de ``shared.db``, el único camino a la base. El
arranque la abre, llama a ``OutboxCatalog.synchronize`` y confirma: el catálogo queda en la base
entero o no queda.

``vigia_app`` tiene ``SELECT``, ``INSERT`` y ``UPDATE`` sobre las tres (nuc_0001), nunca
``DELETE``. Al guardar un consumidor no se toca su circuito, y al guardar una tarea no se toca su
arrendamiento ni su avance: esas columnas son del despachador y del planificador.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import (
    PersistedConsumer,
    PersistedEventType,
    PersistedPeriodicTask,
)

__all__ = ["SqlOutboxCatalogStore"]

_LOAD_EVENT_TYPES: Final = text(
    "SELECT event_name, publisher_unit, payload_schema, description_es FROM shared.event_type"
)
_LOAD_CONSUMERS: Final = text(
    "SELECT consumer_name, unit, subscribed_events, has_external_dependency FROM shared.consumer"
)
_LOAD_PERIODIC_TASKS: Final = text("SELECT task_name, unit, schedule FROM shared.periodic_task")

_SAVE_EVENT_TYPE: Final = text(
    "INSERT INTO shared.event_type (event_name, publisher_unit, payload_schema, description_es)"
    " VALUES (:event_name, :publisher_unit, CAST(:payload_schema AS jsonb), :description_es)"
    " ON CONFLICT (event_name) DO UPDATE SET payload_schema = EXCLUDED.payload_schema,"
    " description_es = EXCLUDED.description_es"
)
_SAVE_CONSUMER: Final = text(
    "INSERT INTO shared.consumer (consumer_name, unit, subscribed_events,"
    " has_external_dependency)"
    " VALUES (:consumer_name, :unit, CAST(:subscribed_events AS text[]),"
    " :has_external_dependency)"
    " ON CONFLICT (consumer_name) DO UPDATE SET subscribed_events = EXCLUDED.subscribed_events,"
    " has_external_dependency = EXCLUDED.has_external_dependency"
)
_SAVE_PERIODIC_TASK: Final = text(
    "INSERT INTO shared.periodic_task (task_name, unit, schedule, iterates_organizations,"
    " next_run_at)"
    " VALUES (:task_name, :unit, :schedule, true, :next_run_at)"
    " ON CONFLICT (task_name) DO UPDATE SET schedule = EXCLUDED.schedule,"
    " next_run_at = EXCLUDED.next_run_at"
)


class SqlOutboxCatalogStore:
    """El puerto sobre una transacción abierta; no la confirma."""

    def __init__(self, transaction: Transaction) -> None:
        self._transaction = transaction

    async def _rows(self, statement: Any) -> list[Mapping[str, Any]]:
        result = await self._transaction.execute(statement)
        return [dict(row) for row in result.mappings().all()]

    async def load_event_types(self) -> Mapping[str, PersistedEventType]:
        return {
            row["event_name"]: PersistedEventType(
                event_name=row["event_name"],
                publisher_unit=row["publisher_unit"],
                payload_schema=row["payload_schema"],
                description_es=row["description_es"],
            )
            for row in await self._rows(_LOAD_EVENT_TYPES)
        }

    async def load_consumers(self) -> Mapping[str, PersistedConsumer]:
        return {
            row["consumer_name"]: PersistedConsumer(
                consumer_name=row["consumer_name"],
                unit=row["unit"],
                subscribed_events=tuple(sorted(row["subscribed_events"])),
                has_external_dependency=row["has_external_dependency"],
            )
            for row in await self._rows(_LOAD_CONSUMERS)
        }

    async def load_periodic_tasks(self) -> Mapping[str, PersistedPeriodicTask]:
        return {
            row["task_name"]: PersistedPeriodicTask(
                task_name=row["task_name"], unit=row["unit"], schedule=row["schedule"]
            )
            for row in await self._rows(_LOAD_PERIODIC_TASKS)
        }

    async def save_event_type(self, row: PersistedEventType) -> None:
        await self._transaction.execute(
            _SAVE_EVENT_TYPE,
            {
                "event_name": row.event_name,
                "publisher_unit": row.publisher_unit,
                "payload_schema": json.dumps(row.payload_schema, allow_nan=False),
                "description_es": row.description_es,
            },
        )

    async def save_consumer(self, row: PersistedConsumer) -> None:
        await self._transaction.execute(
            _SAVE_CONSUMER,
            {
                "consumer_name": row.consumer_name,
                "unit": row.unit,
                "subscribed_events": list(row.subscribed_events),
                "has_external_dependency": row.has_external_dependency,
            },
        )

    async def save_periodic_task(self, row: PersistedPeriodicTask, next_run_at: datetime) -> None:
        await self._transaction.execute(
            _SAVE_PERIODIC_TASK,
            {
                "task_name": row.task_name,
                "unit": row.unit,
                "schedule": row.schedule,
                "next_run_at": next_run_at,
            },
        )
