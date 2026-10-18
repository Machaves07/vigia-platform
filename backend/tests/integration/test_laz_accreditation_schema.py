"""Esquema ``accreditation`` de ``laz_0031`` (TASK-338, LC-LAZ-02; NFR-LAZ-07 y 08;
PAT-LAZ-RES-01, PAT-LAZ-ESC-02, PAT-LAZ-RES-05; U04-H-02, 03 y 08; H-33 y H-39).

Base migrada propia del módulo (``alembic upgrade head`` como proceso aparte) con los datos de
``tests/identity_db.py`` y una fila de cada tabla de ``accreditation`` en cada planta
(``tests/accreditation_db.py``). Criterios de TASK-338:

- ``alembic upgrade head`` aplica ``laz_0031``: esquema y nueve tablas con RLS forzada, la
  política de organización y la RESTRICTIVE de proveedor en todas, permisos sin ``DELETE`` ni
  ``TRUNCATE`` y ``UPDATE`` solo en las columnas que el diseño deja cambiar; las definiciones de
  SQLAlchemy Core coinciden con la base.
- ``adoption_counter`` tiene exactamente las columnas del diseño: ninguna de usuario (H-33).
- La unicidad ``(organization_id, zone_id, period)`` rechaza un segundo sello del mismo periodo,
  también bajo concurrencia, y cae sin su restricción.
- El índice del límite exacto ``(organization_id, user_id, requested_at)``.
- Lo fijado en el primer intento (U04-H-08) no cambia al retomar; ``audit_sample_item`` es de
  solo anexar; las restricciones del diseño (listas cerradas, longitudes, límites, claves
  foráneas a la misma planta y zona).

Cada mutación corre en una transacción que se revierte o se restaura en ``finally``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.accreditation_db import (
    ACCREDITATION_TABLES,
    PERIOD_KEY_TABLES,
    ROW_BUILDERS,
    AccreditationScope,
    AccreditationSeed,
    adoption_counter,
    audit_sample,
    audit_sample_item,
    commitment,
    export_package,
    export_request,
    exposure_indicator,
    insert,
    monthly_package,
    next_month,
    program_metric_snapshot,
    seed_accreditation,
)
from tests.identity_db import BASE_TIME, MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_REVISION, HEAD_SCHEMA_VERSION
from vigia_platform.accreditation.adapters.postgres import tables as accreditation_tables

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
INSUFFICIENT_PRIVILEGE: Final = "42501"
CHECK_VIOLATION: Final = "23514"
UNIQUE_VIOLATION: Final = "23505"
FOREIGN_KEY_VIOLATION: Final = "23503"
RESTRICT_VIOLATION: Final = "23001"
WAIT_FOR_LOCK_SECONDS: Final = 30.0

ADOPTION_COLUMNS: Final = frozenset(
    {
        # domain-entities §3.6, literales: zona y día, nunca usuario (H-33).
        "organization_id",
        "plant_id",
        "zone_id",
        "day",
        "activity_counts",
        "session_count",
        "last_activity_at",
    }
)


def _load(name: str) -> Any:
    path = BACKEND / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load("laz_0031_accreditation_schema.py")


@dataclass(frozen=True)
class Accreditation:
    database: MigratedDatabase
    seed: AccreditationSeed

    @property
    def a(self) -> AccreditationScope:
        return self.seed.plant(self.seed.identity.a.organization_id)

    @property
    def a_other_plant(self) -> AccreditationScope:
        return self.seed.plant(self.seed.identity.a.organization_id, 1)

    @property
    def b(self) -> AccreditationScope:
        return self.seed.plant(self.seed.identity.b.organization_id)


@pytest.fixture(scope="module")
def accreditation(postgres_endpoint: PostgresEndpoint) -> Iterator[Accreditation]:
    with seeded_identity(postgres_endpoint, "vigia_laz_0031") as (database, identity):

        async def prepare() -> Accreditation:
            connection = await database.connect()
            try:
                return Accreditation(database, await seed_accreditation(connection, identity))
            finally:
                await connection.close()

        yield asyncio.run(prepare())


@pytest_asyncio.fixture
async def superuser(accreditation: Accreditation) -> AsyncIterator[Any]:
    connection = await accreditation.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(accreditation: Accreditation) -> AsyncIterator[Any]:
    connection = await accreditation.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la prueba."""


Statement = tuple[str, list[Any]]


async def _steps(connection: Any, organization_id: uuid.UUID, *steps: Statement) -> str:
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


def _columns(sql: str) -> list[str]:
    return sql[sql.index("(") + 1 : sql.index(")")].split(", ")


def _with(statement: Statement, **changes: Any) -> Statement:
    """La misma fila con ``changes`` aplicados."""
    sql, args = statement
    table = sql.split()[2].split(".")[1]
    return insert(table, dict(zip(_columns(sql), args, strict=True)) | changes)


# --- Criterio 1: alembic upgrade head aplica laz_0031 -----


@pytest.mark.asyncio
async def test_upgrade_head_applies_laz_0031(superuser: Any) -> None:
    assert HEAD_SCHEMA_VERSION >= 31
    assert await superuser.fetchval("SELECT version_num FROM public.alembic_version") == (
        HEAD_REVISION
    )
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'accreditation'"
        )
        == "vigia_migrate"
    )
    rows = await superuser.fetch(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
        " pg_get_userbyid(c.relowner) AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'accreditation' AND c.relkind IN ('r', 'p')"
    )
    # Las nueve entidades de DE §3.1 a §3.6.
    assert {row["relname"] for row in rows} == set(ACCREDITATION_TABLES) == set(MIGRATION.TABLES)
    assert len(ACCREDITATION_TABLES) == 9
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows), rows
    assert {row["owner"] for row in rows} == {"vigia_migrate"}

    policies = await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'accreditation'"
    )
    by_table: dict[str, dict[str, Any]] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], {})[policy["policyname"]] = policy
    assert set(by_table) == set(ACCREDITATION_TABLES)
    for table, named in by_table.items():
        # Todas tienen planta: la RESTRICTIVE de proveedor y ninguna de A-69.
        assert set(named) == {"organization_isolation", "provider_concession_scope"}, table
        isolation = named["organization_isolation"]
        assert (isolation["permissive"], isolation["cmd"]) == ("PERMISSIVE", "ALL"), table
        for clause in (isolation["qual"], isolation["with_check"]):
            assert clause == "(organization_id = shared.vigia_current_organization())", table
        provider = named["provider_concession_scope"]
        assert (provider["permissive"], provider["cmd"]) == ("RESTRICTIVE", "ALL"), table
        for clause in (provider["qual"], provider["with_check"]):
            assert clause == "identity.rls_provider_scope_allows(organization_id, plant_id)"

    for table in ACCREDITATION_TABLES:
        privileges = {
            privilege: await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)",
                f"accreditation.{table}",
                privilege,
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
        }
        assert privileges == {
            "SELECT": True,
            "INSERT": True,
            "DELETE": False,
            "TRUNCATE": False,
        }, table


@pytest.mark.asyncio
async def test_app_update_privileges_are_exactly_the_whitelist(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT table_name, column_name FROM information_schema.column_privileges"
        " WHERE table_schema = 'accreditation' AND grantee = 'vigia_app'"
        " AND privilege_type = 'UPDATE'"
    )
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(row["table_name"], set()).add(row["column_name"])
    fixed = {"organization_id", "plant_id", "zone_id"}
    for table, columns in granted.items():
        assert not columns & fixed, table
    assert "export_package" not in granted
    assert granted["export_request"] >= {
        "lease_owner",
        "lease_until",
        "progress_percent",
        "progress_updated_at",
        "current_artifact",
        "multipart_upload_id",
        "attempt_count",
        "checkpoint_record_ids",
        "generated_at",
    }
    assert not granted["export_request"] & {"zone_ids", "period_from", "period_to", "user_id"}
    assert granted["audit_sample_item"] == {
        "outcome",
        "comment",
        "reviewed_by",
        "reviewed_at",
        "record_id",
    }
    assert granted["adoption_counter"] == {"activity_counts", "session_count", "last_activity_at"}
    assert granted == {
        table: set(MIGRATION.app_updatable_columns(table))
        for table in ACCREDITATION_TABLES
        if MIGRATION.app_updatable_columns(table)
    }


@pytest.mark.asyncio
async def test_core_tables_match_the_database(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, a.attname::text AS column_name, NOT a.attnotnull AS"
        " nullable FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'accreditation' AND c.relkind IN ('r', 'p')"
        " AND a.attnum > 0 AND NOT a.attisdropped"
    )
    database: dict[str, dict[str, bool]] = {}
    for row in rows:
        database.setdefault(row["table_name"], {})[row["column_name"]] = row["nullable"]
    core = {
        table.name: {column.name: bool(column.nullable) for column in table.columns}
        for table in accreditation_tables.METADATA.tables.values()
    }
    assert core == database
    primary_keys = {
        row["table_name"]: set(row["columns"])
        for row in await superuser.fetch(
            "SELECT c.relname AS table_name, array_agg(a.attname::text) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
            " WHERE n.nspname = 'accreditation' AND i.indisprimary GROUP BY c.relname"
        )
    }
    assert primary_keys == {
        table.name: {column.name for column in table.primary_key.columns}
        for table in accreditation_tables.METADATA.tables.values()
    }


@pytest.mark.asyncio
async def test_export_request_has_the_columns_of_the_dated_notes(superuser: Any) -> None:
    """§3.1 con las notas de 2026-09-21 (arrendamiento, progreso, subida por partes) y U04-H-03
    y 08 (``package_form``, ``checkpoint_record_ids`` y ``generated_at``)."""
    columns = {
        row["column_name"]
        for row in await superuser.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'accreditation' AND table_name = 'export_request'"
        )
    }
    assert columns >= {
        "state",
        "package_form",
        "lease_owner",
        "lease_until",
        "progress_percent",
        "progress_updated_at",
        "current_artifact",
        "multipart_upload_id",
        "attempt_count",
        "checkpoint_record_ids",
        "generated_at",
        "expires_at",
    }


# --- Criterio 4: adoption_counter sin ninguna columna de usuario (H-33) -----


async def _adoption_columns(connection: Any) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'accreditation' AND table_name = 'adoption_counter'"
    )
    return {str(row["column_name"]) for row in rows}


@pytest.mark.asyncio
async def test_adoption_counter_has_exactly_the_design_columns(superuser: Any) -> None:
    assert await _adoption_columns(superuser) == ADOPTION_COLUMNS
    # Ni una clave foránea hacia identity.user_account.
    references = await superuser.fetch(
        "SELECT confrelid::regclass::text AS target FROM pg_constraint"
        " WHERE conrelid = 'accreditation.adoption_counter'::regclass AND contype = 'f'"
    )
    assert {row["target"] for row in references} == {"identity.zone"}


@pytest.mark.asyncio
async def test_a_user_column_in_adoption_counter_would_be_detected(superuser: Any) -> None:
    """Sonda negativa: una columna de usuario añadida rompe la igualdad de conjuntos."""
    for column in ("user_id", "actor_ref", "Usuario"):
        try:
            async with superuser.transaction():
                await superuser.execute(
                    f'ALTER TABLE accreditation.adoption_counter ADD COLUMN "{column}" uuid'
                )
                assert await _adoption_columns(superuser) != ADOPTION_COLUMNS
                raise _Rollback
        except _Rollback:
            pass
    assert await _adoption_columns(superuser) == ADOPTION_COLUMNS


@pytest.mark.asyncio
async def test_activity_counts_only_admits_loop_activity_kinds(
    app: Any, accreditation: Accreditation
) -> None:
    """Ni un usuario por la puerta de atrás del mapa (P3): solo claves de ``loop_activity_kind``
    con enteros ≥ 0."""
    scope = accreditation.a
    org = scope.organization_id
    rejected = {
        "clave de usuario": {"user_id": 1},
        "usuario como clave": {str(uuid.uuid4()): 1},
        "clave con mayúscula": {"Inbox_opened": 1},
        "clave con espacio": {"inbox_opened ": 1},
        "clave con homoglifo": {"inbox_оpened": 1},  # o cirílica  # noqa: RUF001
        "valor negativo": {"inbox_opened": -1},
        "valor fraccionario": {"inbox_opened": 1.5},
        "valor de texto": {"inbox_opened": "1"},
        "valor anidado": {"inbox_opened": {"user": 1}},
    }
    outcomes = {
        label: await _steps(app, org, adoption_counter(scope, activity_counts=counts))
        for label, counts in rejected.items()
    }
    assert outcomes == dict.fromkeys(rejected, CHECK_VIOLATION)
    assert await _steps(app, org, _with(adoption_counter(scope), activity_counts="[]")) == (
        CHECK_VIOLATION
    )
    every_kind = {
        "inbox_opened": 0,
        "finding_classified": 3,
        "queue_resolved": 1,
        "analysis_advanced": 2,
        "action_managed": 4,
        "finding_closed": 1,
    }
    for counts in ({}, every_kind):
        assert await _steps(app, org, adoption_counter(scope, activity_counts=counts)) == "ok"


# --- Criterio 5: un sello por zona y periodo -----


@pytest.mark.asyncio
async def test_the_period_keys_are_unique_indexes(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, i.indisunique AS is_unique,"
        " array(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, n)"
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum ORDER BY k.n)"
        "  AS columns, ic.relname AS index_name"
        " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
        " JOIN pg_class ic ON ic.oid = i.indexrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'accreditation'"
    )
    unique = {
        row["table_name"]
        for row in rows
        if row["is_unique"] and row["columns"] == ["organization_id", "zone_id", "period"]
    }
    assert unique == set(PERIOD_KEY_TABLES)
    # PAT-LAZ-ESC-02: el índice del límite exacto, en ese orden.
    rate = [row for row in rows if row["index_name"] == "export_request_rate"]
    assert [(row["table_name"], row["columns"]) for row in rate] == [
        ("export_request", ["organization_id", "user_id", "requested_at"])
    ]


def _period_row(table: str, scope: AccreditationScope, period: dt.date) -> Statement:
    builders = {
        "monthly_package": monthly_package,
        "program_metric_snapshot": program_metric_snapshot,
        "exposure_indicator": exposure_indicator,
    }
    return builders[table](scope, period=period)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", PERIOD_KEY_TABLES)
async def test_a_second_seal_of_the_same_period_is_rejected(
    table: str, app: Any, accreditation: Accreditation
) -> None:
    scope = accreditation.a
    org = scope.organization_id
    period = next_month()
    first = _period_row(table, scope, period)
    assert await _steps(app, org, first, _period_row(table, scope, period)) == UNIQUE_VIOLATION
    # Otro mes de la misma zona, o el mismo mes en otra zona, sí entran.
    assert await _steps(app, org, first, _period_row(table, scope, next_month())) == "ok"
    other = accreditation.a_other_plant
    assert await _steps(app, org, first, _period_row(table, other, period)) == "ok"
    # El mismo mes y zona en otra organización no choca (la clave lleva la organización).
    b = accreditation.b
    assert await _steps(app, b.organization_id, _period_row(table, b, period)) == "ok"


async def _race(
    database: MigratedDatabase, organization_id: uuid.UUID, rows: list[Statement]
) -> list[str]:
    """La primera escribe y sigue abierta; la segunda espera su candado; se confirma la primera.

    Si la segunda no espera ningún candado (sin la restricción), termina sola y las dos pasan.
    Las filas confirmadas se quedan: cada carrera usa un periodo nuevo.
    """
    first = await database.connect("vigia_app")
    second = await database.connect("vigia_app")
    observer = await database.connect()
    try:
        transaction = first.transaction()
        await transaction.start()
        try:
            await set_scope(first, organization_id)
            await first.execute(rows[0][0], *rows[0][1])

            async def contender() -> str:
                try:
                    async with second.transaction():
                        await set_scope(second, organization_id)
                        await second.execute(rows[1][0], *rows[1][1])
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


@pytest.mark.asyncio
@pytest.mark.parametrize("table", PERIOD_KEY_TABLES)
async def test_the_period_key_holds_under_concurrency(
    table: str, accreditation: Accreditation
) -> None:
    scope = accreditation.a
    for _ in range(3):
        period = next_month()
        rows = [_period_row(table, scope, period), _period_row(table, scope, period)]
        assert await _race(accreditation.database, scope.organization_id, rows) == [
            "ok",
            UNIQUE_VIOLATION,
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", PERIOD_KEY_TABLES)
async def test_the_period_key_without_its_constraint(
    table: str, superuser: Any, accreditation: Accreditation
) -> None:
    """Sonda negativa: sin la restricción, la misma carrera deja dos sellos del mismo periodo.
    La restricción se restaura en ``finally`` (las filas duplicadas se borran antes)."""
    scope = accreditation.a
    constraint = f"{table}_period"
    await superuser.execute(f"ALTER TABLE accreditation.{table} DROP CONSTRAINT {constraint}")
    periods: list[dt.date] = []
    try:
        for _ in range(3):
            period = next_month()
            periods.append(period)
            rows = [_period_row(table, scope, period), _period_row(table, scope, period)]
            assert await _race(accreditation.database, scope.organization_id, rows) == ["ok", "ok"]
    finally:
        await superuser.execute(
            f"DELETE FROM accreditation.{table} WHERE period = ANY ($1::date[])",  # noqa: S608
            periods,
        )
        await superuser.execute(
            f"ALTER TABLE accreditation.{table} ADD CONSTRAINT {constraint}"
            " UNIQUE (organization_id, zone_id, period)"
        )


# --- U04-H-08: lo fijado en el primer intento no cambia al retomar -----


async def _new_export(app: Any, scope: AccreditationScope) -> uuid.UUID:
    export_id = uuid.uuid4()
    async with app.transaction():
        await set_scope(app, scope.organization_id)
        sql, args = export_request(scope, export_id=export_id)
        await app.execute(sql, *args)
    return export_id


def _update(export_id: uuid.UUID, assignments: str, *values: Any) -> Statement:
    return (
        f"UPDATE accreditation.export_request SET {assignments} WHERE export_id = $1",  # noqa: S608
        [export_id, *values],
    )


_FIRST_ATTEMPT: Final = "checkpoint_record_ids = $2, generated_at = $3"


@pytest.mark.asyncio
async def test_the_first_attempt_fixes_checkpoints_and_generated_at(
    app: Any, accreditation: Accreditation
) -> None:
    scope = accreditation.a
    org = scope.organization_id
    export_id = await _new_export(app, scope)
    checkpoints = [uuid.uuid4(), uuid.uuid4()]
    first = _update(export_id, _FIRST_ATTEMPT, checkpoints, BASE_TIME)
    lease = _update(
        export_id,
        "state = 'in_progress', lease_owner = 'worker-1', lease_until = $2,"
        " attempt_count = attempt_count + 1",
        BASE_TIME + dt.timedelta(seconds=60),
    )
    assert await _steps(app, org, lease, first) == "ok"
    later = BASE_TIME + dt.timedelta(minutes=5)
    retake = {
        "otra generated_at": _update(export_id, "generated_at = $2", later),
        "otros puntos de control": _update(export_id, "checkpoint_record_ids = $2", [uuid.uuid4()]),
        "puntos de control vacíos": _update(export_id, "checkpoint_record_ids = '{}'"),
        "las dos de nuevo": _update(export_id, _FIRST_ATTEMPT, [uuid.uuid4()], later),
        "las dos a nulo": _update(export_id, "checkpoint_record_ids = NULL, generated_at = NULL"),
    }
    outcomes = {
        label: await _steps(app, org, lease, first, statement)
        for label, statement in retake.items()
    }
    assert outcomes == dict.fromkeys(retake, RESTRICT_VIOLATION)
    # La retoma sí cambia el arrendamiento, el progreso, la subida por partes y el contador, y
    # reescribir el mismo valor no es un cambio.
    retake_ok = _update(
        export_id,
        "lease_owner = 'worker-2', lease_until = $2, attempt_count = attempt_count + 1,"
        " progress_percent = 40, progress_updated_at = $2, current_artifact = 'findings_json',"
        " multipart_upload_id = 'upload-2', checkpoint_record_ids = $3, generated_at = $4",
        later,
        checkpoints,
        BASE_TIME,
    )
    assert await _steps(app, org, lease, first, retake_ok) == "ok"
    # Y el superusuario (sin privilegios que lo frenen) tampoco: lo impide el disparador.
    superuser = await accreditation.database.connect()
    try:
        assert (
            await _steps(superuser, org, lease, first, retake["otra generated_at"])
            == RESTRICT_VIOLATION
        )
    finally:
        await superuser.close()


@pytest.mark.asyncio
async def test_without_the_trigger_a_retake_would_move_generated_at(
    superuser: Any, accreditation: Accreditation
) -> None:
    """Sonda negativa: sin el disparador, la misma retoma cambia ``generated_at``."""
    scope = accreditation.a
    export_id = uuid.uuid4()
    sql, args = export_request(scope, export_id=export_id)
    outcome = ""
    try:
        async with superuser.transaction():
            await superuser.execute(sql, *args)
            await superuser.execute(
                "DROP TRIGGER export_request_fixed_on_first_attempt ON accreditation.export_request"
            )
            for statement in (
                _update(export_id, _FIRST_ATTEMPT, [uuid.uuid4()], BASE_TIME),
                _update(export_id, "generated_at = $2", BASE_TIME + dt.timedelta(minutes=5)),
            ):
                await superuser.execute(statement[0], *statement[1])
            outcome = "ok"
            raise _Rollback
    except _Rollback:
        pass
    assert outcome == "ok"


# --- audit_sample_item ⛓ solo anexar (H-39) -----


@pytest.mark.asyncio
async def test_the_audit_result_is_written_once(app: Any, accreditation: Accreditation) -> None:
    scope = accreditation.a
    org = scope.organization_id
    item_id = uuid.uuid4()
    item = audit_sample_item(scope, sample_item_id=item_id)
    reviewer = json.dumps({"user_id": str(scope.user_id), "role": "coordinator_sst"})
    review = (
        "UPDATE accreditation.audit_sample_item SET outcome = 'consistent_criterion',"
        " reviewed_by = $2, reviewed_at = $3, record_id = $4 WHERE sample_item_id = $1",
        [item_id, reviewer, BASE_TIME, uuid.uuid4()],
    )
    assert await _steps(app, org, item, review) == "ok"
    for statement in (
        "UPDATE accreditation.audit_sample_item SET outcome = 'criterion_error'",
        "UPDATE accreditation.audit_sample_item SET reviewed_at = reviewed_at + interval '1 day'",
        "UPDATE accreditation.audit_sample_item SET outcome = NULL, reviewed_by = NULL,"
        " reviewed_at = NULL",
    ):
        assert (
            await _steps(
                app,
                org,
                item,
                review,
                (statement + " WHERE sample_item_id = $1", [item_id]),
            )
            == RESTRICT_VIOLATION
        ), statement
    superuser = await accreditation.database.connect()
    try:
        # Ni el superusuario cambia una columna fija ni borra (ENABLE ALWAYS).
        for statement in (
            "UPDATE accreditation.audit_sample_item SET classification_id = $2"
            " WHERE sample_item_id = $1",
            "DELETE FROM accreditation.audit_sample_item WHERE sample_item_id = $1 AND $2::uuid"
            " IS NOT NULL",
        ):
            assert (
                await _steps(superuser, org, item, (statement, [item_id, uuid.uuid4()]))
                == RESTRICT_VIOLATION
            ), statement
    finally:
        await superuser.close()


@pytest.mark.asyncio
async def test_the_app_cannot_change_fixed_columns(app: Any, accreditation: Accreditation) -> None:
    org = accreditation.a.organization_id
    for statement in (
        "UPDATE accreditation.export_request SET zone_ids = zone_ids",
        "UPDATE accreditation.export_request SET plant_id = plant_id",
        "UPDATE accreditation.export_request SET period_to = period_to",
        "UPDATE accreditation.export_request SET user_id = user_id",
        "UPDATE accreditation.export_package SET archive_sha256 = archive_sha256",
        "UPDATE accreditation.monthly_package SET period = period",
        "UPDATE accreditation.monthly_package SET zone_id = zone_id",
        "UPDATE accreditation.audit_sample SET seed = seed",
        "UPDATE accreditation.audit_sample_item SET classification_id = classification_id",
        "UPDATE accreditation.commitment SET text = text",
        "UPDATE accreditation.program_metric_snapshot SET period = period",
        "UPDATE accreditation.exposure_indicator SET zone_id = zone_id",
        "UPDATE accreditation.adoption_counter SET day = day",
        *(f"DELETE FROM accreditation.{table}" for table in ACCREDITATION_TABLES),  # noqa: S608
    ):
        assert await _steps(app, org, (statement, [])) == INSUFFICIENT_PRIVILEGE, statement
    for statement in (
        "UPDATE accreditation.commitment SET state = 'done', closed_at = recorded_at",
        "UPDATE accreditation.program_metric_snapshot SET recomputed_at = materialized_at",
        "UPDATE accreditation.adoption_counter SET session_count = session_count + 1",
    ):
        assert await _steps(app, org, (statement, [])) == "ok", statement


# --- Restricciones del diseño -----


@pytest.mark.asyncio
async def test_design_constraints(app: Any, accreditation: Accreditation) -> None:
    scope = accreditation.a
    org = scope.organization_id
    request = export_request(scope)
    zones = [uuid.uuid4() for _ in range(21)]
    rejected = {
        # U04-H-02: composing no existe.
        "estado composing": _with(request, state="composing"),
        "forma de paquete desconocida": _with(request, package_form="with_audio"),
        "sin zonas": _with(request, zone_ids=[]),
        "21 zonas": _with(request, zone_ids=zones),
        "zona nula": _with(request, zone_ids=[scope.zone_id, None]),
        "periodo de 367 días": _with(
            request, period_from=BASE_TIME - dt.timedelta(days=367), period_to=BASE_TIME
        ),
        "periodo vacío": _with(request, period_from=BASE_TIME, period_to=BASE_TIME),
        "progreso 101": _with(request, progress_percent=101),
        "progreso -1": _with(request, progress_percent=-1),
        "artefacto de 129": _with(request, current_artifact="a" * 129),
        "artefacto vacío": _with(request, current_artifact=""),
        "subida de 257": _with(request, multipart_upload_id="u" * 257),
        "arrendamiento sin plazo": _with(request, lease_owner="worker-1"),
        "plazo sin arrendamiento": _with(request, lease_until=BASE_TIME),
        "generated_at sin puntos de control": _with(request, generated_at=BASE_TIME),
        "puntos de control sin generated_at": _with(request, checkpoint_record_ids=[uuid.uuid4()]),
        "intentos negativos": _with(request, attempt_count=-1),
        "código de fallo con texto libre": _with(request, failure_code="Falló la subida"),
        "solicitante que no es objeto": _with(request, requested_by="[]"),
        "paquete del día 2": _with(monthly_package(scope, period=dt.date(2026, 9, 2))),
        "paquete en estado desconocido": _with(monthly_package(scope), state="composing"),
        "muestra de 31": _with(audit_sample(scope), population_size=100, sample_size=31),
        "muestra mayor que la población": _with(
            audit_sample(scope), population_size=3, sample_size=4
        ),
        "resultado sin revisor": _with(
            audit_sample_item(scope), outcome="criterion_error", reviewed_at=BASE_TIME
        ),
        "resultado desconocido": _with(
            audit_sample_item(scope),
            outcome="wrong",
            reviewed_at=BASE_TIME,
            reviewed_by=json.dumps({"role": "coordinator_sst"}),
        ),
        "comentario de 1001": _with(audit_sample_item(scope), comment="c" * 1001),
        "compromiso de 19": _with(commitment(scope), text="c" * 19),
        "compromiso de 1001": _with(commitment(scope), text="c" * 1001),
        "compromiso en estado desconocido": _with(commitment(scope), state="closed"),
        "nota de cierre de 501": _with(commitment(scope), closed_note="n" * 501),
        "métrica semanal": _with(program_metric_snapshot(scope), granularity="week"),
        "huella que no es hex 64": _with(program_metric_snapshot(scope), inputs_digest="0" * 63),
        "huella en mayúsculas": _with(program_metric_snapshot(scope), inputs_digest="A" * 64),
        "exposición desconocida": _with(exposure_indicator(scope), status="safe"),
        "cobertura de 101": _with(exposure_indicator(scope), observable_coverage_percent=101.0),
        "minutos negativos": _with(exposure_indicator(scope), confirmed_minutes=-1.0),
        "sesiones negativas": _with(adoption_counter(scope), session_count=-1),
    }
    outcomes = {label: await _steps(app, org, statement) for label, statement in rejected.items()}
    assert outcomes == dict.fromkeys(rejected, CHECK_VIOLATION)

    accepted = {
        "20 zonas": _with(request, zone_ids=zones[:20]),
        "una zona": _with(request, zone_ids=[scope.zone_id]),
        "periodo de 366 días": _with(
            request, period_from=BASE_TIME - dt.timedelta(days=366), period_to=BASE_TIME
        ),
        "artefacto de 128": _with(request, current_artifact="a" * 128),
        "subida de 256": _with(request, multipart_upload_id="u" * 256),
        "progreso 100": _with(request, progress_percent=100),
        "con clips": _with(request, package_form="with_clips"),
        "fijado al nacer": _with(
            request, checkpoint_record_ids=[uuid.uuid4()], generated_at=BASE_TIME
        ),
        "compromiso de 20": _with(commitment(scope), text="c" * 20),
        "compromiso de 1000": _with(commitment(scope), text="c" * 1000),
        "muestra de 30": _with(audit_sample(scope), population_size=300, sample_size=30),
        "comentario vacío": _with(audit_sample_item(scope), comment=""),
    }
    outcomes = {label: await _steps(app, org, statement) for label, statement in accepted.items()}
    assert outcomes == dict.fromkeys(accepted, "ok")
    for state in ("requested", "in_progress", "ready", "failed", "expired"):
        assert await _steps(app, org, _with(request, state=state)) == "ok", state


@pytest.mark.asyncio
async def test_rows_hang_from_their_own_plant_zone_and_organization(
    app: Any, accreditation: Accreditation
) -> None:
    scope = accreditation.a
    org = scope.organization_id
    other = accreditation.a_other_plant
    foreign = {
        # Un usuario de otra organización no existe para la clave foránea.
        "solicitante de B": _with(export_request(scope), user_id=accreditation.b.user_id),
        "revisor de B": _with(audit_sample(scope), reviewer_user_id=accreditation.b.user_id),
        "responsable de B": _with(commitment(scope), responsible_user_id=accreditation.b.user_id),
        # Una zona que no es de la planta.
        "zona de otra planta": _with(monthly_package(scope), zone_id=other.zone_id),
        "zona inexistente": _with(adoption_counter(scope), zone_id=uuid.uuid4()),
        # Un hijo con otra planta o zona que su padre.
        "muestra de otra planta": _with(
            audit_sample(scope), plant_id=other.plant_id, zone_id=other.zone_id
        ),
        "elemento de otra planta": _with(
            audit_sample_item(scope), plant_id=other.plant_id, zone_id=other.zone_id
        ),
        "compromiso de otra planta": _with(
            commitment(scope), plant_id=other.plant_id, zone_id=other.zone_id
        ),
    }
    outcomes = {label: await _steps(app, org, statement) for label, statement in foreign.items()}
    assert outcomes == dict.fromkeys(foreign, FOREIGN_KEY_VIOLATION)
    # El paquete de exportación lleva la planta de su solicitud.
    export_id = await _new_export(app, scope)
    package = (
        "INSERT INTO accreditation.export_package (export_id, organization_id, plant_id,"
        " document_sha256, archive_sha256, manifest, size_bytes, artifact_count,"
        " checkpoint_record_ids, verifier_version) VALUES ($1, $2, $3, $4, $4, '{}', 1, 1,"
        " '{}', 'verifier-1.0.0')"
    )
    digest = "a" * 64
    assert await _steps(app, org, (package, [export_id, org, other.plant_id, digest])) == (
        FOREIGN_KEY_VIOLATION
    )
    assert await _steps(app, org, (package, [export_id, org, scope.plant_id, digest])) == "ok"
    assert await _steps(app, org, export_package(scope)) == "ok"


def test_every_table_has_a_row_builder() -> None:
    assert tuple(ROW_BUILDERS) == ACCREDITATION_TABLES == MIGRATION.TABLES
