"""PR-NUC-13 en la base: el disparador encadena bien con escrituras concurrentes (TASK-108).

Inserciones directas en ``ledger.ledger_record`` y ``shared.audit_entry`` con ``vigia_app``, sin
el escritor de la aplicación (TASK-113), desde varias conexiones a la vez:

- con **20 transacciones concurrentes** sobre la misma cadena, las secuencias quedan contiguas
  ``1..n`` y ``received_at`` no decreciente (criterio de TASK-108; BR-NUC-47);
- la marca se toma **al obtener la exclusión** y no al empezar la transacción: una transacción
  que empezó antes pero escribe después queda detrás con una marca mayor o igual;
- con escrituras generadas y entrelazadas en varias cadenas de varias organizaciones (planta,
  organización y auditoría), cada cadena cumple la propiedad y cada ``record_hash`` (o
  ``entry_hash``) se recalcula en Python desde las columnas persistidas y el hash anterior;
- si otra transacción retiene la cabeza más que ``lock_timeout``, la escritura falla con
  ``lock_not_available`` (``chain_locked_timeout`` en ``shared.db``) y no deja hueco.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    DatabaseLoop,
    MigratedDatabase,
    audit_envelope,
    audit_values,
    chain_hash,
    genesis_hash,
    insert_audit,
    insert_record,
    migrated_database,
    record_envelope,
    record_values,
    register_record_types,
    set_organization,
)

pytestmark = pytest.mark.integration

CONCURRENT_TRANSACTIONS = 20
POOL_SIZE = CONCURRENT_TRANSACTIONS
LOCK_NOT_AVAILABLE = "55P03"


@dataclass(frozen=True)
class Chain:
    """Una cadena: expediente de planta u organización, o auditoría de la organización."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    audit: bool = False


# --- Fixtures -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_chain") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def loop() -> Iterator[DatabaseLoop]:
    runner = DatabaseLoop()
    yield runner
    runner.close()


@pytest.fixture(scope="module")
def superuser(database: MigratedDatabase, loop: DatabaseLoop) -> Iterator[Any]:
    connection = loop.run(database.connect())
    loop.run(register_record_types(connection))
    yield connection
    loop.run(connection.close())


@pytest.fixture(scope="module")
def app_pool(database: MigratedDatabase, loop: DatabaseLoop, superuser: Any) -> Iterator[Any]:
    endpoint = database.endpoint

    async def create() -> Any:
        # Dentro del bucle: el constructor del pool toma el bucle en curso.
        return await asyncpg.create_pool(
            host=endpoint.host,
            port=endpoint.port,
            user="vigia_app",
            password=database.app_password,
            database=database.database,
            min_size=POOL_SIZE,
            max_size=POOL_SIZE,
        )

    pool = loop.run(create())
    yield pool
    loop.run(pool.close())


# --- Escritura y verificación ---------------------------------------------------------------------


async def _write(
    pool: Any, chain: Chain, *, before: float = 0.0, hold: float = 0.0, start: asyncio.Event
) -> None:
    """Una transacción de ``vigia_app`` que escribe un registro (o una entrada) en ``chain``."""
    async with pool.acquire() as connection, connection.transaction():
        await set_organization(connection, chain.organization_id)
        await start.wait()
        if before:
            await connection.execute("SELECT pg_sleep($1)", before)
        if chain.audit:
            await insert_audit(connection, audit_values(chain.organization_id))
        else:
            await insert_record(connection, record_values(chain.organization_id, chain.plant_id))
        if hold:
            await connection.execute("SELECT pg_sleep($1)", hold)


async def _run_concurrently(pool: Any, writes: list[tuple[Chain, float, float]]) -> None:
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(_write(pool, chain, before=before, hold=hold, start=start))
        for chain, before, hold in writes
    ]
    await asyncio.sleep(0)
    start.set()
    await asyncio.gather(*tasks)


async def _verify(superuser: Any, chain: Chain) -> list[Any]:
    """PR-NUC-13 sobre una cadena: secuencias, marcas, enlaces, hashes y cabeza."""
    if chain.audit:
        rows = await superuser.fetch(
            "SELECT * FROM shared.audit_entry WHERE organization_id = $1 ORDER BY chain_sequence",
            chain.organization_id,
        )
        marks = [row["occurred_at"] for row in rows]
        hashes = [row["entry_hash"] for row in rows]
        recomputed = [audit_envelope(dict(row)) for row in rows]
    else:
        rows = await superuser.fetch(
            "SELECT * FROM ledger.ledger_record WHERE organization_id = $1"
            " AND plant_id IS NOT DISTINCT FROM $2 ORDER BY chain_sequence",
            chain.organization_id,
            chain.plant_id,
        )
        marks = [row["received_at"] for row in rows]
        hashes = [row["record_hash"] for row in rows]
        recomputed = [record_envelope(dict(row)) for row in rows]

    assert [row["chain_sequence"] for row in rows] == list(range(1, len(rows) + 1))
    assert marks == sorted(marks), "la marca de la cadena decreció"
    previous = genesis_hash(chain.organization_id, chain.plant_id)
    for row, envelope, stored in zip(rows, recomputed, hashes, strict=True):
        assert row["previous_hash"] == previous
        assert stored == chain_hash(envelope, previous)
        previous = stored

    head = await superuser.fetchrow(
        "SELECT last_sequence, last_hash FROM ledger.chain_head WHERE organization_id = $1"
        " AND kind = $2 AND plant_id IS NOT DISTINCT FROM $3",
        chain.organization_id,
        "audit" if chain.audit else "ledger",
        chain.plant_id,
    )
    assert head is not None
    assert (head["last_sequence"], head["last_hash"]) == (len(rows), previous)
    return list(rows)


# --- Criterio: 20 transacciones concurrentes sobre la misma cadena --------------------------------


def test_twenty_concurrent_transactions_leave_a_contiguous_chain(
    loop: DatabaseLoop, superuser: Any, app_pool: Any
) -> None:
    chain = Chain(uuid.uuid4(), uuid.uuid4())
    # Cada transacción retiene la exclusión un momento antes de confirmar: las demás esperan.
    loop.run(_run_concurrently(app_pool, [(chain, 0.0, 0.01)] * CONCURRENT_TRANSACTIONS))
    rows = loop.run(_verify(superuser, chain))
    assert len(rows) == CONCURRENT_TRANSACTIONS


@pytest.mark.parametrize("audit", [False, True], ids=("ledger", "audit"))
def test_mark_is_taken_when_the_lock_is_obtained(
    loop: DatabaseLoop, superuser: Any, app_pool: Any, audit: bool
) -> None:
    """``now()`` (inicio de la transacción) daría marcas decrecientes; la del disparador no."""
    chain = Chain(uuid.uuid4(), None, audit=audit)
    # La primera transacción empieza a la vez que las otras pero escribe la última.
    writes = [(chain, 0.3, 0.0)] + [(chain, 0.0, 0.02)] * 4
    loop.run(_run_concurrently(app_pool, writes))
    rows = loop.run(_verify(superuser, chain))
    assert len(rows) == len(writes)


# --- PR-NUC-13: escrituras generadas y entrelazadas en varias cadenas -----------------------------


@st.composite
def write_plans(draw: st.DrawFn) -> list[tuple[Chain, float, float]]:
    """Escrituras entrelazadas en cadenas de planta, de organización y de auditoría."""
    organizations = [uuid.uuid4() for _ in range(draw(st.integers(1, 3)))]
    chains = []
    for organization_id in organizations:
        chains.append(Chain(organization_id, None))
        chains.append(Chain(organization_id, None, audit=True))
        chains.extend(Chain(organization_id, uuid.uuid4()) for _ in range(draw(st.integers(1, 2))))
    delays = st.sampled_from((0.0, 0.0, 0.001, 0.005))
    return draw(
        st.lists(st.tuples(st.sampled_from(chains), delays, delays), min_size=1, max_size=16)
    )


@given(plan=write_plans())
def test_interleaved_writes_keep_every_chain_valid(
    loop: DatabaseLoop, superuser: Any, app_pool: Any, plan: list[tuple[Chain, float, float]]
) -> None:
    loop.run(_run_concurrently(app_pool, plan))
    for chain in {chain for chain, _, _ in plan}:
        rows = loop.run(_verify(superuser, chain))
        assert len(rows) == sum(1 for written, _, _ in plan if written == chain)


# --- Exclusión retenida: fallo transitorio sin hueco ----------------------------------------------


def test_lock_timeout_fails_without_leaving_a_gap(
    loop: DatabaseLoop, superuser: Any, app_pool: Any
) -> None:
    chain = Chain(uuid.uuid4(), uuid.uuid4())

    async def scenario() -> asyncpg.PostgresError:
        async with app_pool.acquire() as holder, app_pool.acquire() as waiter:
            holding = holder.transaction()
            await holding.start()
            await set_organization(holder, chain.organization_id)
            await insert_record(holder, record_values(chain.organization_id, chain.plant_id))
            try:
                async with waiter.transaction():
                    await set_organization(waiter, chain.organization_id)
                    await waiter.execute("SET LOCAL lock_timeout = '200ms'")
                    await insert_record(
                        waiter, record_values(chain.organization_id, chain.plant_id)
                    )
            except asyncpg.PostgresError as error:
                await holding.commit()
                return error
            raise AssertionError("la escritura no esperó la exclusión de la cadena")

    error = loop.run(scenario())
    assert error.sqlstate == LOCK_NOT_AVAILABLE
    loop.run(_run_concurrently(app_pool, [(chain, 0.0, 0.0)]))
    rows = loop.run(_verify(superuser, chain))
    assert len(rows) == 2
