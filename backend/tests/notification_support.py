"""Mundo de prueba del motor de notificaciones (TASK-324) contra PostgreSQL 16 real.

``notification_world(endpoint, prefix)``: una base migrada con el par de organizaciones de U-02
(``tests/identity_db.py``: la proveedora con su operador y su instalador; los clientes A y B, cada
uno con su administrador de organización y dos plantas con su zona), ``shared.db`` como
``vigia_app``, los constructores de contexto del sistema reales (``ScopeContexts``) y el
``IdentityQueryPort`` real (``HierarchyService`` con la consulta acotada, A-69). El motor se
construye con la política de texto libre **real** de todas las unidades (los dos validadores de
U-04 con sus preparadores, que leen la base).

Dobles solo donde el módulo real es de otra tarea: ``StubSender`` es el ``EmailSenderPort`` de
TASK-325 (solo su ``circuit_state``), ``StubRecords`` el registro ``export_produced`` del evento.
Los usuarios, las plantas, las zonas y las asignaciones de rol se siembran como superusuario.

Solo datos generados (NFR-CTR-43): correos ``@example.test`` y nombres sintéticos. Las marcas salen
del ``SimulatedClock``, nunca de la hora real.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import uuid
from collections.abc import Awaitable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal, TypeVar
from unittest.mock import Mock

from tests.factories import uuid7
from tests.identity_db import (
    BASE_TIME,
    IdentitySeed,
    MigratedDatabase,
    insert_concession,
    insert_plant,
    seeded_identity,
)
from tests.integration.conftest import PostgresEndpoint
from tests.loop_db import LoopScope, corrective_action
from tests.outbox_support import app_database
from tests.worker_support import system_contexts
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.notifications.engine import NotificationEngine
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
from vigia_platform.shared.db import Database
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key
from vigia_platform.shared.runtime.units import free_text_registry, registered_units

__all__ = [
    "Member",
    "NotificationWorld",
    "Senders",
    "StubRecords",
    "StubSender",
    "notification_world",
]

T = TypeVar("T")
START: Final = BASE_TIME + timedelta(days=30)
POOL_SIZE: Final = 8
"""Conexiones del proceso de prueba: dos entregas a la vez, cada una con su transacción y las
lecturas de la identidad en conexiones propias."""


class StubSender:
    """El ``EmailSenderPort`` de TASK-325: aquí solo importa su circuito."""

    def __init__(self, state: Literal["closed", "open", "half_open"] = "closed") -> None:
        self.state: Literal["closed", "open", "half_open"] = state
        self.checks = 0

    def circuit_state(self) -> Literal["closed", "open", "half_open"]:
        self.checks += 1
        return self.state


@dataclass
class Senders:
    """``EmailSenderRegistry`` con el puerto que se le ponga (o ninguno)."""

    current: StubSender | None = None

    @property
    def sender(self) -> StubSender | None:
        return self.current


@dataclass(frozen=True)
class _View:
    content: Mapping[str, Any]

    def document(self) -> Mapping[str, Any]:
        return self.content


@dataclass
class StubRecords:
    """``LectorExpediente.get`` del registro ``export_produced`` que cita el evento."""

    documents: dict[uuid.UUID, Mapping[str, Any]] = field(default_factory=dict)

    async def get(
        self,
        context: ScopeContext,
        record_id: uuid.UUID,
        *,
        record_types: tuple[str, ...] | None = None,
    ) -> Any:
        found = self.documents.get(record_id)
        return None if found is None else _View(found)


@dataclass(frozen=True)
class Member:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


@dataclass
class NotificationWorld:
    migrated: MigratedDatabase
    seed: IdentitySeed
    database: Database
    contexts: ScopeContexts
    clock: SimulatedClock
    loop: asyncio.AbstractEventLoop
    free_text: FreeTextPolicyRegistry
    directory: HierarchyService
    owners: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    """Un usuario de cada organización cliente, para las filas sembradas que lo piden."""

    @property
    def provider_id(self) -> uuid.UUID:
        return self.seed.provider_organization_id

    def run(self, awaitable: Awaitable[T]) -> T:
        return self.loop.run_until_complete(awaitable)

    async def superuser(self) -> Any:
        return await self.migrated.connect()

    # --- siembra ---

    def new_organization(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """Un cliente nuevo con una planta y su zona: (org, planta, zona). Su único usuario es un
        ``line_manager`` desactivado (el dueño de las filas sembradas): no recibe nada."""

        async def run() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
            connection = await self.superuser()
            try:
                organization_id, owner = uuid.uuid4(), uuid.uuid4()
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO identity.organization (organization_id, code, name, kind,"
                        " created_at, created_by, concession_max_days)"
                        " VALUES ($1, $2, 'Cliente sintético', 'client', $3, $4, 90)",
                        organization_id,
                        f"ORG-{secrets.token_hex(6).upper()}",
                        BASE_TIME,
                        self.seed.operator_id,
                    )
                    await connection.execute(
                        "INSERT INTO identity.user_account (user_id, organization_id, email,"
                        " display_name, status, created_at, deactivated_at)"
                        " VALUES ($1, $2, $3, 'Dueño sintético', 'deactivated', $4, $4)",
                        owner,
                        organization_id,
                        f"owner-{secrets.token_hex(6)}@example.test",
                        BASE_TIME,
                    )
                    plant = await insert_plant(
                        connection, organization_id, owner, self.seed.operator_id
                    )
            finally:
                await connection.close()
            self.owners[organization_id] = owner
            return organization_id, plant.plant_id, plant.zone_id

        return self.run(run())

    def add_plant(self, organization_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        """Una planta más de ``organization_id`` con su zona: (planta, zona)."""

        async def run() -> tuple[uuid.UUID, uuid.UUID]:
            connection = await self.superuser()
            try:
                plant = await insert_plant(
                    connection,
                    organization_id,
                    self.owners[organization_id],
                    self.seed.operator_id,
                )
            finally:
                await connection.close()
            return plant.plant_id, plant.zone_id

        return self.run(run())

    def add_zone(self, organization_id: uuid.UUID, plant_id: uuid.UUID) -> uuid.UUID:
        async def run() -> uuid.UUID:
            zone_id = uuid.uuid4()
            connection = await self.superuser()
            try:
                await connection.execute(
                    "INSERT INTO identity.zone"
                    " (zone_id, organization_id, plant_id, code, name, created_at, created_by)"
                    " VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
                    zone_id,
                    organization_id,
                    plant_id,
                    f"ZN-{secrets.token_hex(6).upper()}",
                    BASE_TIME,
                    self.seed.operator_id,
                )
            finally:
                await connection.close()
            return zone_id

        return self.run(run())

    def add_member(
        self,
        organization_id: uuid.UUID,
        role: Role,
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
        *,
        status: str = "active",
        removed: bool = False,
    ) -> Member:
        """Un usuario nuevo con una asignación de ``role`` sobre el alcance."""

        async def run() -> Member:
            user_id = uuid.uuid4()
            connection = await self.superuser()
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO identity.user_account (user_id, organization_id, email,"
                        " display_name, status, created_at, deactivated_at)"
                        " VALUES ($1, $2, $3, 'Persona sintética', $4, $5, $6)",
                        user_id,
                        organization_id,
                        f"member-{secrets.token_hex(6)}@example.test",
                        status,
                        BASE_TIME,
                        BASE_TIME if status == "deactivated" else None,
                    )
                    await connection.execute(
                        "INSERT INTO identity.role_assignment (assignment_id, organization_id,"
                        " user_id, role, scope_level, scope_id, assigned_at, assigned_by,"
                        " removed_at, removed_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                        uuid.uuid4(),
                        organization_id,
                        user_id,
                        role.value,
                        scope_level.value,
                        scope_id,
                        BASE_TIME,
                        self.seed.operator_id,
                        BASE_TIME + timedelta(days=1) if removed else None,
                        self.seed.operator_id if removed else None,
                    )
            finally:
                await connection.close()
            return Member(user_id, organization_id, role, scope_level, scope_id)

        return self.run(run())

    def seed_action(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        assignee: uuid.UUID,
    ) -> uuid.UUID:
        """Una ``loop.corrective_action`` de la zona con su responsable (la de una reapertura)."""
        action_id = uuid.uuid4()
        scope = LoopScope(organization_id, plant_id, zone_id, uuid.uuid4(), assignee, uuid.uuid4())

        async def run() -> None:
            connection = await self.superuser()
            try:
                sql, args = corrective_action(scope, action_id=action_id)
                await connection.execute(sql, *args)
            finally:
                await connection.close()

        self.run(run())
        return action_id

    def plant_concession(self, organization_id: uuid.UUID, plant_id: uuid.UUID) -> ScopeContext:
        """El contexto del instalador del proveedor bajo una concesión vigente de ``plant_id``."""

        async def run() -> uuid.UUID:
            connection = await self.superuser()
            try:
                return await insert_concession(
                    connection, self.seed, organization_id, scope_level="plant", scope_id=plant_id
                )
            finally:
                await connection.close()

        concession_id = self.run(run())
        return _seal_scope_context(
            organization_id=organization_id,
            actor=Actor(
                kind=ActorKind.PROVIDER_USER,
                id=self.seed.installer_id,
                display_name_snapshot="Instalación sintética",
                unit=ActorUnit.U04,
                role_in_use=Role.PROVIDER_INSTALLER,
                concession_id=concession_id,
            ),
            origin=ContextOrigin.SESSION,
            allowed_scopes=[AllowedScope(ScopeLevel.PLANT, plant_id, Role.PROVIDER_INSTALLER)],
            correlation_id=uuid7(),
            session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest(),
        )

    # --- eventos y motor ---

    def event(
        self,
        organization_id: uuid.UUID,
        event_name: str,
        payload: Mapping[str, Any],
        *,
        plant_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
    ) -> OutboxEvent:
        return OutboxEvent(
            event_id=event_id or uuid7(),
            organization_id=organization_id,
            plant_id=plant_id,
            event_name=event_name,
            partition_key=partition_key(organization_id, plant_id),
            ledger_sequence=None,
            payload=dict(payload),
            correlation_id=uuid7(),
            created_at=self.clock.now(),
        )

    def engine(
        self,
        *,
        senders: Senders | None = None,
        records: StubRecords | None = None,
        directory: Any = None,
    ) -> NotificationEngine:
        return NotificationEngine(
            directory=directory or self.directory,
            records=records or StubRecords(),
            contexts=self.contexts,
            database=self.database,
            senders=senders if senders is not None else Senders(StubSender()),
            free_text=self.free_text,
            clock=self.clock,
            provider_organization_id=self.provider_id,
        )

    def event_context(self, event: OutboxEvent) -> ScopeContext:
        return self.contexts.context_from_event(event, unit=ActorUnit.U04)

    def deliver(
        self,
        engine: NotificationEngine,
        event: OutboxEvent,
        *,
        context: ScopeContext | None = None,
    ) -> int:
        """Una entrega como la del despachador: transacción con el contexto del evento."""

        async def run() -> int:
            async with self.database.transaction(context or self.event_context(event)) as tx:
                return await engine.process(event, tx)

        return self.run(run())

    # --- lecturas como superusuario ---

    def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async def run() -> list[dict[str, Any]]:
            connection = await self.superuser()
            try:
                rows = [dict(row) for row in await connection.fetch(sql, *args)]
            finally:
                await connection.close()
            for row in rows:
                if isinstance(row.get("deep_link_ref"), str):
                    row["deep_link_ref"] = json.loads(row["deep_link_ref"])
            return rows

        return self.run(run())

    def notifications(self, event_id: uuid.UUID) -> list[dict[str, Any]]:
        """Los avisos del evento en todas las organizaciones."""
        return self.fetch(
            "SELECT * FROM notifications.notification WHERE source_event_id = $1"
            " ORDER BY organization_id, recipient_user_id",
            event_id,
        )

    def deliveries(self, event_id: uuid.UUID) -> list[dict[str, Any]]:
        return self.fetch(
            "SELECT d.*, n.recipient_user_id FROM notifications.notification_delivery AS d"
            " JOIN notifications.notification AS n ON n.notification_id = d.notification_id"
            " AND n.created_at = d.notification_created_at WHERE n.source_event_id = $1"
            " ORDER BY n.recipient_user_id, d.channel",
            event_id,
        )

    def emails(self, event_id: uuid.UUID) -> list[dict[str, Any]]:
        return self.fetch(
            "SELECT e.* FROM notifications.email_message AS e"
            " JOIN notifications.notification AS n ON n.notification_id = ANY(e.related_ids)"
            " WHERE n.source_event_id = $1 ORDER BY e.to_user_id",
            event_id,
        )

    def count(self, table: str, organization_id: uuid.UUID) -> int:
        rows = self.fetch(
            f"SELECT count(*) AS n FROM notifications.{table}"  # noqa: S608
            " WHERE organization_id = $1",
            organization_id,
        )
        return int(rows[0]["n"])


@contextlib.contextmanager
def notification_world(endpoint: PostgresEndpoint, prefix: str) -> Iterator[NotificationWorld]:
    with seeded_identity(endpoint, prefix) as (migrated, seed):
        loop = asyncio.new_event_loop()
        clock = SimulatedClock(START)
        # Topes de la fixture largos (retro 15): la prueba concurrente espera a la otra entrega en
        # la clave de idempotencia y ningún tope de producción debe decidir su resultado.
        database = app_database(
            migrated,
            worker_pool_size=POOL_SIZE,
            lock_timeout_ms=60_000,
            statement_timeout_ms=120_000,
            pool_timeout_seconds=60.0,
        )
        contexts = system_contexts(clock, seed.provider_organization_id)
        inert: Any = Mock()
        directory = HierarchyService(
            IdentityDependencies(
                database=database,
                writer=inert,
                audit=inert,
                outbox=inert,
                authorizer=inert,
                free_text=inert,
                clock=clock,
                provider_organization_id=seed.provider_organization_id,
            ),
            lookups=contexts,
        )
        world = NotificationWorld(
            migrated=migrated,
            seed=seed,
            database=database,
            contexts=contexts,
            clock=clock,
            loop=loop,
            free_text=free_text_registry(registered_units()),
            directory=directory,
            owners={tenant.organization_id: tenant.user_id for tenant in (seed.a, seed.b)},
        )
        try:
            yield world
        finally:
            loop.run_until_complete(database.dispose())
            loop.close()
