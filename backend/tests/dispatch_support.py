"""Entorno de prueba del despachador de la bandeja (TASK-129).

Lo comparten ``tests/properties/test_outbox_stateful.py``, ``test_partition_sharing.py`` y
``tests/integration/test_outbox_dispatch.py``:

- ``dispatch_catalog()``: los eventos de U-02, ``dispatch_probe`` (lo que se entrega),
  ``dispatch_echo`` (sin suscriptores: el efecto **en la base** de un manejador) y dos
  consumidores con manejador guionizado: ``dispatch_plain`` (sin dependencia externa) y
  ``dispatch_external`` (con ella), más ``alert_metrics``.
- ``ScriptedHandler``: el manejador de prueba. Cada invocación se registra (evento, partición,
  organización y ``correlation_id`` del contexto de la transacción) y su comportamiento sale del
  guion del evento o del modo global: ``ok``, ``defect`` (publica el eco y lanza: el
  ``SAVEPOINT`` debe deshacerlo), ``dependency_down`` (``ExternalDependencyDown``) o
  ``crash_after_effect`` (efecto externo hecho y caída del proceso antes de confirmar:
  ``SimulatedCrash`` es ``BaseException``, como una señal no capturable). El efecto externo es
  idempotente por ``event_id`` (BR-NUC-76) y se cuenta aparte del efecto en la base.
- ``DispatchEnvironment``: base migrada, ``Outbox`` y ``Dispatcher`` como ``vigia_app``, reloj
  simulado y lecturas como superusuario (sin RLS) de entregas, cola muerta, ecos y circuitos.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.factories import make_context, uuid7
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import ProbePayload, app_database, probe_payload
from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.api.errors import ExternalDependencyDown
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics
from vigia_platform.shared.outbox.dispatcher import Dispatcher
from vigia_platform.shared.outbox.publish import NewEvent, Outbox, OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, EventType, OutboxCatalog
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types

PROBE: Literal["dispatch_probe"] = "dispatch_probe"
ECHO: Literal["dispatch_echo"] = "dispatch_echo"
PLAIN = "dispatch_plain"
EXTERNAL = "dispatch_external"
SCRIPTED_CONSUMERS = (PLAIN, EXTERNAL)
START = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


class Behavior(enum.StrEnum):
    OK = "ok"
    DEFECT = "defect"
    DEPENDENCY_DOWN = "dependency_down"
    CRASH_AFTER_EFFECT = "crash_after_effect"


class HandlerDefect(Exception):
    """Defecto del manejador: cuenta como intento."""

    code = "probe_defect"


class SimulatedCrash(BaseException):
    """El proceso muere (señal no capturable) después del efecto y antes de confirmar."""


@dataclass(frozen=True, slots=True)
class Invocation:
    consumer: str
    event_id: uuid.UUID
    partition_key: str
    event_organization_id: uuid.UUID
    context_organization_id: uuid.UUID
    event_correlation_id: uuid.UUID
    context_correlation_id: uuid.UUID
    behavior: Behavior


@dataclass
class ScriptedHandler:
    """Manejador guionizado de un consumidor de prueba."""

    consumer: str
    outbox: Outbox | None = None
    mode: Behavior = Behavior.OK
    script: dict[uuid.UUID, deque[Behavior]] = field(default_factory=dict)
    invocations: list[Invocation] = field(default_factory=list)
    effects: Counter[uuid.UUID] = field(default_factory=Counter)
    raw_effects: Counter[uuid.UUID] = field(default_factory=Counter)
    active_partitions: set[str] = field(default_factory=set)
    overlaps: list[str] = field(default_factory=list)
    interleave: int = 0
    """Cuántas veces cede el bucle dentro del manejador (reparto concurrente)."""

    def reset(self) -> None:
        self.mode = Behavior.OK
        self.script.clear()
        self.invocations.clear()
        self.effects.clear()
        self.raw_effects.clear()
        self.active_partitions.clear()
        self.overlaps.clear()

    def plan(self, event_id: uuid.UUID, behaviors: Sequence[Behavior]) -> None:
        self.script.setdefault(event_id, deque()).extend(behaviors)

    def _next(self, event_id: uuid.UUID) -> Behavior:
        queued = self.script.get(event_id)
        if queued:
            return queued.popleft()
        return self.mode

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        if event.partition_key in self.active_partitions:
            self.overlaps.append(event.partition_key)
        self.active_partitions.add(event.partition_key)
        try:
            await self._handle(event, transaction)
        finally:
            self.active_partitions.discard(event.partition_key)

    async def _handle(self, event: OutboxEvent, transaction: Transaction) -> None:
        behavior = self._next(event.event_id)
        context = transaction.context
        self.invocations.append(
            Invocation(
                consumer=self.consumer,
                event_id=event.event_id,
                partition_key=event.partition_key,
                event_organization_id=event.organization_id,
                context_organization_id=context.organization_id,
                event_correlation_id=event.correlation_id,
                context_correlation_id=context.correlation_id,
                behavior=behavior,
            )
        )
        for _ in range(self.interleave):
            await asyncio.sleep(0)
        if behavior is Behavior.DEPENDENCY_DOWN:
            raise ExternalDependencyDown("probe_dependency")
        # Efecto en la base, en la transacción de la entrega: solo queda si se confirma.
        assert self.outbox is not None
        await self.outbox.publish(
            transaction,
            NewEvent(
                event_name=ECHO,
                payload=probe_payload(zone_id=str(event.event_id), code=self.consumer),
            ),
        )
        if behavior is Behavior.DEFECT:
            raise HandlerDefect("defecto inyectado")
        # Efecto externo, idempotente por event_id (BR-NUC-76).
        self.raw_effects[event.event_id] += 1
        if self.effects[event.event_id] == 0:
            self.effects[event.event_id] = 1
        if behavior is Behavior.CRASH_AFTER_EFFECT:
            raise SimulatedCrash()

    def completed(self) -> Counter[uuid.UUID]:
        """Invocaciones que terminaron sin excepción, por evento."""
        return Counter(i.event_id for i in self.invocations if i.behavior is Behavior.OK)


def dispatch_catalog(
    handlers: Mapping[str, ScriptedHandler], metrics: PlatformMetrics | None = None
) -> OutboxCatalog:
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    for name in (PROBE, ECHO):
        catalog.event_types.register(
            EventType(
                event_name=name,
                publisher_unit=ActorUnit.U02,
                payload_model=ProbePayload,
                description_es="Evento sintético del despachador",
            )
        )
    for name, external in ((PLAIN, False), (EXTERNAL, True)):
        catalog.consumers.register(
            Consumer(
                consumer_name=name,
                unit=ActorUnit.U02,
                subscribed_events=(PROBE,),
                handler=handlers[name],
                has_external_dependency=external,
            )
        )
    register_alerts_consumer(catalog.consumers, metrics)
    return catalog


class _NoContextStore:
    """Los contextos de evento y de la proveedora no leen la base."""

    async def session_row(self, *_: Any) -> None:
        raise AssertionError("el despachador no construye contextos de sesión")

    async def operator_row(self, *_: Any) -> None:
        raise AssertionError("el despachador no construye contextos de operador")


def metrics_with_reader() -> tuple[PlatformMetrics, InMemoryMetricReader]:
    """Métricas en memoria con la política global (valores de consumidor ya registrados)."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return PlatformMetrics(provider.get_meter("pruebas")), reader


def metric_points(
    reader: InMemoryMetricReader, name: MetricName
) -> list[tuple[dict[str, Any], float]]:
    """``(atributos, valor)`` de cada punto de ``name``."""
    data = reader.get_metrics_data()
    points: list[tuple[dict[str, Any], float]] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    points.extend(
                        (dict(point.attributes or {}), float(point.value))
                        for point in metric.data.data_points
                        if isinstance(point, NumberDataPoint)
                    )
    return points


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    event_id: uuid.UUID
    consumer: str
    organization_id: uuid.UUID
    partition_key: str
    status: str
    attempts: int
    publish_seq: int


@dataclass
class DispatchEnvironment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    database: Database
    catalog: OutboxCatalog
    outbox: Outbox
    clock: SimulatedClock
    contexts: ScopeContexts
    handlers: dict[str, ScriptedHandler]
    metrics: PlatformMetrics
    reader: InMemoryMetricReader
    provider_organization_id: uuid.UUID
    extra_databases: list[Database] = field(default_factory=list)
    watermark: int = 0
    """``publish_seq`` más alto al aquietar: las lecturas solo ven eventos posteriores."""
    admin: Any = None

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def dispatcher(self, database: Database | None = None, **changes: Any) -> Dispatcher:
        fields: dict[str, Any] = {
            "database": database or self.database,
            "catalog": self.catalog,
            "outbox": self.outbox,
            "contexts": self.contexts,
            "clock": self.clock,
            "jitter": lambda: 0.5,
            "metrics": self.metrics,
        }
        fields.update(changes)
        return Dispatcher(**fields)

    def new_database(self) -> Database:
        """Otro pool, como el de otro proceso de trabajo."""
        database = app_database(self.migrated)
        self.extra_databases.append(database)
        return database

    async def publish(
        self, organization_id: uuid.UUID, plant_ids: Sequence[uuid.UUID | None]
    ) -> list[OutboxEvent]:
        """Publica un ``dispatch_probe`` por planta, en una transacción de la organización."""
        context = make_context(kind=ActorKind.USER, organization_id=organization_id)
        published: list[OutboxEvent] = []
        async with self.database.transaction(context) as transaction:
            for plant_id in plant_ids:
                publication = await self.outbox.publish(
                    transaction,
                    NewEvent(event_name=PROBE, payload=probe_payload(), plant_id=plant_id),
                )
                published.append(publication.event)
        return published

    async def _admin(self) -> Any:
        """Una conexión de superusuario para todas las lecturas de la prueba (sin RLS)."""
        if self.admin is None or self.admin.is_closed():
            self.admin = await self.migrated.connect()
        return self.admin

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return list(await (await self._admin()).fetch(sql, *args))

    async def execute(self, sql: str, *args: Any) -> None:
        await (await self._admin()).execute(sql, *args)

    async def deliveries(self, consumer: str | None = None) -> list[DeliveryRow]:
        rows = await self.fetch(
            "SELECT d.event_id, d.consumer_name, d.organization_id, e.partition_key, d.status,"
            " d.attempts, e.publish_seq FROM shared.outbox_delivery d"
            " JOIN shared.outbox_event e ON e.event_id = d.event_id"
            " WHERE e.event_name = $1 AND ($2::text IS NULL OR d.consumer_name = $2)"
            " AND e.publish_seq > $3 ORDER BY e.publish_seq, d.consumer_name",
            PROBE,
            consumer,
            self.watermark,
        )
        return [
            DeliveryRow(
                event_id=row["event_id"],
                consumer=row["consumer_name"],
                organization_id=row["organization_id"],
                partition_key=row["partition_key"],
                status=row["status"],
                attempts=row["attempts"],
                publish_seq=row["publish_seq"],
            )
            for row in rows
        ]

    async def dead_letters(self) -> list[Any]:
        return await self.fetch(
            "SELECT l.event_id, l.consumer_name, l.organization_id, l.attempts,"
            " l.last_error_code, l.failed_at FROM shared.dead_letter l"
            " JOIN shared.outbox_event e ON e.event_id = l.event_id"
            " WHERE e.publish_seq > $1 ORDER BY l.failed_at",
            self.watermark,
        )

    async def events_named(self, event_name: str) -> list[Any]:
        """Eventos ``event_name`` publicados después de aquietar."""
        return await self.fetch(
            "SELECT event_id, organization_id, payload::text AS payload FROM shared.outbox_event"
            " WHERE event_name = $1 AND publish_seq > $2 ORDER BY publish_seq",
            event_name,
            self.watermark,
        )

    async def echoes(self) -> Counter[tuple[str, uuid.UUID]]:
        """Efectos en la base: ecos confirmados por (consumidor, evento de origen)."""
        rows = await self.fetch(
            "SELECT payload->>'code' AS consumer, payload->>'zone_id' AS source"
            " FROM shared.outbox_event WHERE event_name = $1 AND publish_seq > $2",
            ECHO,
            self.watermark,
        )
        return Counter((row["consumer"], uuid.UUID(row["source"])) for row in rows)

    async def circuit(self, consumer: str) -> tuple[str, datetime | None]:
        (row,) = await self.fetch(
            "SELECT circuit_state, circuit_opened_at FROM shared.consumer WHERE consumer_name = $1",
            consumer,
        )
        return row["circuit_state"], row["circuit_opened_at"]

    async def quiesce(self) -> None:
        """Deja la bandeja sin entregas abiertas ni circuitos abiertos (entre ejemplos).

        Como superusuario: la base es compartida por todos los ejemplos del módulo y el
        despachador ve las particiones de todas las organizaciones.
        """
        await self.execute(
            "UPDATE shared.outbox_delivery SET status = 'delivered', delivered_at = now()"
            " WHERE status IN ('pending', 'retrying', 'dead_letter')"
        )
        await self.execute(
            "UPDATE shared.consumer SET circuit_state = 'closed', circuit_opened_at = NULL"
        )
        (row,) = await self.fetch(
            "SELECT coalesce(max(publish_seq), 0) AS top FROM shared.outbox_event"
        )
        self.watermark = int(row["top"])
        for handler in self.handlers.values():
            handler.reset()


def operator_context(env: DispatchEnvironment) -> ScopeContext:
    """Orden administrativa de un ``platform_operator`` de la proveedora (sin leer la base)."""
    provider = env.provider_organization_id
    return _seal_scope_context(
        organization_id=provider,
        actor=Actor(
            kind=ActorKind.OPERATOR,
            id=uuid.uuid4(),
            display_name_snapshot="Operador sintético",
            unit=ActorUnit.U02,
        ),
        origin=ContextOrigin.ADMIN_COMMAND,
        allowed_scopes=(AllowedScope(ScopeLevel.ORGANIZATION, provider, Role.PLATFORM_OPERATOR),),
        correlation_id=uuid7(),
    )


class DenialAudit:
    """``AuthorizationAudit`` que solo anota las denegaciones."""

    def __init__(self) -> None:
        self.denied: list[Any] = []

    async def authorization_denied(self, context: Any, key: Any, resource: Any) -> None:
        self.denied.append((context.actor.kind, key, resource.kind))


def replay_service(env: DispatchEnvironment) -> tuple[DeadLetterReplay, DenialAudit]:
    """``DeadLetterReplay`` con el escritor de auditoría real y denegaciones anotadas."""
    denials = DenialAudit()
    replay = DeadLetterReplay(
        database=env.database,
        authorizer=Authorizer(audit=denials, provider_organization_id=env.provider_organization_id),
        audit=AuditWriter(
            database=env.database,
            clock=env.clock,
            provider_organization_id=env.provider_organization_id,
        ),
        clock=env.clock,
    )
    return replay, denials


@contextmanager
def dispatch_environment(
    postgres_endpoint: PostgresEndpoint, prefix: str
) -> Iterator[DispatchEnvironment]:
    loop = DatabaseLoop()
    with migrated_database(postgres_endpoint, prefix) as migrated:
        database = app_database(migrated, worker_pool_size=4)
        clock = SimulatedClock(START)
        handlers = {name: ScriptedHandler(name) for name in SCRIPTED_CONSUMERS}
        metrics, reader = metrics_with_reader()
        catalog = dispatch_catalog(handlers, metrics)
        provider = uuid.uuid4()

        async def synchronize() -> None:
            async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)

        loop.run(synchronize())
        outbox = Outbox(catalog, clock)
        for handler in handlers.values():
            handler.outbox = outbox
        contexts = ScopeContexts(
            store=_NoContextStore(),
            clock=clock,
            provider_organization_id=provider,
            system_actor_id=uuid.uuid4(),
        )
        environment = DispatchEnvironment(
            loop=loop,
            migrated=migrated,
            database=database,
            catalog=catalog,
            outbox=outbox,
            clock=clock,
            contexts=contexts,
            handlers=handlers,
            metrics=metrics,
            reader=reader,
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
