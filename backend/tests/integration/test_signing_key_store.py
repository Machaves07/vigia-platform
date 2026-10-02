"""``SqlSigningKeyStore`` contra PostgreSQL 16 (TASK-132; seguimiento R3 de VIG-60).

Dos ``SigningService`` («dos procesos», cada uno con su pool) sobre la misma base como
``vigia_app``, con el gestor de secretos en memoria y el expediente anotado (aquí solo importa el
almacén). Garantía que se prueba: **una rotación que publica conjunto solo se confirma sobre la
última publicación**; si otro proceso publicó entre medias, ``KeyStateConflict`` y nada cambia.

- ``test_r3_interleaving_is_rejected``: el orden exacto de la sonda R3 (A relee, B rota
  ``catalog`` y confirma, A confirma ``gate``) termina en conflicto para A; la última publicación
  lleva la ``catalog`` nueva y la ``issued_at`` sigue creciendo.
- ``test_concurrent_rotations_never_lose_a_key``: rotaciones de propósitos distintos lanzadas a la
  vez desde los dos procesos (``asyncio.gather``), con reintento ante conflicto. Invariantes: cada
  publicación solo pierde, respecto de la anterior, claves del propósito que rota; las
  ``issued_at`` son estrictamente crecientes; la última publicación coincide con las claves
  ``active`` y ``overlapping`` de la base.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.factories import make_context
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.signing_support import BOOTSTRAP_ORDER, InMemorySecrets, RecordingEvents
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorKind, ScopeContext, _seal_scope_context
from vigia_platform.shared.db import Database
from vigia_platform.shared.signing.keys import NODE_PURPOSES, KeyStateConflict, SigningPurpose
from vigia_platform.shared.signing.service import RotationCommit, SigningService
from vigia_platform.shared.signing_store import SqlSigningKeyStore

pytestmark = pytest.mark.integration


@dataclass
class World:
    migrated: MigratedDatabase
    provider_id: uuid.UUID
    operator_id: uuid.UUID

    def context(self) -> ScopeContext:
        return make_context(kind=ActorKind.SYSTEM, organization_id=self.provider_id)

    def operator_context(self) -> ScopeContext:
        """Orden administrativa del operador sembrado (``rotated_by`` es una FK a la cuenta)."""
        base = make_context(kind=ActorKind.OPERATOR, organization_id=self.provider_id)
        return _seal_scope_context(
            organization_id=base.organization_id,
            actor=replace(base.actor, id=self.operator_id),
            origin=base.origin,
            allowed_scopes=base.allowed_scopes,
            correlation_id=base.correlation_id,
        )

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        async def go() -> list[Any]:
            connection = await asyncpg.connect(self.migrated.as_role().dsn)
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        return asyncio.run(go())


async def _seed(migrated: MigratedDatabase, provider_id: uuid.UUID, operator_id: uuid.UUID) -> None:
    connection = await asyncpg.connect(migrated.as_role().dsn)
    now = datetime(2026, 10, 2, tzinfo=UTC)
    try:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO identity.organization (organization_id, code, name, kind,"
                " created_at, created_by) VALUES ($1, 'PROV-KS', 'Proveedora', 'provider', $2, $3)",
                provider_id,
                now,
                operator_id,
            )
            await connection.execute(
                "INSERT INTO identity.user_account (user_id, organization_id, email, display_name,"
                " status, created_at) VALUES ($1, $2, 'op-ks@example.test', 'Operadora',"
                " 'active', $3)",
                operator_id,
                provider_id,
                now,
            )
    finally:
        await connection.close()


@pytest.fixture
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with migrated_database(postgres_endpoint, "keystore") as migrated:
        provider_id, operator_id = uuid.uuid4(), uuid.uuid4()
        asyncio.run(_seed(migrated, provider_id, operator_id))
        yield World(migrated, provider_id, operator_id)


class PausingStore(SqlSigningKeyStore):
    """Con ``pause`` puesto, la próxima confirmación espera a ``release`` (A calculó antes)."""

    def __init__(self, *, database: Database, context: Any) -> None:
        super().__init__(database=database, context=context)
        self.pause = False
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_rotation(self, commit: RotationCommit) -> None:
        if self.pause:
            self.pause = False
            self.waiting.set()
            await self.release.wait()
        await super().commit_rotation(commit)


def _service(
    world: World, database: Database, secrets: InMemorySecrets, store: SqlSigningKeyStore
) -> SigningService:
    return SigningService(
        provider_organization_id=world.provider_id,
        store=store,
        secrets=secrets,
        events=RecordingEvents(),
        clock=SystemClock(),
        environment="test",
    )


def _publications(world: World) -> list[tuple[datetime, frozenset[str]]]:
    rows = world.fetch(
        "SELECT issued_at, ARRAY(SELECT k ->> 'key_id' FROM jsonb_array_elements(keys) AS k)"
        " AS key_ids FROM identity.key_set_publication ORDER BY issued_at, publication_id"
    )
    return [(row["issued_at"], frozenset(row["key_ids"])) for row in rows]


def _purposes(world: World) -> dict[str, str]:
    return {
        r["key_id"]: r["purpose"]
        for r in world.fetch("SELECT key_id, purpose FROM identity.signing_key")
    }


def _published_now(world: World) -> frozenset[str]:
    node = [purpose.value for purpose in NODE_PURPOSES]
    rows = world.fetch(
        "SELECT key_id FROM identity.signing_key"
        " WHERE status IN ('active', 'overlapping') AND purpose = ANY($1)",
        node,
    )
    return frozenset(row["key_id"] for row in rows)


def _check_history(world: World) -> None:
    """Ninguna publicación pierde una clave de otro propósito; ``issued_at`` creciente; la última
    publicación es lo que la base tiene vigente."""
    history = _publications(world)
    purposes = _purposes(world)
    for (before_at, before), (after_at, after) in itertools.pairwise(history):
        assert after_at > before_at
        added = after - before
        assert len(added) == 1, (before, after)
        (new_key,) = added
        rotated = purposes[new_key]
        lost = before - after
        assert all(purposes[key] == rotated for key in lost), (rotated, lost)
    assert history[-1][1] == _published_now(world)


async def _bootstrap(service: SigningService, context: ScopeContext) -> None:
    await service.start(required=())
    for purpose in BOOTSTRAP_ORDER:
        await service.rotate(purpose, context=context)


def test_r3_interleaving_is_rejected(world: World) -> None:
    async def scenario() -> None:
        secrets = InMemorySecrets()
        database_a = app_database(world.migrated, worker_pool_size=2)
        database_b = app_database(world.migrated, worker_pool_size=2)
        store_a = PausingStore(database=database_a, context=world.context)
        store_b = SqlSigningKeyStore(database=database_b, context=world.context)
        a = _service(world, database_a, secrets, store_a)
        b = _service(world, database_b, secrets, store_b)
        operator = world.operator_context()
        try:
            await _bootstrap(a, operator)
            await b.start()
            store_a.pause = True
            gate = asyncio.create_task(a.rotate(SigningPurpose.GATE, context=operator))
            await store_a.waiting.wait()  # A ya releyó y calculó su conjunto
            rotated = await b.rotate(SigningPurpose.CATALOG, context=operator)
            store_a.release.set()
            with pytest.raises(KeyStateConflict):
                await gate
            # El conjunto vigente lleva la catalog de B; la gate de A no existe.
            latest = b.current_publication()
            assert latest is not None and rotated.new_key.key_id in latest.key_ids
            # A relee y su siguiente intento sí se confirma, sobre la publicación de B.
            retried = await a.rotate(SigningPurpose.GATE, context=operator)
            assert retried.publication is not None
            assert rotated.new_key.key_id in retried.publication.key_ids
        finally:
            await database_a.dispose()
            await database_b.dispose()

    asyncio.run(scenario())
    _check_history(world)
    keys = world.fetch("SELECT purpose, count(*) AS n FROM identity.signing_key GROUP BY purpose")
    assert {(r["purpose"], r["n"]) for r in keys} == {
        ("key_set", 1),
        ("catalog", 2),
        ("gate", 2),
        ("live_view_token", 1),
        ("checkpoint", 1),
    }


def test_concurrent_rotations_never_lose_a_key(world: World) -> None:
    conflicts = 0

    async def rotate(
        service: SigningService, purpose: SigningPurpose, context: ScopeContext
    ) -> None:
        nonlocal conflicts
        for _ in range(20):
            try:
                await service.rotate(purpose, context=context)
                return
            except KeyStateConflict:
                conflicts += 1
        raise AssertionError("demasiados conflictos seguidos")

    async def scenario() -> None:
        secrets = InMemorySecrets()
        database_a = app_database(world.migrated, worker_pool_size=2)
        database_b = app_database(world.migrated, worker_pool_size=2)
        a = _service(
            world,
            database_a,
            secrets,
            SqlSigningKeyStore(database=database_a, context=world.context),
        )
        b = _service(
            world,
            database_b,
            secrets,
            SqlSigningKeyStore(database=database_b, context=world.context),
        )
        operator = world.operator_context()
        try:
            await _bootstrap(a, operator)
            await b.start()
            pairs = [
                (SigningPurpose.CATALOG, SigningPurpose.GATE),
                (SigningPurpose.LIVE_VIEW_TOKEN, SigningPurpose.CATALOG),
                (SigningPurpose.GATE, SigningPurpose.KEY_SET),
                (SigningPurpose.CATALOG, SigningPurpose.LIVE_VIEW_TOKEN),
                (SigningPurpose.GATE, SigningPurpose.CATALOG),
                (SigningPurpose.KEY_SET, SigningPurpose.GATE),
            ]
            for first, second in pairs:
                await asyncio.gather(rotate(a, first, operator), rotate(b, second, operator))
        finally:
            await database_a.dispose()
            await database_b.dispose()

    asyncio.run(scenario())
    _check_history(world)
    publications = _publications(world)
    assert len(publications) == 4 + 12  # alta: key_set y tres más; después, doce rotaciones
    assert conflicts >= 1, "las rotaciones no llegaron a solaparse: la prueba no ejerció nada"
