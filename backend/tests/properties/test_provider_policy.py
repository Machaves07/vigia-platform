"""PR-NUC-52: la política de proveedor de ``identity`` (TASK-107, PAT-NUC-SEG-01 capa 2).

Para cualquier concesión generada y cualquier tabla con planta o zona, una consulta **sin filtro
de aplicación** (``UnfilteredPort``, el puerto de prueba) bajo ``actor_kind`` del proveedor
devuelve exactamente las filas del alcance concedido mientras la concesión está vigente, y cero
filas tras revocarla o vencer, para cualquier instante generado respecto de ``now()``.

El puerto lee por ``shared.db.Database`` como ``vigia_app``: la misma apertura de transacción
con ``SET LOCAL`` de ``vigia.organization_id``, ``vigia.actor_kind`` y ``vigia.concession_id``
que usará la aplicación. Base migrada propia con los datos de ``tests/identity_db.py`` (dos
clientes con dos plantas cada uno); cada ejemplo inserta su concesión (la tabla es ⛓, así que
las concesiones se acumulan; ninguna expectativa depende de las de otros ejemplos).

Los instantes se generan en minutos enteros, a un minuto o más de cada borde, para que el
``now()`` de la inserción y el de la lectura (un poco posterior) caigan del mismo lado.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import TextClause, text

from tests.factories import make_context
from tests.identity_db import (
    PROVIDER_SCOPED_TABLES,
    IdentitySeed,
    MigratedDatabase,
    Tenant,
    insert_concession,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode

pytestmark = pytest.mark.integration


class UnfilteredPort:
    """Puerto de prueba: lee cada tabla entera, sin ningún filtro de aplicación (PR-NUC-52).

    Solo la base decide qué filas ve el contexto: si la política de proveedor faltara, este
    puerto devolvería toda la organización.
    """

    QUERIES: ClassVar[dict[str, TextClause]] = {
        "plant": text("SELECT plant_id FROM identity.plant"),
        "zone": text("SELECT zone_id FROM identity.zone"),
        "node_identity": text("SELECT node_id FROM identity.node_identity"),
        "zone_node_assignment": text("SELECT assignment_id FROM identity.zone_node_assignment"),
        "live_view_token_issuance": text("SELECT jti FROM identity.live_view_token_issuance"),
    }

    def __init__(self, database: Database) -> None:
        self._database = database

    async def visible(self, context: ScopeContext) -> dict[str, set[uuid.UUID]]:
        """Identificadores visibles de cada tabla con planta o zona, en una sola transacción."""
        result: dict[str, set[uuid.UUID]] = {}
        async with self._database.transaction(context) as transaction:
            for table, query in self.QUERIES.items():
                rows = (await transaction.execute(query)).all()
                result[table] = {row[0] for row in rows}
        return result


def test_the_port_covers_every_provider_scoped_table() -> None:
    assert set(UnfilteredPort.QUERIES) == set(PROVIDER_SCOPED_TABLES)


@dataclass(frozen=True)
class Policy:
    database: MigratedDatabase
    seed: IdentitySeed


@pytest.fixture(scope="module")
def runner() -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as loop:
        yield loop


@pytest.fixture(scope="module")
def policy(postgres_endpoint: PostgresEndpoint) -> Iterator[Policy]:
    with seeded_identity(postgres_endpoint, "vigia_provider_policy") as (database, seed):
        yield Policy(database, seed)


@pytest.fixture(scope="module")
def port(policy: Policy, runner: asyncio.Runner) -> Iterator[UnfilteredPort]:
    settings = DatabaseSettings(
        url=policy.database.as_role("vigia_app").sqlalchemy_url,
        process=ProcessKind.API,
        sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS; en AWS, verify-full
    )
    database = Database.create(settings)
    try:
        yield UnfilteredPort(database)
    finally:
        runner.run(database.dispose())


@pytest.fixture(scope="module")
def superuser(policy: Policy, runner: asyncio.Runner) -> Iterator[Any]:
    connection = runner.run(policy.database.connect())
    try:
        yield connection
    finally:
        runner.run(connection.close())


EMPTY: dict[str, set[uuid.UUID]] = {table: set() for table in PROVIDER_SCOPED_TABLES}


# --- Generación de concesiones ------------------------------------------------------------------

MINUTE = dt.timedelta(minutes=1)
MAX_DURATION_MINUTES = 90 * 24 * 60


@dataclass(frozen=True)
class Scenario:
    tenant: str
    """``a`` o ``b``: la organización cliente de la concesión y del contexto."""
    plant: int | None
    """``None`` para toda la organización; 0 o 1 para una de sus dos plantas."""
    timing: str
    """``current`` (vigente), ``expired`` (vencida) o ``future`` (aún no en vigor)."""
    granted_offset: dt.timedelta
    duration: dt.timedelta
    status: str
    context_concession: str
    """``own`` (la generada), ``unknown`` (una que no existe) u ``other_tenant`` (la vigente
    de toda la otra organización)."""

    @property
    def in_force(self) -> bool:
        return self.timing == "current" and self.status == "active"


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    timing = draw(st.sampled_from(["current", "expired", "future"]))
    if timing == "current":  # concedida hace g, vence dentro de e; 1 h ≤ g + e ≤ 90 días
        granted = draw(st.integers(60, 60 * 24 * 60))
        expires = draw(st.integers(1, 30 * 24 * 60))
        offset, duration = -granted, granted + expires
    elif timing == "expired":  # venció hace e
        duration = draw(st.integers(60, MAX_DURATION_MINUTES))
        offset = -draw(st.integers(1, 30 * 24 * 60)) - duration
    else:  # entra en vigor dentro de g
        offset = draw(st.integers(1, 7 * 24 * 60))
        duration = draw(st.integers(60, MAX_DURATION_MINUTES))
    return Scenario(
        tenant=draw(st.sampled_from(["a", "b"])),
        plant=draw(st.sampled_from([None, 0, 1])),
        timing=timing,
        granted_offset=offset * MINUTE,
        duration=duration * MINUTE,
        status=draw(st.sampled_from(["active", "active", "revoked", "expired"])),
        context_concession=draw(st.sampled_from(["own", "own", "own", "unknown", "other_tenant"])),
    )


def _tenant(seed: IdentitySeed, name: str) -> Tenant:
    return seed.a if name == "a" else seed.b


def _expected(seed: IdentitySeed, scenario: Scenario) -> dict[str, set[uuid.UUID]]:
    if not scenario.in_force or scenario.context_concession != "own":
        return EMPTY
    tenant = _tenant(seed, scenario.tenant)
    plant_id = None if scenario.plant is None else tenant.plants[scenario.plant].plant_id
    return tenant.scoped_ids(plant_id)


async def _grant(superuser: Any, seed: IdentitySeed, scenario: Scenario) -> uuid.UUID:
    tenant = _tenant(seed, scenario.tenant)
    level, scope = "organization", None
    if scenario.plant is not None:
        level, scope = "plant", tenant.plants[scenario.plant].plant_id
    return await insert_concession(
        superuser,
        seed,
        tenant.organization_id,
        scope_level=level,
        scope_id=scope,
        granted_offset=scenario.granted_offset,
        duration=scenario.duration,
        status=scenario.status,
    )


@given(scenario=scenarios())
def test_provider_sees_exactly_the_granted_scope_while_in_force(
    policy: Policy,
    port: UnfilteredPort,
    superuser: Any,
    runner: asyncio.Runner,
    scenario: Scenario,
) -> None:
    seed = policy.seed
    tenant = _tenant(seed, scenario.tenant)
    other = seed.b if scenario.tenant == "a" else seed.a
    concession = runner.run(_grant(superuser, seed, scenario))
    context_concession = {
        "own": concession,
        "unknown": uuid.uuid4(),
        "other_tenant": seed.concessions[other.organization_id],
    }[scenario.context_concession]
    context = make_context(
        kind=ActorKind.PROVIDER_USER,
        organization_id=tenant.organization_id,
        concession_id=context_concession,
    )
    assert runner.run(port.visible(context)) == _expected(seed, scenario), scenario


def test_a_client_user_still_sees_the_whole_organization(
    policy: Policy, port: UnfilteredPort, runner: asyncio.Runner
) -> None:
    """Control: la política de proveedor no recorta a nadie más."""
    for tenant in (policy.seed.a, policy.seed.b):
        for kind in (ActorKind.USER, ActorKind.NODE, ActorKind.SYSTEM, ActorKind.OPERATOR):
            context = make_context(kind=kind, organization_id=tenant.organization_id)
            assert runner.run(port.visible(context)) == tenant.scoped_ids()


def test_revoking_cuts_access_in_the_next_transaction(
    policy: Policy, port: UnfilteredPort, superuser: Any, runner: asyncio.Runner
) -> None:
    seed = policy.seed
    plant = seed.a.plants[0]

    async def scenario() -> None:
        concession = await insert_concession(
            superuser, seed, seed.a.organization_id, scope_level="plant", scope_id=plant.plant_id
        )
        context = make_context(
            kind=ActorKind.PROVIDER_USER,
            organization_id=seed.a.organization_id,
            concession_id=concession,
        )
        assert await port.visible(context) == seed.a.scoped_ids(plant.plant_id)
        # El cliente revoca con su propio contexto (actualización de cierre como vigia_app).
        client = make_context(kind=ActorKind.USER, organization_id=seed.a.organization_id)
        async with port._database.transaction(client) as transaction:
            revoked = await transaction.execute(
                text(
                    "UPDATE identity.provider_concession SET status = 'revoked',"
                    " revoked_at = now(), revoked_by = :user, revoked_by_side = 'client'"
                    " WHERE concession_id = :concession"
                ),
                {"user": seed.a.user_id, "concession": concession},
            )
            assert revoked.rowcount == 1
        assert await port.visible(context) == EMPTY

    runner.run(scenario())


def test_expiry_cuts_access_without_any_update(
    policy: Policy, port: UnfilteredPort, superuser: Any, runner: asyncio.Runner
) -> None:
    """La concesión vence sola: ``expires_at > now()`` basta, sin pasarla a ``expired``."""
    seed = policy.seed

    async def scenario() -> None:
        concession = await insert_concession(
            superuser,
            seed,
            seed.b.organization_id,
            granted_offset=-dt.timedelta(hours=1) + dt.timedelta(seconds=2),
            duration=dt.timedelta(hours=1),
        )
        context = make_context(
            kind=ActorKind.PROVIDER_USER,
            organization_id=seed.b.organization_id,
            concession_id=concession,
        )
        assert await port.visible(context) == seed.b.scoped_ids()
        await asyncio.sleep(3)
        assert await port.visible(context) == EMPTY

    runner.run(scenario())


def test_provider_cannot_write_outside_the_granted_plant(
    policy: Policy, port: UnfilteredPort, superuser: Any, runner: asyncio.Runner
) -> None:
    """La política también es ``WITH CHECK``: el proveedor no escribe fuera de su alcance."""
    seed = policy.seed
    inside, outside = seed.a.plants

    async def scenario() -> None:
        concession = await insert_concession(
            superuser, seed, seed.a.organization_id, scope_level="plant", scope_id=inside.plant_id
        )
        context = make_context(
            kind=ActorKind.PROVIDER_USER,
            organization_id=seed.a.organization_id,
            concession_id=concession,
        )
        insert = text(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
            " created_at) VALUES (:node, :organization, :plant, :code, now())"
        )

        class Rollback(Exception):
            pass

        with pytest.raises(Rollback):
            async with port._database.transaction(context) as transaction:
                await transaction.execute(
                    insert,
                    {
                        "node": uuid.uuid4(),
                        "organization": seed.a.organization_id,
                        "plant": inside.plant_id,
                        "code": "ND-INSIDE",
                    },
                )
                raise Rollback
        with pytest.raises(Exception, match="row-level security"):
            async with port._database.transaction(context) as transaction:
                await transaction.execute(
                    insert,
                    {
                        "node": uuid.uuid4(),
                        "organization": seed.a.organization_id,
                        "plant": outside.plant_id,
                        "code": "ND-OUTSIDE",
                    },
                )

    runner.run(scenario())


@pytest.mark.parametrize(
    ("actor_kind", "concession"),
    [
        ("provider", None),  # el nombre del diseño, sin concesión
        ("provider_user", None),  # el valor de ActorKind, sin concesión
        ("provider_user", "unknown"),
        ("user", "unknown"),  # cualquier actor con concesión fijada queda acotado a ella
    ],
)
def test_provider_markers_without_a_concession_in_force_see_nothing(
    policy: Policy, runner: asyncio.Runner, actor_kind: str, concession: str | None
) -> None:
    """Fallo cerrado a nivel de base, sin pasar por ``ScopeContext``."""
    seed = policy.seed

    async def scenario() -> None:
        connection = await policy.database.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_scope(
                    connection,
                    seed.a.organization_id,
                    actor_kind=actor_kind,
                    concession_id=uuid.uuid4() if concession == "unknown" else None,
                )
                for table in PROVIDER_SCOPED_TABLES:
                    rows = await connection.fetch(
                        f"SELECT 1 FROM identity.{table}"  # noqa: S608 - nombre fijo de la lista
                    )
                    assert rows == [], table
        finally:
            await connection.close()

    runner.run(scenario())


def test_a_concession_on_another_organization_is_not_a_key(
    policy: Policy, runner: asyncio.Runner
) -> None:
    """La concesión vigente de B no abre A aunque el contexto diga A."""
    seed = policy.seed

    async def scenario() -> None:
        connection = await policy.database.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_scope(
                    connection,
                    seed.a.organization_id,
                    actor_kind="provider_user",
                    concession_id=seed.concessions[seed.b.organization_id],
                )
                for table in PROVIDER_SCOPED_TABLES:
                    assert await connection.fetch(f"SELECT 1 FROM identity.{table}") == []  # noqa: S608
        finally:
            await connection.close()

    runner.run(scenario())
