"""Entorno de prueba de ``vigia-worker`` (TASK-130).

Lo comparten ``tests/properties/test_leases_stateful.py``,
``tests/integration/test_worker_iteration.py``
y el proceso de trabajo real que lanza esta última (``tests/worker_process.py``):

- ``WorkerEnvironment``: base migrada, ``shared.db`` como ``vigia_app`` (nunca superusuario) con el
  pool del worker, ``ScopeContexts`` del sistema (sin leer la base), reloj simulado y lecturas y
  siembras como superusuario (sin RLS): organizaciones, tareas de ``shared.periodic_task`` y su
  arrendamiento y avance.
- ``ProbeTask``: el manejador de la tarea de prueba ``worker_probe``. Por cada organización
  publica en **la transacción de la organización** un evento ``worker_probe_effect`` (el efecto
  en la base: solo queda si la organización se confirma), anota cada invocación con la
  organización y el contexto, y puede fallar en organizaciones elegidas o morir (``SimulatedCrash``,
  ``BaseException``, como una señal no capturable) en una.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StrictStr
from vigia_contracts.models.common import UUID

from tests.factories import make_context
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import app_database
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ContextOrigin
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.outbox.publish import NewEvent, Outbox
from vigia_platform.shared.outbox.registries import (
    EventType,
    OutboxCatalog,
    PayloadModel,
    Schedule,
)
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.secrets import DataKey
from vigia_platform.shared.signing.keys import SigningPurpose
from vigia_platform.shared.storage import ObjectHead

PROBE_TASK = "worker_probe"
EFFECT_EVENT: Literal["worker_probe_effect"] = "worker_probe_effect"
START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

SnakeCode = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
]


class EffectPayload(PayloadModel):
    """Solo identificadores: la organización que se procesó y la ejecución."""

    organization_id: UUID
    task: SnakeCode


class ProbeDefect(Exception):
    """Fallo de la tarea en una organización: se registra y la tarea sigue."""

    code = "probe_defect"


class SimulatedCrash(BaseException):
    """El proceso muere (señal no capturable) en mitad de una organización."""


@dataclass(frozen=True, slots=True)
class Invocation:
    organization_id: uuid.UUID
    context_organization_id: uuid.UUID
    actor_kind: ActorKind
    origin: ContextOrigin


@dataclass
class ProbeTask:
    """Manejador guionizado de ``worker_probe``."""

    outbox: Outbox | None = None
    fail_in: set[uuid.UUID] = field(default_factory=set)
    crash_in: set[uuid.UUID] = field(default_factory=set)
    invocations: list[Invocation] = field(default_factory=list)
    after: Callable[[Transaction], Awaitable[None]] | None = None
    """Se ejecuta tras el efecto, en la misma transacción (la prueba inyecta ahí su suceso)."""

    def reset(self) -> None:
        self.fail_in.clear()
        self.crash_in.clear()
        self.invocations.clear()
        self.after = None

    async def __call__(self, transaction: Transaction) -> None:
        context = transaction.context
        organization_id = context.organization_id
        self.invocations.append(
            Invocation(organization_id, organization_id, context.actor.kind, context.origin)
        )
        assert self.outbox is not None
        await self.outbox.publish(
            transaction,
            NewEvent(
                event_name=EFFECT_EVENT,
                payload={"organization_id": str(organization_id), "task": PROBE_TASK},
            ),
        )
        if self.after is not None:
            await self.after(transaction)
        if organization_id in self.fail_in:
            raise ProbeDefect("fallo inyectado")
        if organization_id in self.crash_in:
            raise SimulatedCrash()


def worker_catalog(probe: ProbeTask, schedule: Schedule | None = None) -> OutboxCatalog:
    """Los eventos y el consumidor de U-02 (``alert_metrics``, como el worker real), el evento del
    efecto de prueba y la tarea ``worker_probe`` (sin sellar). Todas las pruebas de una base
    sincronizan el mismo catálogo: un consumidor persistido sin registrar no arranca."""
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    register_alerts_consumer(catalog.consumers)
    catalog.event_types.register(
        EventType(
            event_name=EFFECT_EVENT,
            publisher_unit=ActorUnit.U02,
            payload_model=EffectPayload,
            description_es="Efecto sintético de la tarea de prueba del worker",
        )
    )
    catalog.periodic_tasks.register(
        PROBE_TASK, schedule or Schedule.every(3600), probe, unit=ActorUnit.U02
    )
    return catalog


class NoContextStore:
    """Los contextos del sistema y de iteración no leen la base."""

    async def session_row(self, *_: Any) -> None:
        raise AssertionError("el worker no construye contextos de sesión")

    async def operator_row(self, *_: Any) -> None:
        raise AssertionError("el worker no construye contextos de operador")


def system_contexts(
    clock: SimulatedClock | Any, provider_organization_id: uuid.UUID
) -> ScopeContexts:
    return ScopeContexts(
        store=NoContextStore(),
        clock=clock,
        provider_organization_id=provider_organization_id,
        system_actor_id=uuid.uuid4(),
    )


@dataclass
class TaskRow:
    next_run_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    last_outcome: str | None
    last_success_at: datetime | None
    progress_run_at: datetime | None
    progress_organization_id: uuid.UUID | None
    progress_failures: int


@dataclass
class WorkerEnvironment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    database: Database
    clock: SimulatedClock
    contexts: ScopeContexts
    provider_organization_id: uuid.UUID
    extra_databases: list[Database] = field(default_factory=list)
    admin: Any = None

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def new_database(self) -> Database:
        """Otro pool, como el de otro proceso de trabajo."""
        database = app_database(self.migrated, worker_pool_size=4)
        self.extra_databases.append(database)
        return database

    async def _admin(self) -> Any:
        if self.admin is None or self.admin.is_closed():
            self.admin = await self.migrated.connect()
        return self.admin

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list(await (await self._admin()).fetch(sql, *args))

    async def execute(self, sql: str, *args: Any) -> None:
        await (await self._admin()).execute(sql, *args)

    async def add_organizations(
        self, count: int, *, status: str = "active", kind: str = "client"
    ) -> list[uuid.UUID]:
        """``count`` organizaciones nuevas, en orden de ``organization_id``."""
        created = sorted(uuid.uuid4() for _ in range(count))
        admin = await self._admin()
        for organization_id in created:
            creator = uuid.uuid4()
            # La organización y quien la creó van juntas: ``created_by`` es diferible.
            async with admin.transaction():
                await admin.execute(
                    "INSERT INTO identity.organization"
                    " (organization_id, code, name, kind, status, created_at, created_by)"
                    " VALUES ($1, $2, 'Organización sintética', $3, $4, $5, $6)",
                    organization_id,
                    f"ORG-{uuid.uuid4().hex[:8].upper()}",
                    kind,
                    status,
                    START,
                    creator,
                )
                await admin.execute(
                    "INSERT INTO identity.user_account"
                    " (user_id, organization_id, email, display_name, status, created_at)"
                    " VALUES ($1, $2, $3, 'Usuario sintético', 'active', $4)",
                    creator,
                    organization_id,
                    f"u{creator.hex[:12]}@example.test",
                    START,
                )
        return created

    async def suspend_all_organizations(self) -> None:
        """Deja fuera de la iteración las organizaciones de ejemplos anteriores."""
        await self.execute("UPDATE identity.organization SET status = 'suspended'")

    async def put_task(self, task_name: str, next_run_at: datetime) -> None:
        """La fila de ``task_name`` sin arrendamiento ni avance, que vence en ``next_run_at``."""
        await self.execute(
            "INSERT INTO shared.periodic_task (task_name, unit, schedule, next_run_at)"
            " VALUES ($1, 'U-02', 'every:3600s', $2)"
            " ON CONFLICT (task_name) DO UPDATE SET next_run_at = EXCLUDED.next_run_at,"
            " lease_owner = NULL, lease_until = NULL, last_outcome = NULL, last_run_at = NULL,"
            " last_success_at = NULL, progress_run_at = NULL, progress_organization_id = NULL,"
            " progress_failures = 0",
            task_name,
            next_run_at,
        )

    async def task_row(self, task_name: str) -> TaskRow:
        (row,) = await self.fetch(
            "SELECT next_run_at, lease_owner, lease_until, last_outcome, last_success_at,"
            " progress_run_at, progress_organization_id, progress_failures"
            " FROM shared.periodic_task WHERE task_name = $1",
            task_name,
        )
        return TaskRow(**dict(row))

    async def effects(self, since: datetime | None = None) -> list[uuid.UUID]:
        """Organizaciones con efecto confirmado de ``worker_probe``, en orden de publicación."""
        rows = await self.fetch(
            "SELECT payload->>'organization_id' AS organization FROM shared.outbox_event"
            " WHERE event_name = $1 AND ($2::timestamptz IS NULL OR created_at >= $2)"
            " ORDER BY publish_seq",
            EFFECT_EVENT,
            since,
        )
        return [uuid.UUID(row["organization"]) for row in rows]


async def synchronize(database: Database, catalog: OutboxCatalog, clock: Any) -> None:
    async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
        await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)


@contextmanager
def worker_environment(
    postgres_endpoint: PostgresEndpoint, prefix: str
) -> Iterator[WorkerEnvironment]:
    loop = DatabaseLoop()
    with migrated_database(postgres_endpoint, prefix) as migrated:
        database = app_database(migrated, worker_pool_size=4)
        clock = SimulatedClock(START)
        provider = uuid.uuid4()
        environment = WorkerEnvironment(
            loop=loop,
            migrated=migrated,
            database=database,
            clock=clock,
            contexts=system_contexts(clock, provider),
            provider_organization_id=provider,
        )
        try:
            yield environment
        finally:
            if environment.admin is not None:
                loop.run(environment.admin.close())
            for extra in environment.extra_databases:
                loop.run(extra.dispose())
            loop.run(database.dispose())
            loop.close()


# --- Dobles de las comprobaciones de arranque que la prueba no ejercita ---------------------


class StubSigning:
    """Firma con clave activa para cada propósito (la comprobación ``signing_keys`` pasa)."""

    ready = True

    async def start(self) -> None:
        return None

    def has_active_key(self, purpose: SigningPurpose) -> bool:
        return True

    async def run_refresh(self, stop: asyncio.Event) -> None:
        await stop.wait()


class StubKms:
    """KMS que genera y descifra una clave de datos fija (la comprobación ``data_key`` pasa)."""

    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey:
        return DataKey(plaintext=b"k" * 32, wrapped=b"w" * 32, key_id=key_id)

    async def decrypt(self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]) -> bytes:
        return b"k" * 32


class StubStorage:
    """Almacén con el objeto centinela (la comprobación ``storage`` pasa)."""

    async def head_object(self, key: str) -> ObjectHead:
        return ObjectHead(
            key=key,
            size_bytes=0,
            checksum_sha256=None,
            checksum_type=None,
            content_type=None,
            metadata={},
            version_id=None,
        )
