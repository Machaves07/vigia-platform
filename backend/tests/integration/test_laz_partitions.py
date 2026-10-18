"""Particiones mensuales de ``loop`` (TASK-301; NFR-LAZ-07; PAT-LAZ-ESC-04; NFR-NUC-14).

Base migrada propia con los datos de ``tests/identity_db.py`` y de ``tests/loop_db.py``:

- la migración deja la partición por defecto, el mes en curso y los tres siguientes de
  ``finding_state`` (``occurred_at_bucket``), ``classification`` (``classified_at``) y
  ``review_resolution`` (``resolved_at``), con sus límites en UTC;
- ``create_partitions`` (``PartitionMaintenance`` de U-02, la lista ``PartitionedTable``
  ampliada) crea los meses que faltan de las tres con tres meses por adelantado y es idempotente;
- una fila de un mes sin partición cae en la partición por defecto sin rechazarse, se cuenta en
  ``report_default_rows`` (métrica ``default_partition_rows``) y ese mes queda ``blocked``;
- **imagen N-1**: las sentencias literales de ``create_partitions`` de ``origin/main`` (las
  funciones de nuc_0016 y gob_0018) corren sin error contra este esquema y solo devuelven las
  tablas que su ``PartitionedTable`` conoce;
- las funciones nuevas exigen el contexto del sistema o de un operador.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.identity_db import MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.loop_db import (
    LoopScope,
    LoopSeed,
    classification,
    finding_state,
    review_resolution,
    seed_loop,
)
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.shared.archive.partitions import (
    PARTITION_MONTHS_AHEAD,
    PartitionedTable,
    PartitionMaintenance,
    PartitionReport,
    add_months,
    month_of,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.observability.metrics import MetricName

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE: Final = "42501"
LOOP_PARTITIONED: Final = {
    PartitionedTable.FINDING_STATE: "occurred_at_bucket",
    PartitionedTable.CLASSIFICATION: "classified_at",
    PartitionedTable.REVIEW_RESOLUTION: "resolved_at",
}
"""Las tres tablas particionadas de ``loop`` y su columna de partición."""
BUILDERS: Final = {
    PartitionedTable.FINDING_STATE: finding_state,
    PartitionedTable.CLASSIFICATION: classification,
    PartitionedTable.REVIEW_RESOLUTION: review_resolution,
}


@dataclass(frozen=True)
class World:
    database: MigratedDatabase
    seed: LoopSeed
    month: dt.date
    """Mes en curso de la base (UTC)."""

    @property
    def a(self) -> LoopScope:
        return self.seed.plant(self.seed.identity.a.organization_id)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with seeded_identity(postgres_endpoint, "vigia_laz_partitions") as (database, identity):

        async def prepare() -> World:
            connection = await database.connect()
            try:
                seed = await seed_loop(connection, identity)
                month = await connection.fetchval(
                    "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"
                )
            finally:
                await connection.close()
            return World(database, seed, month)

        yield asyncio.run(prepare())


@pytest_asyncio.fixture
async def superuser(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(world: World) -> AsyncIterator[Any]:
    connection = await world.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la prueba."""


def _partition_name(table: PartitionedTable, month: dt.date) -> str:
    return f"{table.value.split('.')[1]}_{month.year:04d}_{month.month:02d}"


def _bound(month: dt.date) -> str:
    following = add_months(month, 1)
    return (
        f"FOR VALUES FROM ('{month.isoformat()} 00:00:00+00') "
        f"TO ('{following.isoformat()} 00:00:00+00')"
    )


async def _partitions(connection: Any, table: PartitionedTable) -> dict[str, str]:
    rows = await connection.fetch(
        "SELECT child.relname AS name, pg_get_expr(child.relpartbound, child.oid) AS bound"
        " FROM pg_inherits AS inheritance JOIN pg_class AS child"
        " ON child.oid = inheritance.inhrelid WHERE inheritance.inhparent = $1::regclass",
        table.value,
    )
    return {row["name"]: row["bound"] for row in rows}


def _create(world: World, now: dt.datetime, metrics: Any) -> PartitionReport:
    database = app_database(world.database)
    context = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)

    async def run() -> PartitionReport:
        try:
            async with database.transaction(context) as transaction:
                return await PartitionMaintenance(
                    clock=SimulatedClock(now), metrics=metrics
                ).create(transaction)
        finally:
            await database.dispose()

    return asyncio.run(run())


# --- La migración -----


@pytest.mark.asyncio
async def test_the_migration_leaves_default_current_and_three_months_ahead(
    superuser: Any, world: World
) -> None:
    months = [add_months(world.month, step) for step in range(PARTITION_MONTHS_AHEAD + 1)]
    for table, column in LOOP_PARTITIONED.items():
        assert await superuser.fetchval("SELECT pg_get_partkeydef($1::regclass)", table.value) == (
            f"RANGE ({column})"
        )
        found = await _partitions(superuser, table)
        expected = {_partition_name(table, month): _bound(month) for month in months}
        expected[f"{table.value.split('.')[1]}_default"] = "DEFAULT"
        assert found == expected, table


# --- create_partitions -----


def test_create_partitions_maintains_the_loop_tables(world: World) -> None:
    """Los meses que faltan de las tres tablas, con tres por adelantado, y nada la segunda vez."""
    metrics, _ = metrics_with_reader()
    now = dt.datetime(2052, 7, 20, 9, 0, tzinfo=dt.UTC)
    report = _create(world, now, metrics)
    months = [add_months(month_of(now), step) for step in range(PARTITION_MONTHS_AHEAD + 1)]
    created = {(result.table, result.month) for result in report.created}
    for table in LOOP_PARTITIONED:
        assert {(table, month) for month in months} <= created, table
    assert report.blocked == ()
    assert set(report.default_rows) == set(PartitionedTable)
    again = _create(world, now, metrics)
    assert again.created == ()

    async def bounds() -> dict[PartitionedTable, dict[str, str]]:
        connection = await world.database.connect()
        try:
            return {table: await _partitions(connection, table) for table in LOOP_PARTITIONED}
        finally:
            await connection.close()

    found = asyncio.run(bounds())
    for table in LOOP_PARTITIONED:
        for month in months:
            assert found[table][_partition_name(table, month)] == _bound(month)
        # Ni un mes antes del en curso ni uno después del tercero.
        for month in (add_months(month_of(now), -1), add_months(month_of(now), 4)):
            assert _partition_name(table, month) not in found[table]


@pytest.mark.parametrize("table", list(LOOP_PARTITIONED))
def test_a_row_in_the_default_partition_shows_in_default_partition_rows(
    table: PartitionedTable, world: World
) -> None:
    metrics, reader = metrics_with_reader()
    scope = world.a
    # Un mes propio de cada tabla, sin partición: la fila cae en la de por defecto.
    year = {
        PartitionedTable.FINDING_STATE: 2094,
        PartitionedTable.CLASSIFICATION: 2095,
        PartitionedTable.REVIEW_RESOLUTION: 2096,
    }[table]
    instant = dt.datetime(year, 5, 6, 7, 0, tzinfo=dt.UTC)

    async def insert() -> str:
        connection = await world.database.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_scope(connection, scope.organization_id)
                sql, args = BUILDERS[table](scope, instant)
                await connection.execute(sql, *args)
        finally:
            await connection.close()
        owner = await world.database.connect()
        try:
            return str(
                await owner.fetchval(
                    f"SELECT tableoid::regclass::text FROM {table.value}"  # noqa: S608
                    f" WHERE {LOOP_PARTITIONED[table]} = $1",
                    instant,
                )
            )
        finally:
            await owner.close()

    assert asyncio.run(insert()) == f"{table.value}_default"
    report = _create(world, dt.datetime(year, 5, 1, tzinfo=dt.UTC), metrics)
    assert report.default_rows[table] > 0
    published = {
        attributes["table"]: value
        for attributes, value in metric_points(reader, MetricName.DEFAULT_PARTITION_ROWS)
    }
    assert published[table.value] > 0
    for other in LOOP_PARTITIONED:
        assert other.value in published
    # El mes con la fila en la por defecto no se puede crear: queda bloqueado, sin abortar.
    assert (table, dt.date(year, 5, 1)) in {
        (result.table, result.month) for result in report.blocked
    }


# --- Compatibilidad con la imagen N-1 (NFR-NUC-14) -----


class PartitionedTableN1(enum.StrEnum):
    """El ``PartitionedTable`` de ``origin/main`` antes de esta tarea: U-02 y ``fleet``."""

    LEDGER_RECORD = "ledger.ledger_record"
    EVIDENCE = "ledger.evidence"
    AUDIT_ENTRY = "shared.audit_entry"
    HEARTBEAT_HISTORY = "fleet.heartbeat_history"
    ENROLLMENT_ATTEMPT = "fleet.enrollment_attempt"
    FLEET_ALARM = "fleet.fleet_alarm"


N1_CREATE: Final = (
    "SELECT parent, partition, month, created, blocked"
    " FROM shared.vigia_create_month_partitions($1, $2)"
    " UNION ALL SELECT parent, partition, month, created, blocked"
    " FROM shared.vigia_create_fleet_month_partitions($1, $2)"
)
N1_DEFAULT_ROWS: Final = (
    "SELECT parent, row_count FROM shared.vigia_default_partition_rows()"
    " UNION ALL SELECT parent, row_count FROM shared.vigia_fleet_default_partition_rows()"
)
"""Las sentencias de ``create_partitions`` de la imagen N-1, literales."""


@pytest.mark.asyncio
async def test_the_previous_image_create_partitions_still_works_on_schema_n(
    app: Any, superuser: Any
) -> None:
    """La imagen N-1 contra el esquema N: sus funciones devuelven solo sus seis tablas (cada fila
    se convierte a su ``PartitionedTable``) y su ``create_partitions`` confirma las particiones.
    Si laz_0029 ampliara esas funciones a ``loop``, la conversión lanzaría ``ValueError`` y la
    transacción entera se revertiría."""
    first = dt.date(2073, 3, 1)
    last = add_months(first, PARTITION_MONTHS_AHEAD)
    async with app.transaction():
        await set_scope(app, uuid.uuid4(), actor_kind="system")
        created = [
            (PartitionedTableN1(row["parent"]), row["month"], row["created"])
            for row in await app.fetch(N1_CREATE, first, last)
        ]
        defaults = {
            PartitionedTableN1(row["parent"]): row["row_count"]
            for row in await app.fetch(N1_DEFAULT_ROWS)
        }
    assert {table for table, _, _ in created} == set(PartitionedTableN1)
    assert all(was_created for _, _, was_created in created)
    assert set(defaults) == set(PartitionedTableN1)
    for table in PartitionedTableN1:
        assert await superuser.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", f"{table.value}_{first.year:04d}_03"
        ), table
    # Las de loop de esos meses no las crea N-1: las crea la imagen N con su función propia.
    assert await superuser.fetchval("SELECT to_regclass('loop.finding_state_2073_03') IS NULL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM shared.vigia_create_loop_month_partitions('2074-01-01', '2074-01-01')",
        "SELECT * FROM shared.vigia_loop_default_partition_rows()",
    ],
)
async def test_the_loop_partition_functions_need_the_system_or_an_operator(
    statement: str, app: Any, superuser: Any
) -> None:
    for kind in ("user", "provider_user", "node"):
        try:
            async with app.transaction():
                await set_scope(app, uuid.uuid4(), actor_kind=kind)
                await app.fetch(statement)
                raise _Rollback
        except asyncpg.PostgresError as error:
            assert str(error.sqlstate) == INSUFFICIENT_PRIVILEGE, kind
        except _Rollback:
            pytest.fail(f"{kind} pudo ejecutar {statement}")
    assert await superuser.fetchval("SELECT to_regclass('loop.finding_state_2074_01') IS NULL")
