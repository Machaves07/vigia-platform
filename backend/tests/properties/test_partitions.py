"""PR-NUC-43 y la tarea ``create_partitions`` contra PostgreSQL 16 real (TASK-131, LC-NUC-33).

PR-NUC-43: «para cualquier fecha generada dentro de los tres meses siguientes, la partición existe
tras ``create_partitions``; para una fecha fuera del rango cubierto la fila cae en la partición por
defecto, no se rechaza y ``default_partition_rows`` es mayor que cero» (PAT-NUC-ESC-01).

Base migrada hasta ``nuc_0015`` y la tarea como ``vigia_app`` (nunca superusuario): las funciones
de nuc_0015 son de ``vigia_migrate``. El reloj de la tarea es un ``SimulatedClock`` desplazado
meses hacia delante, así que cada ejemplo crea particiones nuevas de verdad; las fechas fuera del
rango son de 2080 en adelante, que ningún ejemplo crea.

- Criterio 3 de la tarea: ejecutar ``create_partitions`` dos veces no falla ni duplica, tampoco
  con dos a cuatro llamadas a la vez desde conexiones distintas (candado consultivo de nuc_0015).
- Cada partición nueva queda protegida como las de la migración (``TRUNCATE`` falla, disparadores
  con ``ENABLE ALWAYS``) y sin privilegios para ``vigia_app``.
- Un mes con filas en la partición por defecto se devuelve como ``blocked`` sin abortar el resto.
- Guardas: solo el sistema o un operador ejecuta la función; el manejador solo actúa en la
  iteración de la organización proveedora.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import text

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop, evidence_values, insert_evidence, set_organization
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.shared.archive.partitions import (
    CREATE_PARTITIONS,
    CREATE_PARTITIONS_SCHEDULE,
    PARTITION_MONTHS_AHEAD,
    PartitionedTable,
    PartitionMaintenance,
    PartitionReport,
    add_months,
    create_partitions_handler,
    month_of,
    register_create_partitions,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.db import Database
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry

pytestmark = pytest.mark.integration

BASE = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
INSUFFICIENT_PRIVILEGE = "42501"
RESTRICT_VIOLATION = "23001"


@dataclass
class Environment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    database: Database
    provider_id: uuid.UUID
    metrics: Any
    reader: InMemoryMetricReader

    def system(self, organization_id: uuid.UUID | None = None) -> ScopeContext:
        return unit_context(
            organization_id or self.provider_id, ActorUnit.U02, kind=ActorKind.SYSTEM
        )

    def maintenance(self, now: datetime) -> PartitionMaintenance:
        return PartitionMaintenance(clock=SimulatedClock(now), metrics=self.metrics)

    def create(
        self, now: datetime, context: ScopeContext | None = None, until: date | None = None
    ) -> PartitionReport:
        maintenance = self.maintenance(now)

        async def run() -> PartitionReport:
            async with self.database.transaction(context or self.system()) as transaction:
                return await maintenance.create(transaction, until=until)

        return self.loop.run(run())

    def fetch(self, query: str, *args: Any) -> list[Any]:
        async def run() -> list[Any]:
            connection = await self.migrated.connect()
            try:
                return list(await connection.fetch(query, *args))
            finally:
                await connection.close()

        return self.loop.run(run())


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[Environment]:
    with migrated_database(postgres_endpoint, "vigia_partitions") as migrated:
        loop = DatabaseLoop()
        database = app_database(migrated)
        metrics, reader = metrics_with_reader()
        try:
            yield Environment(loop, migrated, database, uuid.uuid4(), metrics, reader)
        finally:
            loop.run(database.dispose())
            loop.close()


def partition_bounds(environment: Environment, table: PartitionedTable) -> dict[str, str]:
    """Particiones adjuntas de ``table`` con su expresión de rango (sin la de por defecto)."""
    rows = environment.fetch(
        "SELECT child.relname AS name, pg_get_expr(child.relpartbound, child.oid) AS bound"
        " FROM pg_inherits AS inheritance JOIN pg_class AS child"
        " ON child.oid = inheritance.inhrelid WHERE inheritance.inhparent = $1::regclass",
        table.value,
    )
    return {row["name"]: row["bound"] for row in rows if row["bound"] != "DEFAULT"}


def expected_bound(month: date) -> str:
    following = add_months(month, 1)
    return (
        f"FOR VALUES FROM ('{month.isoformat()} 00:00:00+00') "
        f"TO ('{following.isoformat()} 00:00:00+00')"
    )


def sqlstate(error: BaseException | None) -> str | None:
    """El ``SQLSTATE`` de un error de la base, a través de SQLAlchemy y del adaptador."""
    while error is not None:
        for candidate in (error, getattr(error, "orig", None)):
            code = getattr(candidate, "sqlstate", None)
            if isinstance(code, str):
                return code
        error = error.__cause__
    return None


def partition_name(table: PartitionedTable, month: date) -> str:
    return f"{table.value.split('.')[1]}_{month.year:04d}_{month.month:02d}"


def default_rows_metric(environment: Environment) -> dict[str, float]:
    return {
        attributes["table"]: value
        for attributes, value in metric_points(
            environment.reader, MetricName.DEFAULT_PARTITION_ROWS
        )
    }


def insert_evidence_at(environment: Environment, verified_at: datetime) -> str:
    """Una evidencia sintética con ``verified_at`` dado; devuelve la partición que la recibió."""
    organization_id = uuid.uuid4()

    async def run() -> str:
        connection = await environment.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_organization(connection, organization_id)
                row = await insert_evidence(
                    connection,
                    evidence_values(organization_id, uuid.uuid4(), uuid.uuid4(), verified_at),
                )
                assert row is not None
            # Como superusuario: vigia_app no tiene privilegios sobre las particiones.
            owner = await environment.migrated.connect()
            try:
                found = await owner.fetchval(
                    "SELECT tableoid::regclass::text FROM ledger.evidence WHERE evidence_id = $1",
                    row["evidence_id"],
                )
            finally:
                await owner.close()
            return str(found)
        finally:
            await connection.close()

    return environment.loop.run(run())


# --- PR-NUC-43 -----------------------------------------------------------------------------------


@given(
    months=st.integers(min_value=0, max_value=48),
    offset=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=27, hours=23)),
    ahead=st.integers(min_value=0, max_value=2**53),
)
def test_pr_nuc_43_any_date_within_three_months_has_its_partition(
    environment: Environment, months: int, offset: timedelta, ahead: int
) -> None:
    """Con el reloj en cualquier instante, toda fecha desde ahora hasta el final del tercer mes
    siguiente tiene su partición en las tres tablas tras ``create_partitions``."""
    now = datetime.combine(add_months(month_of(BASE), months), datetime.min.time(), UTC) + offset
    report = environment.create(now)
    end = datetime.combine(
        add_months(month_of(now), PARTITION_MONTHS_AHEAD + 1), datetime.min.time(), UTC
    )
    span = (end - now) // timedelta(milliseconds=1)
    target = now + timedelta(milliseconds=ahead % span)
    assert now <= target < end
    assert report.blocked == ()
    month = month_of(target)
    for table in PartitionedTable:
        bounds = partition_bounds(environment, table)
        assert bounds.get(partition_name(table, month)) == expected_bound(month), table
    # Y una fila de verdad cae en esa partición (evidencia: la marca la pone la aplicación).
    assert (
        insert_evidence_at(environment, target)
        == f"ledger.{partition_name(PartitionedTable.EVIDENCE, month)}"
    )


@given(
    moment=st.datetimes(
        min_value=datetime(2080, 1, 1),  # noqa: DTZ001 - límites ingenuos de Hypothesis
        max_value=datetime(2099, 12, 31),  # noqa: DTZ001
        timezones=st.just(UTC),
    )
)
def test_pr_nuc_43_date_outside_coverage_falls_into_default_and_raises_the_metric(
    environment: Environment, moment: datetime
) -> None:
    """Fuera del rango cubierto la fila no se rechaza: cae en la partición por defecto y
    ``default_partition_rows`` es mayor que cero."""
    received = insert_evidence_at(environment, moment.replace(microsecond=0))
    assert received == "ledger.evidence_default"
    report = environment.create(BASE)
    assert report.default_rows[PartitionedTable.EVIDENCE] > 0
    assert default_rows_metric(environment)[PartitionedTable.EVIDENCE.value] > 0


# --- Criterio 3: dos veces seguidas --------------------------------------------------------------


def test_create_partitions_twice_neither_fails_nor_duplicates(environment: Environment) -> None:
    now = datetime(2040, 3, 15, 12, 0, tzinfo=UTC)
    first = environment.create(now)
    months = [add_months(month_of(now), step) for step in range(PARTITION_MONTHS_AHEAD + 1)]
    assert {(r.table, r.month) for r in first.created} == {
        (table, month) for table in PartitionedTable for month in months
    }
    before = {table: partition_bounds(environment, table) for table in PartitionedTable}
    second = environment.create(now)
    assert second.created == ()
    assert second.blocked == ()
    assert len(second.results) == len(first.results)
    after = {table: partition_bounds(environment, table) for table in PartitionedTable}
    assert after == before
    for table in PartitionedTable:
        names = [partition_name(table, month) for month in months]
        assert all(
            after[table][name] == expected_bound(month)
            for name, month in zip(names, months, strict=True)
        )


@pytest.mark.parametrize("callers", [2, 4])
def test_concurrent_create_partitions_neither_fail_nor_duplicate(
    environment: Environment, callers: int
) -> None:
    """Varias llamadas a la vez (la tarea y ``vigia-admin create-partitions``), desde conexiones
    distintas y sobre meses nuevos: ninguna falla, cada partición la crea una sola llamada y queda
    una sola por tabla y mes. Tres rondas, porque el interbloqueo sin candado no sale siempre."""
    database = app_database(environment.migrated, worker_pool_size=callers)

    async def one(maintenance: PartitionMaintenance) -> PartitionReport:
        async with database.transaction(environment.system()) as transaction:
            return await maintenance.create(transaction)

    async def together(now: datetime) -> list[PartitionReport | BaseException]:
        maintenance = environment.maintenance(now)
        return await asyncio.gather(
            *(one(maintenance) for _ in range(callers)), return_exceptions=True
        )

    try:
        for round_ in range(3):
            # 2060-2062 y 2064-2066: meses que ningún otro ejemplo crea (fuera de rango, ≥ 2080).
            now = datetime(2056 + 2 * callers + round_, 5, 20, 12, 0, tzinfo=UTC)
            reports = environment.loop.run(together(now))
            failures = [report for report in reports if isinstance(report, BaseException)]
            assert failures == [], failures
            months = [add_months(month_of(now), step) for step in range(PARTITION_MONTHS_AHEAD + 1)]
            expected = {(table, month) for table in PartitionedTable for month in months}
            created = [
                (result.table, result.month)
                for report in reports
                if isinstance(report, PartitionReport)
                for result in report.created
            ]
            assert sorted(created) == sorted(expected)
            for table in PartitionedTable:
                bounds = partition_bounds(environment, table)
                for month in months:
                    assert bounds[partition_name(table, month)] == expected_bound(month)
    finally:
        environment.loop.run(database.dispose())


def test_the_margin_is_the_current_month_plus_three_in_utc() -> None:
    """El literal del diseño (mes en curso y tres más, PAT-NUC-ESC-01), no la constante del código;
    y el mes es el de UTC aunque el reloj traiga otra zona."""
    assert PARTITION_MONTHS_AHEAD == 3
    assert PartitionMaintenance(clock=SimulatedClock(BASE)).months_ahead == 3
    minus_five = timezone(timedelta(hours=-5))
    assert month_of(datetime(2026, 12, 31, 23, 30, tzinfo=minus_five)) == date(2027, 1, 1)
    plus_fourteen = timezone(timedelta(hours=14))
    assert month_of(datetime(2027, 1, 1, 9, 0, tzinfo=plus_fourteen)) == date(2026, 12, 1)


def test_new_partitions_are_protected_and_not_granted_to_the_application(
    environment: Environment,
) -> None:
    now = datetime(2041, 6, 1, tzinfo=UTC)
    report = environment.create(now)
    assert len(report.created) == 3 * 4  # tres tablas, el mes en curso y tres más
    for result in report.created:
        rows = environment.fetch(
            "SELECT tgname, tgenabled FROM pg_trigger WHERE tgrelid = $1::regclass"
            " AND NOT tgisinternal",
            result.partition,
        )
        assert rows, result.partition
        assert {bytes(row["tgenabled"]) for row in rows} == {b"A"}, result.partition
        assert "append_only_truncate" in {row["tgname"] for row in rows}
        granted = environment.fetch(
            "SELECT has_table_privilege('vigia_app', $1::regclass, 'SELECT, INSERT') AS granted",
            result.partition,
        )
        assert granted[0]["granted"] is False

    async def truncate(partition: str) -> str | None:
        connection = await environment.migrated.connect("vigia_migrate")
        try:
            await connection.execute(f"TRUNCATE {partition}")
        except asyncpg.PostgresError as error:
            return str(error.sqlstate)
        finally:
            await connection.close()
        return None

    assert environment.loop.run(truncate(report.created[0].partition)) == RESTRICT_VIOLATION


def test_month_with_rows_in_the_default_partition_is_blocked_without_aborting(
    environment: Environment,
) -> None:
    month = date(2090, 7, 1)
    assert insert_evidence_at(environment, datetime(2090, 7, 9, 8, 0, tzinfo=UTC)) == (
        "ledger.evidence_default"
    )
    report = environment.create(datetime(2090, 7, 2, tzinfo=UTC), until=month)
    assert [(r.table, r.month) for r in report.blocked] == [(PartitionedTable.EVIDENCE, month)]
    created = {r.table for r in report.created}
    assert created == {PartitionedTable.LEDGER_RECORD, PartitionedTable.AUDIT_ENTRY}
    assert partition_name(PartitionedTable.EVIDENCE, month) not in partition_bounds(
        environment, PartitionedTable.EVIDENCE
    )


# --- Guardas ------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [ActorKind.USER, ActorKind.NODE, ActorKind.PROVIDER_USER])
def test_only_system_or_operator_creates_partitions(
    environment: Environment, kind: ActorKind
) -> None:
    context = unit_context(environment.provider_id, ActorUnit.U02, kind=kind)
    with pytest.raises(Exception) as raised:
        environment.create(datetime(2042, 1, 1, tzinfo=UTC), context)
    assert sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE
    assert partition_name(PartitionedTable.AUDIT_ENTRY, date(2042, 1, 1)) not in partition_bounds(
        environment, PartitionedTable.AUDIT_ENTRY
    )
    # Cada función por separado: una guarda no puede apoyarse en la de la otra.
    for statement in (
        "SELECT * FROM shared.vigia_create_month_partitions('2042-01-01', '2042-01-01')",
        "SELECT * FROM shared.vigia_default_partition_rows()",
    ):

        async def call(sql: str = statement) -> None:
            async with environment.database.transaction(context) as transaction:
                await transaction.execute(text(sql))

        with pytest.raises(Exception) as raised:
            environment.loop.run(call())
        assert sqlstate(raised.value) == INSUFFICIENT_PRIVILEGE, statement


def test_operator_context_creates_until_a_given_month(environment: Environment) -> None:
    context = unit_context(environment.provider_id, ActorUnit.U02, kind=ActorKind.OPERATOR)
    report = environment.create(datetime(2043, 1, 10, tzinfo=UTC), context, until=date(2043, 8, 1))
    assert {r.month for r in report.created} == {date(2043, m, 1) for m in range(1, 9)}


def test_handler_acts_only_in_the_provider_iteration(environment: Environment) -> None:
    registry = PeriodicTaskRegistry()
    clock = SimulatedClock(datetime(2044, 2, 3, tzinfo=UTC))
    maintenance = PartitionMaintenance(clock=clock, metrics=environment.metrics)
    task = register_create_partitions(
        registry, create_partitions_handler(maintenance, environment.provider_id)
    )
    assert task.task_name == CREATE_PARTITIONS
    assert task.schedule == CREATE_PARTITIONS_SCHEDULE
    february = partition_name(PartitionedTable.LEDGER_RECORD, date(2044, 2, 1))

    async def run(context: ScopeContext) -> None:
        async with environment.database.transaction(context) as transaction:
            await task.handler(transaction)

    environment.loop.run(run(environment.system(uuid.uuid4())))
    assert february not in partition_bounds(environment, PartitionedTable.LEDGER_RECORD)
    environment.loop.run(run(environment.system()))
    assert february in partition_bounds(environment, PartitionedTable.LEDGER_RECORD)
