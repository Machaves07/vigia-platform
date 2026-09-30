"""Catálogo de prueba y base migrada para la bandeja de salida (TASK-111).

Lo comparten ``tests/properties/test_outbox_publish_atomicity.py`` y
``tests/integration/test_outbox_publish.py``:

- ``probe_catalog()``: los trece eventos de U-02 más tres de prueba: ``zone_probe`` (dos
  consumidores suscritos), ``bulk_probe`` (uno; carga de tamaño ajustable para el tope de 64 KB)
  y ``solo_probe`` (ninguno).
- ``app_database(migrated)``: el adaptador ``shared.db`` como ``vigia_app`` (nunca superusuario)
  sobre la base migrada.
- ``StatementFaults``: envuelve la conexión de una transacción abierta para contar sentencias y
  hacer fallar la n-ésima (excepción inyectada o conexión cortada por el servidor).
- ``outbox_counts``: eventos y entregas de una organización, leídos como superusuario (sin RLS).

Solo datos generados.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

import asyncpg  # type: ignore[import-untyped]
from pydantic import Field, StrictStr
from vigia_contracts.models.common import UUID, Timestamp

from tests.identity_db import MigratedDatabase
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import (
    ConnectionPort,
    Database,
    DatabaseSettings,
    ProcessKind,
    SslMode,
    Transaction,
)
from vigia_platform.shared.outbox.registries import Consumer, EventType, OutboxCatalog, PayloadModel
from vigia_platform.shared.outbox.u02_events import register_u02_event_types

PROBE_EVENT = "zone_probe"
BULK_EVENT = "bulk_probe"
SOLO_EVENT = "solo_probe"
CONSUMER_A = "probe_consumer_a"
CONSUMER_B = "probe_consumer_b"
SUBSCRIBERS: Mapping[str, tuple[str, ...]] = {
    PROBE_EVENT: (CONSUMER_A, CONSUMER_B),
    BULK_EVENT: (CONSUMER_A,),
    SOLO_EVENT: (),
}

SnakeCode = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
]


class ProbePayload(PayloadModel):
    zone_id: UUID
    state: Literal["observable", "degraded", "unobservable"]
    observed_at: Timestamp
    code: SnakeCode


class BulkPayload(PayloadModel):
    zone_ids: Annotated[list[UUID], Field(max_length=4000)]
    pad: Annotated[StrictStr, Field(max_length=1024, pattern=r"^[a-z]{0,1024}$")]


def probe_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "zone_id": str(uuid.uuid4()),
        "state": "observable",
        "observed_at": "2026-09-29T10:30:00.125Z",
        "code": "walk_test",
    }
    payload.update(changes)
    return payload


async def _handler(*_: Any) -> None:
    return None


def probe_catalog() -> OutboxCatalog:
    """Catálogo sin sellar: lo sella ``synchronize``."""
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    for name, model in (
        (PROBE_EVENT, ProbePayload),
        (BULK_EVENT, BulkPayload),
        (SOLO_EVENT, ProbePayload),
    ):
        catalog.event_types.register(
            EventType(
                event_name=name,
                publisher_unit=ActorUnit.U02,
                payload_model=model,
                description_es="Evento sintético de prueba",
            )
        )
    for consumer in (CONSUMER_A, CONSUMER_B):
        catalog.consumers.register(
            Consumer(
                consumer_name=consumer,
                unit=ActorUnit.U02,
                subscribed_events=tuple(e for e, c in SUBSCRIBERS.items() if consumer in c),
                handler=_handler,
            )
        )
    return catalog


def app_database(migrated: MigratedDatabase, **changes: Any) -> Database:
    """``shared.db`` como ``vigia_app`` (el rol de la aplicación, sin privilegios de dueño)."""
    fields: dict[str, Any] = {
        "url": migrated.as_role("vigia_app").sqlalchemy_url,
        "process": ProcessKind.WORKER,
        "sslmode": SslMode.DISABLE,  # el contenedor local no tiene TLS
        "worker_pool_size": 2,
    }
    fields.update(changes)
    return Database.create(DatabaseSettings(**fields))


class InjectedFault(Exception):
    """Fallo inyectado en mitad de una transacción."""


class StatementFaults:
    """Conexión envuelta: cuenta las sentencias y hace fallar la número ``fail_at`` (desde 1).

    ``before_failure`` se ejecuta justo antes (p. ej. cortar la conexión desde el servidor); sin
    él, la sentencia no se envía y se lanza ``InjectedFault``.
    """

    def __init__(
        self,
        connection: ConnectionPort,
        fail_at: int | None = None,
        before_failure: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._connection = connection
        self._fail_at = fail_at
        self._before_failure = before_failure
        self.statements = 0

    @classmethod
    def install(
        cls,
        transaction: Transaction,
        fail_at: int | None = None,
        before_failure: Callable[[], Awaitable[None]] | None = None,
    ) -> StatementFaults:
        wrapper = cls(transaction._connection, fail_at, before_failure)
        transaction._connection = wrapper  # type: ignore[assignment]
        return wrapper

    async def execute(self, statement: Any, parameters: Mapping[str, Any] | None = None) -> Any:
        self.statements += 1
        if self.statements == self._fail_at:
            if self._before_failure is None:
                raise InjectedFault(f"fallo inyectado en la sentencia {self.statements}")
            await self._before_failure()
        return await self._connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


async def outbox_counts(migrated: MigratedDatabase, organization_id: uuid.UUID) -> tuple[int, int]:
    """(eventos, entregas) de la organización, como superusuario del contenedor."""
    connection = await migrated.connect()
    try:
        events = await connection.fetchval(
            "SELECT count(*) FROM shared.outbox_event WHERE organization_id = $1", organization_id
        )
        deliveries = await connection.fetchval(
            "SELECT count(*) FROM shared.outbox_delivery WHERE organization_id = $1",
            organization_id,
        )
    finally:
        await connection.close()
    return int(events), int(deliveries)


async def terminate_backend(migrated: MigratedDatabase, pid: int) -> None:
    """Corta desde el servidor la conexión ``pid`` (como un reinicio o una conmutación)."""
    connection: Any = await asyncpg.connect(migrated.as_role().dsn)
    try:
        await connection.execute("SELECT pg_terminate_backend($1)", pid)
    finally:
        await connection.close()
