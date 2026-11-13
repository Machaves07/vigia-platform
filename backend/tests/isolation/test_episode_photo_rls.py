"""Aislamiento de ``fleet.episode_photo`` en la base (TASK-572; A-112; A-69; BR-NUC-01).

Con dos organizaciones de ``tests/identity_db.py`` (dos plantas cada una) y una foto del episodio
por planta, como ``vigia_app``:

- el contexto de una organización no ve ni escribe las fotos de la otra
  (``organization_isolation``);
- la proveedora con una concesión de **una sola planta** de A ve solo las fotos de esa planta y no
  escribe en la otra planta ni en la otra organización (``provider_concession_scope``, A-69);
- **sonda negativa**: sin ``provider_concession_scope`` la proveedora ve la otra planta, y sin
  ``organization_isolation`` una organización ve la otra (en transacciones que se revierten).

Solo datos generados (NFR-CTR-43): la imagen es un JPEG sintético.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.episode_photo_support import jpeg
from tests.fleet_db import FleetScope, fleet_scopes
from tests.identity_db import (
    BASE_TIME,
    IdentitySeed,
    MigratedDatabase,
    insert_concession,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

PROVIDER: Final = "provider_user"
INSUFFICIENT_PRIVILEGE: Final = "42501"
IMAGE: Final = jpeg()

_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, camera_id, frame_at, width,"
    " height, size_bytes, sha256, blur_params_version, image, image_absent_reason,"
    " canonical_sha256, platform_record_id) VALUES ($1, $2, $3, $4, $5, $6, 0, 'finding',"
    " 'coexistence', $7, 1, 0.9, $8, $8, $8, $8::timestamptz + interval '30 days', $9, $8, 320,"
    " 240, $10, $11, 'blur-v1', $12, NULL, $13, $14)"
)


def _row(scope: FleetScope) -> tuple[Any, ...]:
    return (
        scope.organization_id,
        uuid.uuid4(),
        scope.plant_id,
        scope.zone_id,
        scope.node_id,
        uuid.uuid4(),
        uuid.uuid4(),
        BASE_TIME,
        uuid.uuid4(),
        len(IMAGE),
        hashlib.sha256(IMAGE).hexdigest(),
        IMAGE,
        "ab" * 32,
        uuid.uuid4(),
    )


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    identity: IdentitySeed
    scopes: dict[uuid.UUID, tuple[FleetScope, ...]]
    concession_id: uuid.UUID

    @property
    def conceded(self) -> FleetScope:
        return self.scopes[self.identity.a.organization_id][0]

    @property
    def other_plant(self) -> FleetScope:
        return self.scopes[self.identity.a.organization_id][1]

    @property
    def other_organization(self) -> FleetScope:
        return self.scopes[self.identity.b.organization_id][0]


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_episode_photo_rls") as (database, identity):

        async def prepare() -> World:
            connection = await database.connect()
            try:
                scopes = {
                    tenant.organization_id: fleet_scopes(tenant)
                    for tenant in (identity.a, identity.b)
                }
                for plants in scopes.values():
                    for scope in plants:
                        await connection.execute(_INSERT, *_row(scope))
                a = identity.a
                concession = await insert_concession(
                    connection,
                    identity,
                    a.organization_id,
                    scope_level="plant",
                    scope_id=a.plants[0].plant_id,
                    status="active",
                )
            finally:
                await connection.close()
            return World(database, identity, scopes, concession)

        yield asyncio.run(prepare())


@pytest_asyncio.fixture
async def app(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def superuser(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la sonda."""


async def _seen(
    connection: Any,
    organization_id: uuid.UUID,
    *,
    actor_kind: str = "user",
    concession_id: uuid.UUID | None = None,
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    async with connection.transaction():
        await set_scope(
            connection, organization_id, actor_kind=actor_kind, concession_id=concession_id
        )
        rows = await connection.fetch(
            "SELECT DISTINCT organization_id, plant_id FROM fleet.episode_photo"
        )
    return {(row["organization_id"], row["plant_id"]) for row in rows}


async def _insert(
    connection: Any,
    organization_id: uuid.UUID,
    target: FleetScope,
    *,
    actor_kind: str = "user",
    concession_id: uuid.UUID | None = None,
) -> str:
    """``ok`` o el SQLSTATE del alta (revertida) de una foto en ``target``."""
    try:
        async with connection.transaction():
            await set_scope(
                connection, organization_id, actor_kind=actor_kind, concession_id=concession_id
            )
            await connection.execute(_INSERT, *_row(target))
            raise _Rollback
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)
    except _Rollback:
        return "ok"


def _plants(world: World, organization_id: uuid.UUID) -> set[tuple[uuid.UUID, uuid.UUID]]:
    return {(s.organization_id, s.plant_id) for s in world.scopes[organization_id]}


@pytest.mark.asyncio
async def test_an_organization_neither_sees_nor_writes_the_photos_of_the_other(
    app: Any, world: World
) -> None:
    a, b = world.identity.a.organization_id, world.identity.b.organization_id
    assert await _seen(app, a) == _plants(world, a)
    assert await _seen(app, b) == _plants(world, b)
    # Con el contexto de B, una foto de A no se escribe.
    assert await _insert(app, b, world.conceded) == INSUFFICIENT_PRIVILEGE
    # La propia sí (la prueba no pasa por una guarda que lo rechaza todo).
    assert await _insert(app, b, world.other_organization) == "ok"


@pytest.mark.asyncio
async def test_the_provider_reads_only_the_conceded_plant(app: Any, world: World) -> None:
    conceded = world.conceded
    provider = {"actor_kind": PROVIDER, "concession_id": world.concession_id}
    seen = await _seen(app, conceded.organization_id, **provider)
    assert seen == {(conceded.organization_id, conceded.plant_id)}
    other = world.other_organization
    assert await _seen(app, other.organization_id, **provider) == set()
    for target in (world.other_plant, world.other_organization):
        result = await _insert(app, target.organization_id, target, **provider)
        assert result == INSUFFICIENT_PRIVILEGE, (target.plant_id, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["provider_concession_scope", "organization_isolation"])
async def test_removing_a_policy_is_detected(superuser: Any, world: World, policy: str) -> None:
    """Sin una de las dos políticas, las comprobaciones de arriba encuentran la fuga."""
    conceded = world.conceded
    try:
        async with superuser.transaction():
            await superuser.execute(f"DROP POLICY {policy} ON fleet.episode_photo")
            if policy == "organization_isolation":
                # Sin la permisiva, nada pasaría: una que lo deja todo hace visible la otra.
                await superuser.execute(
                    "CREATE POLICY open_probe ON fleet.episode_photo AS PERMISSIVE FOR ALL"
                    " TO PUBLIC USING (true) WITH CHECK (true)"
                )
            await superuser.execute("SET LOCAL ROLE vigia_app")
            if policy == "provider_concession_scope":
                seen = await _seen(
                    superuser,
                    conceded.organization_id,
                    actor_kind=PROVIDER,
                    concession_id=world.concession_id,
                )
            else:
                seen = await _seen(superuser, world.identity.b.organization_id)
            raise _Rollback
    except _Rollback:
        pass
    if policy == "provider_concession_scope":
        assert (conceded.organization_id, world.other_plant.plant_id) in seen
    else:
        assert (conceded.organization_id, conceded.plant_id) in seen
