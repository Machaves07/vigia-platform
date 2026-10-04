"""Esquema ``fleet`` de ``gob_0018`` (TASK-203, LC-GOB-21 parte 2, PAT-GOB-ESC-02, NFR-GOB-16).

Base migrada propia del módulo (``alembic upgrade head`` como proceso aparte) con los datos de
``tests/identity_db.py`` y filas de cada tabla de ``fleet`` en cada planta (``tests/fleet_db.py``).
Criterios de TASK-203:

- ``alembic upgrade head`` aplica ``gob_0018``: esquema, 18 tablas, RLS forzada con sus políticas,
  disparadores y permisos; el mes en curso y los tres siguientes de las tres tablas particionadas,
  protegidos; las definiciones de SQLAlchemy Core coinciden con la base y
  ``migrations/append_only.py`` lista las ⛓.
- ``create_partitions`` crea los meses que faltan de las tres tablas y una fila en la partición
  por defecto se refleja en ``default_partition_rows``.
- Solo anexar: ``DELETE``, ``TRUNCATE`` y ``UPDATE`` fuera de la lista blanca fallan con
  ``vigia_app`` (y con el dueño, por el disparador) en las tablas ⛓ y en sus particiones; los
  cierres pasan una sola vez y los estados solo avanzan.
- Una sola alarma abierta por (clase, nodo) con dos transacciones a la vez; la prueba falla sin la
  ranura (base sin el disparador que la ocupa).
- La consulta de identidad por ``(node_id, certificate_serial)`` usa el índice único con datos a
  escala de NFR-GOB-11 (``EXPLAIN`` sin barrido secuencial).
- Bordes de las restricciones que fija el diseño.

Cada mutación corre en una transacción que se revierte siempre, salvo los datos de escala.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import importlib.util
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_db import (
    APPEND_ONLY_TABLES,
    FLEET_TABLES,
    GLOBAL_TABLE,
    HEADERS,
    PARTITIONED_TABLES,
    REVOCATION_STATE_COLUMNS,
    REVOCATION_STATE_TABLE,
    FleetScope,
    FleetSeed,
    clip_upload_grant,
    enrollment_attempt,
    enrollment_code,
    fleet_alarm,
    heartbeat,
    node_credential,
    seed_fleet,
    storage_key,
    update_result,
    verification_clip,
)
from tests.identity_db import BASE_TIME, MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_REVISION, HEAD_SCHEMA_VERSION
from tests.outbox_support import app_database
from tests.writer_support import unit_context
from vigia_platform.fleet.adapters.postgres import tables as fleet_tables
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

BACKEND = Path(__file__).resolve().parents[2]
INSUFFICIENT_PRIVILEGE = "42501"
RESTRICT_VIOLATION = "23001"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
WAIT_FOR_LOCK_SECONDS = 30.0
FLEET_PARTITIONED = (
    PartitionedTable.HEARTBEAT_HISTORY,
    PartitionedTable.ENROLLMENT_ATTEMPT,
    PartitionedTable.FLEET_ALARM,
)


def _load_migration() -> Any:
    path = BACKEND / "migrations" / "versions" / "gob_0018_fleet_schema.py"
    spec = importlib.util.spec_from_file_location("gob_0018_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


@dataclass(frozen=True)
class Fleet:
    database: MigratedDatabase
    seed: FleetSeed
    month: dt.date
    """Mes en curso de la base (UTC): el de las particiones que dejó la migración."""

    @property
    def a(self) -> FleetScope:
        return self.seed.plant(self.seed.identity.a.organization_id)

    @property
    def now(self) -> dt.datetime:
        """Un instante del mes en curso de la base, para caer en una partición mensual."""
        return dt.datetime(self.month.year, self.month.month, 2, 12, 0, tzinfo=dt.UTC)


def _prepare(database: MigratedDatabase, identity: Any) -> Fleet:
    async def run() -> Fleet:
        connection = await database.connect()
        try:
            seed = await seed_fleet(connection, identity)
            month = await connection.fetchval(
                "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"
            )
            # Filas en una partición mensual de verdad (las de la siembra van a la de por defecto).
            scope = seed.plant(identity.a.organization_id)
            at = dt.datetime(month.year, month.month, 2, 12, 0, tzinfo=dt.UTC)
            async with connection.transaction():
                await set_scope(connection, scope.organization_id)
                for sql, args in (
                    heartbeat(scope, at),
                    enrollment_attempt(scope, at),
                    fleet_alarm(scope, at, kind="certificate_expiring"),
                ):
                    await connection.execute(sql, *args)
        finally:
            await connection.close()
        return Fleet(database, seed, month)

    return asyncio.run(run())


@pytest.fixture(scope="module")
def fleet(postgres_endpoint: PostgresEndpoint) -> Iterator[Fleet]:
    with seeded_identity(postgres_endpoint, "vigia_gob_0018") as (database, identity):
        yield _prepare(database, identity)


@pytest_asyncio.fixture
async def superuser(fleet: Fleet) -> AsyncIterator[Any]:
    connection = await fleet.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(fleet: Fleet) -> AsyncIterator[Any]:
    connection = await fleet.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def owner(fleet: Fleet) -> AsyncIterator[Any]:
    connection = await fleet.database.connect("vigia_migrate")
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la prueba."""


async def _steps(connection: Any, organization_id: uuid.UUID, *steps: tuple[str, list[Any]]) -> str:
    """Pasos en una transacción revertida: ``ok`` o el SQLSTATE del primero que falla."""
    try:
        async with connection.transaction():
            await set_scope(connection, organization_id)
            for sql, args in steps:
                try:
                    await connection.execute(sql, *args)
                except asyncpg.PostgresError as error:
                    return str(error.sqlstate)
            raise _Rollback
    except _Rollback:
        return "ok"


async def _sqlstate(connection: Any, organization_id: uuid.UUID, sql: str, *args: Any) -> str:
    return await _steps(connection, organization_id, (sql, list(args)))


def _partition_name(table: str, month: dt.date) -> str:
    return f"{table}_{month.year:04d}_{month.month:02d}"


async def _partitions(connection: Any, table: str) -> dict[str, str]:
    rows = await connection.fetch(
        "SELECT child.relname AS name, pg_get_expr(child.relpartbound, child.oid) AS bound"
        " FROM pg_inherits AS inheritance JOIN pg_class AS child"
        " ON child.oid = inheritance.inhrelid WHERE inheritance.inhparent = $1::regclass",
        f"fleet.{table}",
    )
    return {row["name"]: row["bound"] for row in rows}


def _bound(month: dt.date) -> str:
    following = add_months(month, 1)
    return (
        f"FOR VALUES FROM ('{month.isoformat()} 00:00:00+00') "
        f"TO ('{following.isoformat()} 00:00:00+00')"
    )


# --- Criterio 1: alembic upgrade head aplica gob_0018 -----


@pytest.mark.asyncio
async def test_upgrade_head_applies_gob_0018(superuser: Any) -> None:
    assert HEAD_SCHEMA_VERSION >= 18
    assert await superuser.fetchval("SELECT version_num FROM public.alembic_version") == (
        HEAD_REVISION
    )
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'fleet'"
        )
        == "vigia_migrate"
    )
    rows = await superuser.fetch(
        "SELECT c.relname, c.relkind::text AS kind, c.relrowsecurity, c.relforcerowsecurity,"
        " pg_get_userbyid(c.relowner) AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'fleet' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
    )
    assert {row["relname"] for row in rows} == {*FLEET_TABLES, GLOBAL_TABLE, REVOCATION_STATE_TABLE}
    # Excepción documentada (gob_0020, TASK-218): la marca global de la lista, sin datos de cliente.
    secured = [row for row in rows if row["relname"] != REVOCATION_STATE_TABLE]
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in secured), rows
    (state,) = [row for row in rows if row["relname"] == REVOCATION_STATE_TABLE]
    assert not state["relrowsecurity"]
    assert {row["owner"] for row in rows} == {"vigia_migrate"}
    assert {row["relname"] for row in rows if row["kind"] == "p"} == set(PARTITIONED_TABLES)

    policies = await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'fleet'"
    )
    by_table: dict[str, dict[str, Any]] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], {})[policy["policyname"]] = policy
    assert set(by_table) == {*FLEET_TABLES, GLOBAL_TABLE}
    for table in FLEET_TABLES:
        named = by_table[table]
        assert set(named) == {"organization_isolation", "provider_concession_scope"}, table
        isolation, provider = named["organization_isolation"], named["provider_concession_scope"]
        assert (isolation["permissive"], isolation["cmd"]) == ("PERMISSIVE", "ALL"), table
        assert "vigia_current_organization()" in isolation["qual"]
        assert (provider["permissive"], provider["cmd"]) == ("RESTRICTIVE", "ALL"), table
        plant = "NULL::uuid" if table == "revocation_list_dirty" else "plant_id"
        for clause in (provider["qual"], provider["with_check"]):
            assert f"rls_provider_scope_allows(organization_id, {plant})" in clause, table
    assert set(by_table[GLOBAL_TABLE]) == {"operator_only"}

    # Ningún DELETE ni TRUNCATE para vigia_app; SELECT en todas, INSERT salvo en la ranura y la
    # marca global.
    for table in (*FLEET_TABLES, GLOBAL_TABLE, REVOCATION_STATE_TABLE):
        privileges = {
            privilege: await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)", f"fleet.{table}", privilege
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
        }
        insert = table not in {"open_fleet_alarm", GLOBAL_TABLE, REVOCATION_STATE_TABLE}
        assert privileges == {
            "SELECT": True,
            "INSERT": insert,
            "DELETE": False,
            "TRUNCATE": False,
        }, table


@pytest.mark.asyncio
async def test_current_month_and_three_ahead_exist_and_are_protected(
    superuser: Any, fleet: Fleet
) -> None:
    for table in PARTITIONED_TABLES:
        bounds = await _partitions(superuser, table)
        assert bounds[f"{table}_default"] == "DEFAULT"
        for step in range(PARTITION_MONTHS_AHEAD + 1):
            month = add_months(fleet.month, step)
            assert bounds[_partition_name(table, month)] == _bound(month), (table, month)
        for name in bounds:
            triggers = await superuser.fetch(
                "SELECT tgname, tgenabled::text AS enabled FROM pg_trigger"
                " WHERE tgrelid = $1::regclass AND NOT tgisinternal",
                f"fleet.{name}",
            )
            enabled = {row["tgname"]: row["enabled"] for row in triggers}
            assert {"append_only_update", "append_only_delete", "append_only_truncate"} <= set(
                enabled
            ), name
            assert set(enabled.values()) == {"A"}, name
            granted = await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, 'SELECT, INSERT')", f"fleet.{name}"
            )
            assert granted is False, name


@pytest.mark.asyncio
async def test_app_update_privileges_are_exactly_the_whitelist(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT table_name, column_name FROM information_schema.column_privileges"
        " WHERE table_schema = 'fleet' AND grantee = 'vigia_app' AND privilege_type = 'UPDATE'"
    )
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(row["table_name"], set()).add(row["column_name"])
    expected = {
        table: set(MIGRATION.app_updatable_columns(table))
        for table in FLEET_TABLES
        if MIGRATION.app_updatable_columns(table)
    }
    expected[GLOBAL_TABLE] = {"crl_number", "published_at", "crl_sha256"}
    expected[REVOCATION_STATE_TABLE] = set(REVOCATION_STATE_COLUMNS)
    assert granted == expected
    # Tablas ⛓ sin cierre y la ranura de la alarma: ningún UPDATE.
    for table in (
        "enrollment_attempt",
        "heartbeat_history",
        "target_version_publication",
        "update_result",
        "open_fleet_alarm",
    ):
        assert table not in granted


def test_append_only_registry_lists_every_append_only_table() -> None:
    spec = importlib.util.spec_from_file_location(
        "append_only_under_test", BACKEND / "migrations" / "append_only.py"
    )
    assert spec is not None and spec.loader is not None
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    listed = {name for name in registry.APPEND_ONLY_TABLES if name.startswith("fleet.")}
    assert listed == {f"fleet.{table}" for table in APPEND_ONLY_TABLES}
    assert set(MIGRATION.APPEND_ONLY_TABLES) == set(APPEND_ONLY_TABLES)
    assert set(MIGRATION.TABLES) == set(FLEET_TABLES)


@pytest.mark.asyncio
async def test_triggers_are_always_enabled(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname, t.tgname, t.tgenabled::text AS tgenabled FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'fleet' AND NOT t.tgisinternal AND NOT c.relispartition"
    )
    triggers: dict[str, dict[str, str]] = {}
    for row in rows:
        triggers.setdefault(row["relname"], {})[row["tgname"]] = row["tgenabled"]
    for table in APPEND_ONLY_TABLES:
        expected = {
            "append_only_update": "A",
            "append_only_delete": "A",
            "append_only_no_truncate": "A",
        }
        if table == "fleet_alarm":
            expected |= {"open_alarm_slot": "A", "release_alarm_slot": "A"}
        assert triggers[table] == expected, table
    for table in ("enrollment_code", "node_credential", "clip_upload_grant"):
        assert triggers[table] == {"state_transition": "A"}, table
    assert set(triggers) == {
        *APPEND_ONLY_TABLES,
        "enrollment_code",
        "node_credential",
        "clip_upload_grant",
    }


@pytest.mark.asyncio
async def test_core_tables_match_the_database(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.table_name, c.column_name, c.is_nullable FROM information_schema.columns c"
        " JOIN pg_class k ON k.relname = c.table_name"
        " JOIN pg_namespace n ON n.oid = k.relnamespace AND n.nspname = c.table_schema"
        " WHERE c.table_schema = 'fleet' AND NOT k.relispartition"
    )
    database: dict[str, dict[str, bool]] = {}
    for row in rows:
        database.setdefault(row["table_name"], {})[row["column_name"]] = row["is_nullable"] == "YES"
    core = {
        table.name: {column.name: bool(column.nullable) for column in table.columns}
        for table in fleet_tables.METADATA.tables.values()
    }
    assert core == database
    primary_keys = {
        row["table_name"]: set(row["columns"])
        for row in await superuser.fetch(
            "SELECT c.relname AS table_name, array_agg(a.attname::text) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
            " WHERE n.nspname = 'fleet' AND i.indisprimary AND NOT c.relispartition"
            " GROUP BY c.relname"
        )
    }
    assert primary_keys == {
        table.name: {column.name for column in table.primary_key.columns}
        for table in fleet_tables.METADATA.tables.values()
    }


@pytest.mark.asyncio
async def test_every_index_starts_with_the_scope_or_is_a_key(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, i.relname AS index_name,"
        " (SELECT array_agg(a.attname::text ORDER BY k.ordinality)"
        "  FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ordinality)"
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS columns,"
        " x.indisunique OR x.indisprimary AS constraint_index"
        " FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid"
        " JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'fleet' AND NOT c.relispartition"
    )
    scoped = {
        row["table_name"] for row in rows if row["columns"][:2] == ["organization_id", "plant_id"]
    }
    # Todas las de planta; la marca por organización y la global solo tienen su clave.
    assert scoped == set(FLEET_TABLES) - {"revocation_list_dirty"}
    loose = [
        row["index_name"]
        for row in rows
        if row["columns"][:2] != ["organization_id", "plant_id"] and not row["constraint_index"]
    ]
    assert loose == []
    identity = [row for row in rows if row["index_name"] == "node_credential_identity"]
    assert identity and identity[0]["constraint_index"]
    assert identity[0]["columns"] == [
        "node_id",
        "certificate_serial",
        "organization_id",
        "plant_id",
        "status",
    ]


# --- create_partitions y default_partition_rows -----


def _create(fleet: Fleet, now: dt.datetime, metrics: Any) -> PartitionReport:
    database = app_database(fleet.database)
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


def test_create_partitions_maintains_the_fleet_tables(fleet: Fleet) -> None:
    """``create_partitions`` crea los meses que faltan de las tres tablas (y no los de antes)."""
    metrics, _ = metrics_with_reader()
    now = dt.datetime(2051, 4, 20, 9, 0, tzinfo=dt.UTC)
    report = _create(fleet, now, metrics)
    months = [add_months(month_of(now), step) for step in range(PARTITION_MONTHS_AHEAD + 1)]
    created = {(result.table, result.month) for result in report.created}
    for table in FLEET_PARTITIONED:
        assert {(table, month) for month in months} <= created
    assert report.blocked == ()
    again = _create(fleet, now, metrics)
    assert again.created == ()

    async def bounds() -> dict[str, dict[str, str]]:
        connection = await fleet.database.connect()
        try:
            return {table: await _partitions(connection, table) for table in PARTITIONED_TABLES}
        finally:
            await connection.close()

    found = asyncio.run(bounds())
    for table in PARTITIONED_TABLES:
        for month in months:
            assert found[table][_partition_name(table, month)] == _bound(month)


def test_a_row_in_the_default_partition_shows_in_default_partition_rows(fleet: Fleet) -> None:
    metrics, reader = metrics_with_reader()
    scope = fleet.a

    async def insert() -> str:
        connection = await fleet.database.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_scope(connection, scope.organization_id)
                sql, args = heartbeat(scope, dt.datetime(2093, 5, 6, 7, 0, tzinfo=dt.UTC))
                await connection.execute(sql, *args)
        finally:
            await connection.close()
        owner = await fleet.database.connect()
        try:
            return str(
                await owner.fetchval(
                    "SELECT tableoid::regclass::text FROM fleet.heartbeat_history"
                    " WHERE received_at = '2093-05-06 07:00+00'"
                )
            )
        finally:
            await owner.close()

    assert asyncio.run(insert()) == "fleet.heartbeat_history_default"
    report = _create(fleet, dt.datetime(2093, 5, 1, tzinfo=dt.UTC), metrics)
    assert report.default_rows[PartitionedTable.HEARTBEAT_HISTORY] > 0
    published = {
        attributes["table"]: value
        for attributes, value in metric_points(reader, MetricName.DEFAULT_PARTITION_ROWS)
    }
    assert published[PartitionedTable.HEARTBEAT_HISTORY.value] > 0
    for table in (PartitionedTable.ENROLLMENT_ATTEMPT, PartitionedTable.FLEET_ALARM):
        assert table.value in published
    # El mes con la fila en la por defecto no se puede crear: queda bloqueado, sin abortar.
    assert (PartitionedTable.HEARTBEAT_HISTORY, dt.date(2093, 5, 1)) in {
        (result.table, result.month) for result in report.blocked
    }


# --- Compatibilidad con la imagen N-1 (NFR-GOB-22, NFR-NUC-14) -----


class PartitionedTableN1(enum.StrEnum):
    """El ``PartitionedTable`` de la imagen anterior (U-02, nuc_0016): solo sus tres tablas."""

    LEDGER_RECORD = "ledger.ledger_record"
    EVIDENCE = "ledger.evidence"
    AUDIT_ENTRY = "shared.audit_entry"


N1_CREATE = (
    "SELECT parent, partition, month, created, blocked"
    " FROM shared.vigia_create_month_partitions($1, $2)"
)
N1_DEFAULT_ROWS = "SELECT parent, row_count FROM shared.vigia_default_partition_rows()"
"""Las sentencias de ``create_partitions`` de la imagen N-1, literales."""


@pytest.mark.asyncio
async def test_the_previous_image_create_partitions_still_works_on_schema_n(
    app: Any, superuser: Any, fleet: Fleet
) -> None:
    """La imagen N-1 contra el esquema N: sus dos funciones devuelven solo sus tres tablas (cada
    fila se convierte a su ``PartitionedTable``) y su ``create_partitions`` confirma las
    particiones. Si gob_0018 ampliara esas funciones a ``fleet``, la conversión lanzaría
    ``ValueError`` y la transacción entera se revertiría."""
    first = dt.date(2071, 3, 1)
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
    # Las de fleet de esos meses no las crea N-1: las crea la imagen N con su función propia.
    assert await superuser.fetchval("SELECT to_regclass('fleet.heartbeat_history_2071_03') IS NULL")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM shared.vigia_create_fleet_month_partitions('2072-01-01', '2072-01-01')",
        "SELECT * FROM shared.vigia_fleet_default_partition_rows()",
    ],
)
async def test_the_fleet_partition_functions_need_the_system_or_an_operator(
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
    assert await superuser.fetchval("SELECT to_regclass('fleet.heartbeat_history_2072_01') IS NULL")


# --- Solo anexar -----


async def _monthly_partition(fleet: Fleet, table: str) -> str:
    return f"fleet.{_partition_name(table, fleet.month)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_delete_and_truncate_fail_for_the_app_and_the_owner(
    table: str, app: Any, owner: Any, fleet: Fleet
) -> None:
    organization_id = fleet.a.organization_id
    targets = [f"fleet.{table}"]
    if table in PARTITIONED_TABLES:
        targets += [await _monthly_partition(fleet, table), f"fleet.{table}_default"]
    for target in targets:
        for statement in (f"DELETE FROM {target}", f"TRUNCATE {target}"):  # noqa: S608
            assert await _sqlstate(app, organization_id, statement) == INSUFFICIENT_PRIVILEGE, (
                statement
            )
            # El dueño tiene todos los privilegios: lo para el disparador.
            assert await _sqlstate(owner, organization_id, statement) == RESTRICT_VIOLATION, (
                statement
            )


_NON_WHITELISTED = {
    "enrollment_attempt": "result = 'rate_limited'",
    "heartbeat_history": "payload_summary = '{}'",
    "fleet_alarm": "alarm_kind = 'node_mute'",
    "target_version_publication": "target_version = '9.9.9'",
    "update_result": "result = 'applied'",
    "verification_clip": "sha256 = repeat('0', 64)",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_update_outside_the_whitelist_fails(
    table: str, app: Any, owner: Any, fleet: Fleet
) -> None:
    organization_id = fleet.a.organization_id
    change = _NON_WHITELISTED[table]
    statement = f"UPDATE fleet.{table} SET {change}"  # noqa: S608
    # vigia_app no tiene UPDATE en esa columna; el dueño sí, y lo para el disparador.
    assert await _sqlstate(app, organization_id, statement) == INSUFFICIENT_PRIVILEGE
    assert await _sqlstate(owner, organization_id, statement) == RESTRICT_VIOLATION
    if table in PARTITIONED_TABLES:
        partition = await _monthly_partition(fleet, table)
        on_partition = f"UPDATE {partition} SET {change}"  # noqa: S608
        assert await _sqlstate(app, organization_id, on_partition) == INSUFFICIENT_PRIVILEGE
        assert await _sqlstate(owner, organization_id, on_partition) == RESTRICT_VIOLATION


@pytest.mark.asyncio
async def test_alarm_clearance_is_written_once(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    alarm_id = uuid.uuid4()
    raise_sql, raise_args = fleet_alarm(
        scope, fleet.now, kind="queue_over_threshold", cleared=False, alarm_id=alarm_id
    )
    clear = (
        "UPDATE fleet.fleet_alarm SET cleared_at = $2, cleared_event_id = $3 WHERE alarm_id = $1",
    )

    def clearing(minutes: int) -> tuple[str, list[Any]]:
        return clear[0], [alarm_id, fleet.now + dt.timedelta(minutes=minutes), uuid.uuid4()]

    assert await _steps(app, scope.organization_id, (raise_sql, raise_args), clearing(1)) == "ok"
    assert (
        await _steps(app, scope.organization_id, (raise_sql, raise_args), clearing(1), clearing(2))
        == RESTRICT_VIOLATION
    )
    # Volver a nulo tampoco.
    back = (
        "UPDATE fleet.fleet_alarm SET cleared_at = NULL, cleared_event_id = NULL"
        " WHERE alarm_id = $1",
        [alarm_id],
    )
    assert (
        await _steps(app, scope.organization_id, (raise_sql, raise_args), clearing(1), back)
        == RESTRICT_VIOLATION
    )
    # Medio cierre: la base exige los dos campos a la vez.
    half = (
        "UPDATE fleet.fleet_alarm SET cleared_at = $2 WHERE alarm_id = $1",
        [alarm_id, fleet.now + dt.timedelta(minutes=1)],
    )
    assert (
        await _steps(app, scope.organization_id, (raise_sql, raise_args), half) == CHECK_VIOLATION
    )


@pytest.mark.asyncio
async def test_blur_check_result_closes_once(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    clip_id = uuid.uuid4()
    grant = clip_upload_grant(scope, clip_id, purpose="verification", status="used")
    clip = (
        "INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id, zone_id,"
        " node_id, received_at, sha256) VALUES ($1, $2, $3, $4, $5, $6, repeat('a', 64))",
        [clip_id, scope.organization_id, scope.plant_id, scope.zone_id, scope.node_id, BASE_TIME],
    )

    def result(value: str) -> tuple[str, list[Any]]:
        return (
            "UPDATE fleet.verification_clip SET blur_check_result = $2 WHERE clip_id = $1",
            [clip_id, json.dumps({"passed": value})],
        )

    assert await _steps(app, scope.organization_id, grant, clip, result("yes")) == "ok"
    assert (
        await _steps(app, scope.organization_id, grant, clip, result("yes"), result("no"))
        == RESTRICT_VIOLATION
    )


@pytest.mark.asyncio
async def test_enrollment_code_only_moves_forward(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    code_id = uuid.uuid4()
    issue = enrollment_code(scope, code_id, status="active")

    def to(status: str) -> tuple[str, list[Any]]:
        return (
            "UPDATE fleet.enrollment_code SET status = $2 WHERE code_id = $1",
            [code_id, status],
        )

    for final in ("used", "expired", "superseded"):
        assert await _steps(app, scope.organization_id, issue, to(final)) == "ok", final
        for back in ("active", "used", "expired", "superseded"):
            if back != final:
                assert (
                    await _steps(app, scope.organization_id, issue, to(final), to(back))
                    == RESTRICT_VIOLATION
                ), (final, back)
    # Un solo código activo por nodo; otro estado no choca.
    assert (
        await _steps(app, scope.organization_id, issue, enrollment_code(scope, status="active"))
        == UNIQUE_VIOLATION
    )
    assert await _steps(app, scope.organization_id, issue, enrollment_code(scope)) == "ok"
    # Ninguna otra columna cambia (el hash nunca se reescribe).
    rehash = (
        "UPDATE fleet.enrollment_code SET code_hash = repeat('f', 64) WHERE code_id = $1",
        [code_id],
    )
    assert await _steps(app, scope.organization_id, issue, rehash) == INSUFFICIENT_PRIVILEGE


@pytest.mark.asyncio
async def test_node_credential_only_moves_forward(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    credential_id = uuid.uuid4()
    issue = node_credential(scope, credential_id)

    def to(status: str, revoked: bool = False) -> tuple[str, list[Any]]:
        return (
            "UPDATE fleet.node_credential SET status = $2,"
            " revoked_at = CASE WHEN $3::boolean THEN issued_at + interval '1 day' END"
            " WHERE credential_id = $1",
            [credential_id, status, revoked],
        )

    org = scope.organization_id
    assert await _steps(app, org, issue, to("overlapping"), to("revoked", True)) == "ok"
    assert await _steps(app, org, issue, to("overlapping"), to("superseded")) == "ok"
    assert await _steps(app, org, issue, to("revoked", True)) == "ok"
    assert await _steps(app, org, issue, to("superseded")) == "ok"
    assert await _steps(app, org, issue, to("overlapping"), to("active")) == RESTRICT_VIOLATION
    assert await _steps(app, org, issue, to("revoked", True), to("active")) == RESTRICT_VIOLATION
    # revoked exige su fecha, y la fecha no vuelve a cambiar.
    assert await _steps(app, org, issue, to("revoked")) == CHECK_VIOLATION
    again = (
        "UPDATE fleet.node_credential SET revoked_at = revoked_at + interval '1 hour'"
        " WHERE credential_id = $1",
        [credential_id],
    )
    assert await _steps(app, org, issue, to("revoked", True), again) == RESTRICT_VIOLATION


@pytest.mark.asyncio
async def test_clip_upload_grant_moves_forward_and_verification_never_orphans(
    app: Any, fleet: Fleet
) -> None:
    scope = fleet.a
    clip_id = uuid.uuid4()
    org = scope.organization_id

    def to(status: str, used: bool, orphaned: bool) -> tuple[str, list[Any]]:
        return (
            "UPDATE fleet.clip_upload_grant SET status = $2,"
            " used_at = CASE WHEN $3::boolean THEN issued_at + interval '1 minute' END,"
            " orphaned_at = CASE WHEN $4::boolean THEN issued_at + interval '1 day' END"
            " WHERE clip_id = $1",
            [clip_id, status, used, orphaned],
        )

    issue = clip_upload_grant(scope, clip_id)
    assert await _steps(app, org, issue, to("used", True, False)) == "ok"
    assert await _steps(app, org, issue, to("used", True, False), to("orphan", True, True)) == "ok"
    assert await _steps(app, org, issue, to("expired", False, False)) == "ok"
    assert (
        await _steps(app, org, issue, to("expired", False, False), to("used", True, False))
        == RESTRICT_VIOLATION
    )
    assert await _steps(app, org, issue, to("orphan", True, True)) == RESTRICT_VIOLATION
    verification = clip_upload_grant(scope, clip_id, purpose="verification")
    assert (
        await _steps(app, org, verification, to("used", True, False), to("orphan", True, True))
        == CHECK_VIOLATION
    )


# --- Bordes de las restricciones -----


def _grant_with(scope: FleetScope, **changes: Any) -> tuple[str, list[Any]]:
    clip_id = uuid.uuid4()
    values = {
        "storage_key": storage_key(scope, clip_id),
        "max_size_bytes": 52_428_800,
        "headers": dict(HEADERS),
        "expires": dt.timedelta(minutes=15),
        "content_type": "video/mp4",
    }
    values |= changes
    return (
        "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
        " node_id, storage_key, content_type, max_size_bytes, required_headers, issued_at,"
        " expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        [
            clip_id,
            scope.organization_id,
            scope.plant_id,
            scope.zone_id,
            scope.node_id,
            values["storage_key"].replace("{clip}", str(clip_id)),
            values["content_type"],
            values["max_size_bytes"],
            json.dumps(values["headers"]),
            BASE_TIME,
            BASE_TIME + values["expires"],
        ],
    )


@pytest.mark.asyncio
async def test_clip_upload_grant_bounds(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    org = scope.organization_id
    nine = {**HEADERS, **{f"x-amz-meta-extra-{i}": "1" for i in range(7)}}
    eight = dict(list(nine.items())[:8])
    cases = {
        "en el límite": ({}, "ok"),
        "50 MB + 1": ({"max_size_bytes": 52_428_801}, CHECK_VIOLATION),
        "tamaño cero": ({"max_size_bytes": 0}, CHECK_VIOLATION),
        "ocho cabeceras": ({"headers": eight}, "ok"),
        "nueve cabeceras": ({"headers": nine}, CHECK_VIOLATION),
        "sin la suma": ({"headers": {"x-amz-meta-vigia-anonymized": "true"}}, CHECK_VIOLATION),
        "sin la marca de anonimizado": (
            {"headers": {"x-amz-checksum-sha256": "c2hh"}},
            CHECK_VIOLATION,
        ),
        "cabeceras no objeto": ({"headers": ["x-amz-checksum-sha256"]}, CHECK_VIOLATION),
        "15 min + 1 ms": ({"expires": dt.timedelta(minutes=15, milliseconds=1)}, CHECK_VIOLATION),
        "vence al emitirse": ({"expires": dt.timedelta(0)}, CHECK_VIOLATION),
        "otro tipo": ({"content_type": "video/quicktime"}, CHECK_VIOLATION),
        "clave de otra zona": (
            {
                "storage_key": storage_key(scope, uuid.uuid4()).replace(
                    str(scope.zone_id), str(uuid.uuid4())
                )
            },
            CHECK_VIOLATION,
        ),
        "clave de otro clip": ({"storage_key": storage_key(scope, uuid.uuid4())}, CHECK_VIOLATION),
    }
    for name, (changes, expected) in cases.items():
        sql, args = _grant_with(scope, **changes)
        assert await _steps(app, org, (sql, args)) == expected, name
    # La clave es única aunque el clip sea otro: imposible por el patrón, que incluye el clip.


@pytest.mark.asyncio
async def test_node_configuration_bounds(app: Any, fleet: Fleet, superuser: Any) -> None:
    scope = fleet.a
    org = scope.organization_id
    defaults = await superuser.fetchrow(
        "SELECT sent_records_retention_days, token_max_age_seconds, heartbeat_interval_seconds,"
        " mute_after_seconds, grouping_window_ms FROM fleet.node_configuration WHERE node_id = $1",
        scope.node_id,
    )
    assert tuple(defaults) == (30, 600, 60, 300, 3000)

    def interval(seconds: int, mute: int) -> tuple[str, list[Any]]:
        return (
            "UPDATE fleet.node_configuration SET heartbeat_interval_seconds = $2,"
            " mute_after_seconds = $3 WHERE node_id = $1",
            [scope.node_id, seconds, mute],
        )

    assert await _steps(app, org, interval(15, 75)) == "ok"
    assert await _steps(app, org, interval(600, 3000)) == "ok"
    assert await _steps(app, org, interval(14, 70)) == CHECK_VIOLATION
    assert await _steps(app, org, interval(601, 3005)) == CHECK_VIOLATION
    assert await _steps(app, org, interval(60, 301)) == CHECK_VIOLATION
    for sources, expected in (("[]", CHECK_VIOLATION), (json.dumps([{}] * 9), CHECK_VIOLATION)):
        assert (
            await _sqlstate(
                app,
                org,
                "UPDATE fleet.node_configuration SET time_sources = $2 WHERE node_id = $1",
                scope.node_id,
                sources,
            )
            == expected
        )


@pytest.mark.asyncio
async def test_thresholds_default_to_the_design_values(superuser: Any, fleet: Fleet) -> None:
    row = await superuser.fetchrow(
        "SELECT queue_pending_threshold, queue_age_threshold_minutes, clock_drift_threshold_ms"
        " FROM fleet.plant_fleet_thresholds WHERE plant_id = $1",
        fleet.a.plant_id,
    )
    assert tuple(row) == (100, 30, 5000)


@pytest.mark.asyncio
async def test_node_fleet_record_bounds(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    org = scope.organization_id

    def update(assignments: str, *values: Any) -> tuple[str, list[Any]]:
        return (
            f"UPDATE fleet.node_fleet_record SET {assignments} WHERE node_id = $1",  # noqa: S608
            [scope.node_id, *values],
        )

    url = "https://192.168.10.20:8443/"
    long_host = "a" * (256 - len("https://:8443/"))
    assert await _steps(app, org, update("live_view_local_url = $2", url)) == "ok"
    assert (
        await _steps(app, org, update("live_view_local_url = $2", f"https://{long_host}:8443/"))
        == "ok"
    )
    for bad in (
        f"https://{long_host}a:8443/",  # 257 caracteres
        "http://192.168.10.20:8443/",
        "https://192.168.10.20:8443/?x=1",
        "https://192.168.10.20:8443/#f",
        "https://user@192.168.10.20:8443/",
        "https://192.168.10.20:8443/ruta",
    ):
        assert await _steps(app, org, update("live_view_local_url = $2", bad)) == CHECK_VIOLATION, (
            bad
        )
    reason = "Motivo sintético de la revocación"
    revoke = update("revoked_at = $2, revocation_reason_es = $3", BASE_TIME, reason)
    assert await _steps(app, org, revoke) == "ok"
    assert (
        await _steps(app, org, update("revoked_at = $2", BASE_TIME)) == CHECK_VIOLATION
    )  # sin motivo
    assert (
        await _steps(app, org, update("revoked_at = $2, revocation_reason_es = 'corto'", BASE_TIME))
        == CHECK_VIOLATION
    )
    # La baja exige el nodo revocado, y después de la revocación.
    decommission = update("decommissioned_at = $2", BASE_TIME + dt.timedelta(days=1))
    assert await _steps(app, org, decommission) == CHECK_VIOLATION
    assert await _steps(app, org, revoke, decommission) == "ok"
    early = update("decommissioned_at = $2", BASE_TIME - dt.timedelta(days=1))
    assert await _steps(app, org, revoke, early) == CHECK_VIOLATION
    for fingerprint in ("AB" * 32, "ab" * 31, "zz" * 32):
        assert (
            await _steps(app, org, update("hardware_fingerprint = $2", fingerprint))
            == CHECK_VIOLATION
        ), fingerprint


@pytest.mark.asyncio
async def test_enrollment_rows_bounds(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    org = scope.organization_id
    sql, args = enrollment_code(scope)
    for position, value in ((5, secrets.token_bytes(15)), (5, secrets.token_bytes(17))):
        changed = list(args)
        changed[position] = value
        assert await _steps(app, org, (sql, changed)) == CHECK_VIOLATION
    # Vence exactamente a las 24 horas.
    bad_expiry = sql.replace("interval '24 hours'", "interval '25 hours'")
    assert await _steps(app, org, (bad_expiry, args)) == CHECK_VIOLATION
    attempt_sql, attempt_args = enrollment_attempt(scope)
    # Nodo sin planta (o planta sin nodo): la base no deja una mitad.
    half = list(attempt_args)
    half[2] = None
    assert await _steps(app, org, (attempt_sql, half)) == CHECK_VIOLATION
    for version in ("1.4", "01.4.0", "1.4.0-", "v1.4.0"):
        changed = list(attempt_args)
        changed[6] = version
        assert await _steps(app, org, (attempt_sql, changed)) == CHECK_VIOLATION, version
    # Un intento aceptado tiene nodo; uno rechazado de un código desconocido no.
    unknown_sql, unknown_args = enrollment_attempt(scope, known_node=False)
    assert await _steps(app, org, (unknown_sql, unknown_args)) == "ok"
    accepted = list(unknown_args)
    accepted[7] = "accepted"
    assert await _steps(app, org, (unknown_sql, accepted)) == CHECK_VIOLATION
    for result in ("accepted", "rate_limited"):
        known = list(attempt_args)
        known[7] = result
        assert await _steps(app, org, (attempt_sql, known)) == "ok", result
    known = list(attempt_args)
    known[7] = "node_zone_mismatch"
    assert await _steps(app, org, (attempt_sql, known)) == CHECK_VIOLATION


@pytest.mark.asyncio
async def test_update_result_admits_failed(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    sql, args = update_result(scope)
    for result, expected in (
        ("applied", "ok"),
        ("reverted", "ok"),
        ("failed", "ok"),
        ("skipped", CHECK_VIOLATION),
    ):
        assert (
            await _steps(app, scope.organization_id, (sql.replace("'failed'", f"'{result}'"), args))
            == expected
        ), result


@pytest.mark.asyncio
async def test_node_credential_subject_and_serial(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    org = scope.organization_id
    serial = secrets.token_hex(20)
    sql, args = node_credential(scope, serial=serial)
    assert await _steps(app, org, (sql, args), node_credential(scope, serial=serial)) == (
        UNIQUE_VIOLATION
    )
    other_plant = sql.replace("'plant_id', $3::uuid::text", "'plant_id', gen_random_uuid()::text")
    assert await _steps(app, org, (other_plant, args)) == CHECK_VIOLATION
    for bad in ("A" * 20, "", "0" * 65, "12:34"):
        assert await _steps(app, org, node_credential(scope, serial=bad)) == CHECK_VIOLATION, bad


@pytest.mark.asyncio
async def test_rows_must_match_their_node_plant_and_organization(app: Any, fleet: Fleet) -> None:
    """Las claves foráneas de ``identity``: una planta que no es la del nodo no entra."""
    scope = fleet.a
    other = fleet.seed.plant(fleet.seed.identity.a.organization_id, 1)
    mixed = FleetScope(
        scope.organization_id, other.plant_id, scope.zone_id, scope.node_id, scope.user_id
    )
    for builder in (heartbeat, update_result, node_credential):
        assert await _steps(app, scope.organization_id, builder(mixed)) == FOREIGN_KEY_VIOLATION


@pytest.mark.asyncio
async def test_a_verification_clip_has_the_scope_of_its_grant(app: Any, fleet: Fleet) -> None:
    """``verification_clip_grant_fkey``: la concesión ``clip_id`` del ``FleetScope`` (planta 0)
    solo admite su clip con su misma organización, planta, zona y nodo; con los de la planta 1
    (coherentes con ``identity``) la base lo rechaza."""
    scope = fleet.a
    other = fleet.seed.plant(fleet.seed.identity.a.organization_id, 1)
    elsewhere = FleetScope(
        scope.organization_id,
        other.plant_id,
        other.zone_id,
        other.node_id,
        scope.user_id,
        scope.clip_id,
    )
    org = scope.organization_id
    assert await _steps(app, org, verification_clip(scope)) == "ok"
    assert await _steps(app, org, verification_clip(elsewhere)) == FOREIGN_KEY_VIOLATION


@pytest.mark.asyncio
async def test_global_revocation_list_mark_is_operator_only(app: Any, fleet: Fleet) -> None:
    async def count(kind: str) -> int:
        async with app.transaction():
            await set_scope(app, fleet.a.organization_id, actor_kind=kind)
            return int(await app.fetchval(f"SELECT count(*) FROM fleet.{GLOBAL_TABLE}"))  # noqa: S608

    assert await count("operator") == 1
    for kind in ("user", "system", "provider_user", "node"):
        assert await count(kind) == 0, kind
    publish = (
        f"UPDATE fleet.{GLOBAL_TABLE} SET crl_number = crl_number + 1, published_at = now(),"  # noqa: S608
        " crl_sha256 = repeat('a', 64) RETURNING crl_number"
    )
    try:
        async with app.transaction():
            await set_scope(app, fleet.a.organization_id, actor_kind="operator")
            assert await app.fetchval(publish) == 1
            raise _Rollback
    except _Rollback:
        pass
    try:
        async with app.transaction():
            await set_scope(app, fleet.a.organization_id, actor_kind="user")
            assert await app.fetchval(publish) is None
            raise _Rollback
    except _Rollback:
        pass
    insert = f"INSERT INTO fleet.{GLOBAL_TABLE} VALUES (false)"  # noqa: S608
    assert await _sqlstate(app, fleet.a.organization_id, insert) == INSUFFICIENT_PRIVILEGE


# --- Consulta de identidad por (node_id, certificate_serial) -----


SCALE_NODES = 100
"""NFR-GOB-11: 100 nodos."""
CREDENTIALS_PER_NODE = 25
"""Rotaciones anuales, re-altas y solapamientos de varios años ``[estimación propia]``."""


@pytest.mark.asyncio
async def test_identity_lookup_uses_the_unique_index_at_scale(
    superuser: Any, app: Any, fleet: Fleet
) -> None:
    scope = fleet.a
    nodes = [uuid.uuid4() for _ in range(SCALE_NODES)]
    await superuser.executemany(
        "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
        " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
        [
            (node, scope.organization_id, scope.plant_id, f"ND-{node.hex[:20].upper()}", BASE_TIME)
            for node in nodes
        ],
    )
    rows = []
    for node in nodes:
        for index in range(CREDENTIALS_PER_NODE):
            status = "active" if index == CREDENTIALS_PER_NODE - 1 else "superseded"
            rows.append((uuid.uuid4(), node, secrets.token_hex(20), status))
    await superuser.executemany(
        "INSERT INTO fleet.node_credential (credential_id, organization_id, plant_id, node_id,"
        " certificate_serial, subject, issued_at, expires_at, status)"
        " VALUES ($1, $5, $6, $2, $3, jsonb_build_object('node_id', $2::uuid::text,"
        " 'organization_id', $5::uuid::text, 'plant_id', $6::uuid::text), $7::timestamptz,"
        " $7::timestamptz + interval '365 days', $4)",
        [(*row, scope.organization_id, scope.plant_id, BASE_TIME) for row in rows],
    )
    await superuser.execute("ANALYZE fleet.node_credential")
    node, serial = rows[len(rows) // 2][1], rows[len(rows) // 2][2]
    async with app.transaction():
        await set_scope(app, scope.organization_id)
        plan = await app.fetchval(
            "EXPLAIN (FORMAT JSON) SELECT organization_id, plant_id, status"
            " FROM fleet.node_credential WHERE node_id = $1 AND certificate_serial = $2",
            node,
            serial,
        )
        found = await app.fetchrow(
            "SELECT status FROM fleet.node_credential WHERE node_id = $1"
            " AND certificate_serial = $2",
            node,
            serial,
        )
    text_plan = json.dumps(json.loads(plan))
    assert "Seq Scan" not in text_plan, text_plan
    assert "node_credential_identity" in text_plan, text_plan
    assert found is not None


# --- Una sola alarma abierta por (clase, nodo): concurrencia -----


async def _race_two_alarms(database: MigratedDatabase, scope: FleetScope, kind: str) -> list[str]:
    """Dos transacciones abren a la vez la misma alarma (clase, nodo): la primera escribe y sigue
    abierta, la segunda espera su candado y se confirma la primera."""
    first = await database.connect("vigia_app")
    second = await database.connect("vigia_app")
    observer = await database.connect()
    try:
        transaction = first.transaction()
        await transaction.start()
        try:
            await set_scope(first, scope.organization_id)
            sql, args = fleet_alarm(scope, BASE_TIME, kind=kind, cleared=False)
            await first.execute(sql, *args)

            async def contender() -> str:
                try:
                    async with second.transaction():
                        await set_scope(second, scope.organization_id)
                        other_sql, other_args = fleet_alarm(
                            scope, BASE_TIME + dt.timedelta(seconds=1), kind=kind, cleared=False
                        )
                        await second.execute(other_sql, *other_args)
                except asyncpg.PostgresError as error:
                    return str(error.sqlstate)
                return "ok"

            task = asyncio.create_task(contender())

            async def blocked() -> None:
                pid = second.get_server_pid()
                while not task.done():
                    if await observer.fetchval(
                        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1",
                        pid,
                    ):
                        return
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(blocked(), WAIT_FOR_LOCK_SECONDS)
        except BaseException:
            await transaction.rollback()
            raise
        await transaction.commit()
        return ["ok", await task]
    finally:
        for connection in (first, second, observer):
            await connection.close()


async def _open_alarms(database: MigratedDatabase, scope: FleetScope, kind: str) -> int:
    connection = await database.connect()
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM fleet.fleet_alarm WHERE node_id = $1 AND alarm_kind = $2"
                " AND cleared_at IS NULL",
                scope.node_id,
                kind,
            )
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["camera_below_min_fps", "version_retiring", "orphan_clips_growing"]
)
async def test_two_concurrent_raises_leave_one_open_alarm(kind: str, fleet: Fleet) -> None:
    scope = fleet.a
    assert await _race_two_alarms(fleet.database, scope, kind) == ["ok", UNIQUE_VIOLATION]
    assert await _open_alarms(fleet.database, scope, kind) == 1


@pytest.mark.asyncio
async def test_clearing_frees_the_slot_and_a_new_alarm_opens(app: Any, fleet: Fleet) -> None:
    scope = fleet.a
    org = scope.organization_id
    alarm_id = uuid.uuid4()
    first = fleet_alarm(
        scope, fleet.now, kind="simulated_adapter_in_productive", cleared=False, alarm_id=alarm_id
    )
    second = fleet_alarm(
        scope,
        fleet.now + dt.timedelta(hours=1),
        kind="simulated_adapter_in_productive",
        cleared=False,
    )
    clear = (
        "UPDATE fleet.fleet_alarm SET cleared_at = raised_at + interval '1 minute',"
        " cleared_event_id = gen_random_uuid() WHERE alarm_id = $1",
        [alarm_id],
    )
    assert await _steps(app, org, first, second) == UNIQUE_VIOLATION
    assert await _steps(app, org, first, clear, second) == "ok"
    # Otra clase u otro nodo no comparten ranura.
    assert (
        await _steps(
            app, org, first, fleet_alarm(scope, fleet.now, kind="clock_drift", cleared=False)
        )
        == "ok"
    )
    # La ranura no se escribe directamente.
    assert (
        await _sqlstate(app, org, "UPDATE fleet.open_fleet_alarm SET alarm_id = NULL")
        == INSUFFICIENT_PRIVILEGE
    )


@pytest.fixture(scope="module")
def unguarded(postgres_endpoint: PostgresEndpoint) -> Iterator[Fleet]:
    """Otra base igual, sin el disparador que ocupa la ranura (sonda negativa)."""
    with seeded_identity(postgres_endpoint, "vigia_gob_0018_unguarded") as (database, identity):

        async def drop() -> None:
            connection = await database.connect()
            try:
                await connection.execute("DROP TRIGGER open_alarm_slot ON fleet.fleet_alarm")
            finally:
                await connection.close()

        asyncio.run(drop())
        yield _prepare(database, identity)


@pytest.mark.asyncio
async def test_without_the_slot_two_alarms_stay_open(unguarded: Fleet) -> None:
    scope = unguarded.a
    kind = "camera_below_min_fps"
    assert await _race_two_alarms(unguarded.database, scope, kind) == ["ok", "ok"]
    assert await _open_alarms(unguarded.database, scope, kind) == 2
