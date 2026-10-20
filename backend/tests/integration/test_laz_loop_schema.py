"""Esquema ``loop`` de ``laz_0029`` (TASK-301, LC-LAZ-02; NFR-LAZ-07; PAT-LAZ-ESC-02 y 03).

Base migrada propia del módulo (``alembic upgrade head`` como proceso aparte) con los datos de
``tests/identity_db.py`` y una fila de cada tabla de ``loop`` en cada planta
(``tests/loop_db.py``). Criterios de TASK-301:

- ``alembic upgrade head`` aplica ``laz_0029``: esquema, 19 tablas con RLS forzada y sus
  políticas (organización en todas; proveedor por planta en las de planta; ninguna fila bajo
  concesión en las cinco de solo organización, A-69), permisos sin ``DELETE`` ni ``TRUNCATE`` y
  ``UPDATE`` solo en las columnas que el diseño deja cambiar; las definiciones de SQLAlchemy Core
  coinciden con la base y ``migrations/append_only.py`` lista las ⛓.
- Solo anexar (``closure_attachment`` y ``assistant_request``) con ``vigia_app`` y con el dueño.
- Las garantías de «una sola vez» resisten la concurrencia (y caen sin su guarda).
- PAT-LAZ-ESC-03: ``EXPLAIN (FORMAT JSON)`` de la bandeja y del panel de vencidos usa el índice
  parcial (``finding_state_inbox`` y ``finding_state_overdue``) en la partición del mes.

Cada mutación corre en una transacción que se revierte siempre.
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

from tests.identity_db import (
    MigratedDatabase,
    _insert_organization,
    insert_plant,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_REVISION, HEAD_SCHEMA_VERSION
from tests.loop_db import (
    LOOP_TABLES,
    ORGANIZATION_ONLY_TABLES,
    PLANT_TABLES,
    LoopScope,
    LoopSeed,
    assistant_request,
    blocked_vocabulary_list,
    closure_attachment,
    finding_state,
    insert,
    loop_projection_checkpoint,
    plant_loop_configuration,
    review_queue_item,
    seed_loop,
)
from vigia_platform.loop.adapters.postgres import tables as loop_tables

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
INSUFFICIENT_PRIVILEGE: Final = "42501"
RESTRICT_VIOLATION: Final = "23001"
CHECK_VIOLATION: Final = "23514"
UNIQUE_VIOLATION: Final = "23505"
WAIT_FOR_LOCK_SECONDS: Final = 30.0


def _load(name: str) -> Any:
    path = BACKEND / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load("laz_0029_loop_schema.py")
USAGE = _load("laz_0033_assistant_usage.py")
"""laz_0033 (TASK-316): las columnas de uso de ``assistant_request``, de trabajo del guardián."""
LAZ_0037 = _load("laz_0037_projection_rebuild_shadow.py")

LAZ_0037_SHADOW_TABLES: Final = LAZ_0037._SHADOW_TABLES
"""Las dos tablas sombra de la reconstrucción (TASK-306): a diferencia de las 19 de laz_0029, sí
tienen ``DELETE`` concedido (el intercambio las vacía)."""
LAZ_0037_TABLES: Final = (*LAZ_0037._SHADOW_TABLES, "loop_projection_sample_run")
"""Las tres tablas que ``laz_0037`` añade al esquema ``loop``, aditivas sobre laz_0029."""


@dataclass(frozen=True)
class Loop:
    database: MigratedDatabase
    seed: LoopSeed
    month: dt.date
    """Mes en curso de la base (UTC): el de las particiones que dejó la migración."""

    @property
    def a(self) -> LoopScope:
        return self.seed.plant(self.seed.identity.a.organization_id)

    @property
    def now(self) -> dt.datetime:
        """Un instante del mes en curso de la base, para caer en una partición mensual."""
        return dt.datetime(self.month.year, self.month.month, 2, 12, 0, tzinfo=dt.UTC)


def _prepare(database: MigratedDatabase, identity: Any) -> Loop:
    async def run() -> Loop:
        connection = await database.connect()
        try:
            seed = await seed_loop(connection, identity)
            month = await connection.fetchval(
                "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"
            )
        finally:
            await connection.close()
        return Loop(database, seed, month)

    return asyncio.run(run())


@pytest.fixture(scope="module")
def loop(postgres_endpoint: PostgresEndpoint) -> Iterator[Loop]:
    with seeded_identity(postgres_endpoint, "vigia_laz_0029") as (database, identity):
        yield _prepare(database, identity)


@pytest_asyncio.fixture
async def superuser(loop: Loop) -> AsyncIterator[Any]:
    connection = await loop.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(loop: Loop) -> AsyncIterator[Any]:
    connection = await loop.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def owner(loop: Loop) -> AsyncIterator[Any]:
    connection = await loop.database.connect("vigia_migrate")
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


# --- Criterio 1: alembic upgrade head aplica laz_0029 -----


@pytest.mark.asyncio
async def test_upgrade_head_applies_laz_0029(superuser: Any) -> None:
    assert HEAD_SCHEMA_VERSION >= 29
    assert await superuser.fetchval("SELECT version_num FROM public.alembic_version") == (
        HEAD_REVISION
    )
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'loop'"
        )
        == "vigia_migrate"
    )
    rows = await superuser.fetch(
        "SELECT c.relname, c.relkind::text AS kind, c.relrowsecurity, c.relforcerowsecurity,"
        " pg_get_userbyid(c.relowner) AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'loop' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
    )
    # Las tres de laz_0037 (TASK-306) son aditivas, como monthly_package_lease sobre laz_0031
    # (VIG-239): no las creó laz_0029, pero ya están en el esquema y en LOOP_TABLES.
    assert (
        {row["relname"] for row in rows}
        == set(LOOP_TABLES)
        == set(MIGRATION.TABLES) | set(LAZ_0037_TABLES)
    )
    assert len(LOOP_TABLES) == 19 + len(LAZ_0037_TABLES)
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows), rows
    assert {row["owner"] for row in rows} == {"vigia_migrate"}
    partitioned = {row["relname"] for row in rows if row["kind"] == "p"}
    assert partitioned == {"finding_state", "classification", "review_resolution"}

    policies = await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'loop'"
    )
    by_table: dict[str, dict[str, Any]] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], {})[policy["policyname"]] = policy
    assert set(by_table) == set(LOOP_TABLES)
    for table, named in by_table.items():
        expected = {"organization_isolation"}
        if table in PLANT_TABLES:
            expected.add("provider_concession_scope")
        if table in ORGANIZATION_ONLY_TABLES:
            expected.add("provider_context_denied")
        assert set(named) == expected, table
        isolation = named["organization_isolation"]
        assert (isolation["permissive"], isolation["cmd"]) == ("PERMISSIVE", "ALL"), table
        for clause in (isolation["qual"], isolation["with_check"]):
            assert clause == "(organization_id = shared.vigia_current_organization())", table
        if "provider_concession_scope" in named:
            provider = named["provider_concession_scope"]
            assert (provider["permissive"], provider["cmd"]) == ("RESTRICTIVE", "ALL"), table
            for clause in (provider["qual"], provider["with_check"]):
                assert clause == "identity.rls_provider_scope_allows(organization_id, plant_id)"
        if "provider_context_denied" in named:
            denied = named["provider_context_denied"]
            assert (denied["permissive"], denied["cmd"]) == ("RESTRICTIVE", "ALL"), table
            for clause in (denied["qual"], denied["with_check"]):
                assert clause == "(NOT identity.rls_provider_context())", table

    # Ningún DELETE ni TRUNCATE para vigia_app; SELECT e INSERT en todas. Excepción: las dos
    # tablas sombra de laz_0037 sí tienen DELETE (el intercambio las vacía por planta).
    for table in LOOP_TABLES:
        privileges = {
            privilege: await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)", f"loop.{table}", privilege
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
        }
        expected_delete = table in LAZ_0037_SHADOW_TABLES
        assert privileges == {
            "SELECT": True,
            "INSERT": True,
            "DELETE": expected_delete,
            "TRUNCATE": False,
        }, table
    # Las particiones no se leen directamente: no tienen RLS propia y vigia_app no tiene nada.
    children = await superuser.fetch(
        "SELECT c.oid::regclass::text AS name FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'loop' AND c.relispartition AND c.relkind = 'r'"
    )
    assert len(children) == 3 * (1 + 1 + MIGRATION.PARTITION_MONTHS_AHEAD)
    for child in children:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)", child["name"], privilege
            ), (child["name"], privilege)


@pytest.mark.asyncio
async def test_app_update_privileges_are_exactly_the_whitelist(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT table_name, column_name FROM information_schema.column_privileges"
        " WHERE table_schema = 'loop' AND grantee = 'vigia_app' AND privilege_type = 'UPDATE'"
    )
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(row["table_name"], set()).add(row["column_name"])
    expected = {
        table: set(MIGRATION.app_updatable_columns(table))
        for table in LOOP_TABLES
        if MIGRATION.app_updatable_columns(table)
    }
    expected["assistant_request"] |= set(USAGE.USAGE_COLUMNS)
    # El arrendamiento de la reconstrucción (laz_0037, TASK-306): dos columnas más, aditivas
    # sobre lo que laz_0029 ya dejaba actualizable en loop_projection_checkpoint.
    expected["loop_projection_checkpoint"] |= {"rebuild_owner", "rebuild_lease_until"}
    # Las dos tablas sombra de laz_0037 (TASK-306): UPDATE acotado a las columnas de negocio, no
    # a la clave, la organización, la planta ni la zona (igual invariante que las de laz_0029).
    for table, core in (
        ("episode_rebuild_shadow", loop_tables.EPISODE_REBUILD_SHADOW),
        ("finding_state_rebuild_shadow", loop_tables.FINDING_STATE_REBUILD_SHADOW),
    ):
        expected[table] = {
            column.name
            for column in core.columns
            if column.name not in {"organization_id", "plant_id", "zone_id"}
            and not column.primary_key
        }
    assert granted == expected
    # Las decisiones no cambian: ninguna columna actualizable.
    for table in (
        "classification",
        "review_resolution",
        "action_evidence_entry",
        "closure_record",
        "unclosable_review",
        "reopening",
    ):
        assert table not in granted
    # Ninguna clave, organización, planta ni zona es actualizable.
    for table, columns in granted.items():
        assert not columns & {"organization_id", "plant_id", "zone_id"}, table
    assert not granted["finding_state"] & set(MIGRATION._FINDING_STATE_FIXED)
    assert granted["assistant_request"] == {
        "state",
        "resolved_at",
        "unavailable_cause",
        "attempt_count",
        *USAGE.USAGE_COLUMNS,
    }
    assert granted["closure_attachment"] == {"verified_at"}


def test_append_only_registry_lists_every_append_only_table() -> None:
    spec = importlib.util.spec_from_file_location(
        "append_only_under_test", BACKEND / "migrations" / "append_only.py"
    )
    assert spec is not None and spec.loader is not None
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    listed = {name for name in registry.APPEND_ONLY_TABLES if name.startswith("loop.")}
    # loop_projection_sample_run (laz_0037, TASK-306) es aditiva sobre laz_0029, como
    # monthly_package_lease sobre laz_0031 (VIG-239).
    assert listed == {f"loop.{table}" for table in MIGRATION.APPEND_ONLY_TABLES} | {
        "loop.loop_projection_sample_run"
    }
    assert set(MIGRATION.APPEND_ONLY_TABLES) == {"closure_attachment", "assistant_request"}


@pytest.mark.asyncio
async def test_append_only_triggers_are_always_enabled(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname, c.relispartition, t.tgname, t.tgenabled::text AS tgenabled"
        " FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'loop' AND NOT t.tgisinternal"
    )
    triggers: dict[str, dict[str, str]] = {}
    partitions: dict[str, dict[str, str]] = {}
    for row in rows:
        target = partitions if row["relispartition"] else triggers
        target.setdefault(row["relname"], {})[row["tgname"]] = row["tgenabled"]
    # loop_projection_sample_run (laz_0037, TASK-306) es aditiva: de solo anexar, con los mismos
    # tres disparadores que las de laz_0029.
    append_only = (*MIGRATION.APPEND_ONLY_TABLES, "loop_projection_sample_run")
    assert set(triggers) == set(append_only)
    for table in append_only:
        assert triggers[table] == {
            "append_only_update": "A",
            "append_only_delete": "A",
            "append_only_no_truncate": "A",
        }, table
    # Cada partición de las tres tablas particionadas: vaciarla falla (nuc_0002), como en U-02.
    children = await superuser.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'loop' AND c.relispartition AND c.relkind = 'r'"
    )
    assert {row["relname"] for row in children} == set(partitions)
    assert all(found == {"append_only_truncate": "A"} for found in partitions.values()), partitions


@pytest.mark.asyncio
async def test_core_tables_match_the_database(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, a.attname::text AS column_name, NOT a.attnotnull AS"
        " nullable FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'loop' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        " AND a.attnum > 0 AND NOT a.attisdropped"
    )
    database: dict[str, dict[str, bool]] = {}
    for row in rows:
        database.setdefault(row["table_name"], {})[row["column_name"]] = row["nullable"]
    core = {
        table.name: {column.name: bool(column.nullable) for column in table.columns}
        for table in loop_tables.METADATA.tables.values()
    }
    assert core == database
    primary_keys = {
        row["table_name"]: set(row["columns"])
        for row in await superuser.fetch(
            "SELECT c.relname AS table_name, array_agg(a.attname::text) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
            " WHERE n.nspname = 'loop' AND i.indisprimary AND NOT c.relispartition"
            " GROUP BY c.relname"
        )
    }
    assert primary_keys == {
        table.name: {column.name for column in table.primary_key.columns}
        for table in loop_tables.METADATA.tables.values()
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
        " WHERE n.nspname = 'loop' AND NOT c.relispartition"
    )
    by_name = {row["index_name"]: row for row in rows}
    assert by_name["finding_state_inbox"]["columns"] == [
        "organization_id",
        "plant_id",
        "zone_id",
        "lifecycle_state",
        "occurred_at_bucket",
        "finding_state_id",
    ]
    assert by_name["finding_state_overdue"]["columns"] == [
        "organization_id",
        "plant_id",
        "due_at",
        "finding_state_id",
    ]
    assert by_name["assistant_request_rate"]["columns"] == [
        "organization_id",
        "user_id",
        "requested_at",
    ]
    # Con planta, el índice empieza por (organization_id, plant_id) (PAT-NUC-ESC-07); sin ella,
    # por organization_id. La única excepción declarada es el límite exacto del asistente.
    loose = [
        row["index_name"]
        for row in rows
        if not row["constraint_index"]
        and row["index_name"] != "assistant_request_rate"
        and row["columns"][: 2 if row["table_name"] in PLANT_TABLES else 1]
        != ["organization_id", "plant_id"][: 2 if row["table_name"] in PLANT_TABLES else 1]
    ]
    assert loose == []
    definitions = {
        row["name"]: row["definition"]
        for row in await superuser.fetch(
            "SELECT indexname AS name, indexdef AS definition FROM pg_indexes"
            " WHERE schemaname = 'loop'"
            " AND indexname IN ('finding_state_inbox', 'finding_state_overdue')"
        )
    }
    assert "occurred_at_bucket DESC" in definitions["finding_state_inbox"]
    for state in MIGRATION.ACTIVE_STATES:
        assert f"'{state}'" in definitions["finding_state_inbox"]
    for state in ("discarded", "closed"):
        assert f"'{state}'" not in definitions["finding_state_inbox"]
    assert definitions["finding_state_overdue"].endswith(
        "WHERE (lifecycle_state = ANY (ARRAY['action_assigned'::text, 'overdue'::text]))"
    )


# --- Solo anexar -----


async def _attachment(connection: Any, scope: LoopScope) -> uuid.UUID:
    attachment_id = uuid.uuid4()
    sql, args = closure_attachment(scope, attachment_id=attachment_id)
    await connection.execute(sql, *args)
    return attachment_id


async def _request(connection: Any, scope: LoopScope) -> uuid.UUID:
    request_id = uuid.uuid4()
    sql, args = assistant_request(scope, request_id=request_id)
    await connection.execute(sql, *args)
    return request_id


@pytest.mark.asyncio
async def test_delete_and_truncate_fail_for_the_app_and_the_owner(
    app: Any, owner: Any, superuser: Any, loop: Loop
) -> None:
    scope = loop.a
    for table in MIGRATION.APPEND_ONLY_TABLES:
        assert (
            await _steps(app, scope.organization_id, (f"DELETE FROM loop.{table}", []))  # noqa: S608
            == INSUFFICIENT_PRIVILEGE
        )
        assert (
            await _steps(owner, scope.organization_id, (f"DELETE FROM loop.{table}", []))  # noqa: S608
            == RESTRICT_VIOLATION
        ), table
        # CASCADE: assistant_draft referencia a assistant_request; sin él, PostgreSQL rechaza
        # el vaciado antes de llegar al disparador.
        assert (
            await _steps(owner, scope.organization_id, (f"TRUNCATE loop.{table} CASCADE", []))
            == RESTRICT_VIOLATION
        ), table
    # Las proyecciones tampoco se borran desde la aplicación (P4); excepción documentada: las dos
    # tablas sombra de laz_0037 (TASK-306) no son la proyección en servicio, son su área de
    # preparación interna, y el intercambio las vacía por planta a propósito.
    for table in LOOP_TABLES:
        if table in LAZ_0037_SHADOW_TABLES:
            continue
        assert (
            await _steps(app, scope.organization_id, (f"DELETE FROM loop.{table}", []))  # noqa: S608
            == INSUFFICIENT_PRIVILEGE
        ), table


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("requested_at", "requested_at + interval '1 second'"),
        ("context_digest", "repeat('1', 64)"),
        ("outbox_event_id", "gen_random_uuid()"),
    ],
)
async def test_assistant_request_fields_do_not_change(
    column: str, value: str, owner: Any, superuser: Any, loop: Loop
) -> None:
    """Los campos de la solicitud son de solo anexar: ni el dueño los cambia (disparador)."""
    request_id = await _request(superuser, loop.a)
    update = f"UPDATE loop.assistant_request SET {column} = {value} WHERE request_id = $1"  # noqa: S608
    assert await _steps(owner, loop.a.organization_id, (update, [request_id])) == (
        RESTRICT_VIOLATION
    )


@pytest.mark.asyncio
async def test_assistant_request_leaves_pending_once(app: Any, superuser: Any, loop: Loop) -> None:
    request_id = await _request(superuser, loop.a)
    resolve = (
        "UPDATE loop.assistant_request SET state = $2, unavailable_cause = $3,"
        " resolved_at = requested_at + interval '1 minute', attempt_count = attempt_count + 1"
        " WHERE request_id = $1"
    )
    organization = loop.a.organization_id
    assert await _steps(app, organization, (resolve, [request_id, "ready", None])) == "ok"
    assert (
        await _steps(
            app,
            organization,
            (resolve, [request_id, "ready", None]),
            (resolve, [request_id, "unavailable", "timeout"]),
        )
        == RESTRICT_VIOLATION
    )
    # El contador de intentos cambia sin cerrar nada.
    count = (
        "UPDATE loop.assistant_request SET attempt_count = attempt_count + 1 WHERE request_id = $1"
    )
    assert await _steps(app, organization, (count, [request_id]), (count, [request_id])) == "ok"
    # Una columna fuera de la lista no tiene ni permiso.
    other = (
        "UPDATE loop.assistant_request SET analysis_id = gen_random_uuid() WHERE request_id = $1"
    )
    assert await _steps(app, organization, (other, [request_id])) == INSUFFICIENT_PRIVILEGE


@pytest.mark.asyncio
async def test_closure_attachment_verified_at_closes_once(
    app: Any, superuser: Any, loop: Loop
) -> None:
    attachment_id = await _attachment(superuser, loop.a)
    verify = (
        "UPDATE loop.closure_attachment SET verified_at = uploaded_at + $2::interval"
        " WHERE attachment_id = $1"
    )
    organization = loop.a.organization_id
    one, two = dt.timedelta(minutes=1), dt.timedelta(minutes=2)
    assert await _steps(app, organization, (verify, [attachment_id, one])) == "ok"
    assert (
        await _steps(
            app, organization, (verify, [attachment_id, one]), (verify, [attachment_id, two])
        )
        == RESTRICT_VIOLATION
    )


# --- Restricciones que fija el diseño -----


@pytest.mark.asyncio
async def test_design_constraints(app: Any, loop: Loop) -> None:
    scope = loop.a
    organization = scope.organization_id

    def changed(builder: Any, **values: Any) -> tuple[str, list[Any]]:
        sql, args = builder(scope)
        columns = sql[sql.index("(") + 1 : sql.index(")")].split(", ")
        row = dict(zip(columns, args, strict=True))
        row.update(values)
        table = sql.split()[2].removeprefix("loop.")
        return insert(table, row)

    cases = {
        "adjunto sin declaración": (
            changed(closure_attachment, no_identifiable_person_declared=False),
            CHECK_VIOLATION,
        ),
        "adjunto de 10 MB + 1": (changed(closure_attachment, size_bytes=10485761), CHECK_VIOLATION),
        "adjunto con ruta en el nombre": (
            changed(closure_attachment, filename="a/b.png"),
            CHECK_VIOLATION,
        ),
        "adjunto en la clave de un clip": (
            changed(
                closure_attachment,
                storage_key=f"org/{organization}/plant/{scope.plant_id}/node/x/{uuid.uuid4()}.png",
            ),
            CHECK_VIOLATION,
        ),
        "adjunto de tipo no admitido": (
            changed(closure_attachment, content_type="image/gif"),
            CHECK_VIOLATION,
        ),
        "suspendido sin estado previo": (
            changed(
                finding_state,
                lifecycle_state="suspended_by_reclassification",
                previous_lifecycle_state=None,
            ),
            CHECK_VIOLATION,
        ),
        "estado fuera de la lista": (
            changed(finding_state, lifecycle_state="safe"),
            CHECK_VIOLATION,
        ),
        "ventana de agregación 14": (
            changed(finding_state, aggregation_window_minutes=14),
            CHECK_VIOLATION,
        ),
        "cola con umbral de revisión mayor que el de publicación": (
            changed(review_queue_item, review_threshold=0.8),
            CHECK_VIOLATION,
        ),
        "configuración con mínimo mayor que el plazo por defecto": (
            changed(plant_loop_configuration, action_deadline_min_days=40),
            CHECK_VIOLATION,
        ),
        "lista con versión 0": (
            changed(blocked_vocabulary_list, version=0),
            CHECK_VIOLATION,
        ),
    }
    results = {
        name: await _steps(app, organization, statement) for name, (statement, _) in cases.items()
    }
    assert results == {name: expected for name, (_, expected) in cases.items()}
    # Los bordes válidos pasan: 10 MB exactos, ventana 15 y 480, un solo umbral igual.
    for statement in (
        changed(closure_attachment, size_bytes=10485760),
        changed(finding_state, aggregation_window_minutes=15),
        changed(finding_state, aggregation_window_minutes=480),
        changed(review_queue_item, review_threshold=0.7),
    ):
        assert await _steps(app, organization, statement) == "ok", statement


# --- PAT-LAZ-ESC-03: los planes de la bandeja y de los vencidos -----

_INBOX: Final = (
    "SELECT finding_state_id, occurred_at_bucket FROM loop.finding_state"
    " WHERE organization_id = $1 AND plant_id = $2 AND zone_id = $3"
    " AND lifecycle_state = ANY ($4::text[])"
    " ORDER BY occurred_at_bucket DESC, finding_state_id DESC LIMIT 200"
)
_OVERDUE: Final = (
    "SELECT finding_state_id, due_at FROM loop.finding_state"
    " WHERE organization_id = $1 AND plant_id = $2"
    " AND lifecycle_state IN ('action_assigned', 'overdue') AND due_at < $3"
    " ORDER BY due_at, finding_state_id LIMIT 200"
)


@pytest.fixture(scope="module")
def planned(loop: Loop) -> Loop:
    """Un mes de proyección en la planta de A: 4 000 hallazgos, el 5 % activos, y ``ANALYZE``."""

    async def fill() -> None:
        connection = await loop.database.connect()
        try:
            async with connection.transaction():
                for index in range(4000):
                    at = loop.now + dt.timedelta(minutes=index)
                    active = index % 20 == 0
                    state = (
                        ("open", "action_assigned", "overdue")[index % 3] if active else "closed"
                    )
                    due = at + dt.timedelta(days=30) if state != "open" else None
                    sql, args = finding_state(loop.a, at, state=state, due_at=due)
                    await connection.execute(sql, *args)
            await connection.execute("ANALYZE loop.finding_state")
        finally:
            await connection.close()

    asyncio.run(fill())
    return loop


async def _partition_indexes(connection: Any, parent_index: str) -> set[str]:
    """Los índices de cada partición que cuelgan del índice ``parent_index`` de la tabla padre."""
    rows = await connection.fetch(
        "SELECT child.relname FROM pg_inherits i JOIN pg_class child ON child.oid = i.inhrelid"
        " WHERE i.inhparent = $1::regclass",
        f"loop.{parent_index}",
    )
    return {str(row["relname"]) for row in rows}


def _nodes(plan: Any) -> list[Any]:
    found = []
    stack = [plan]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(node.get("Plans", []))
    return found


def _scans(plan: Any, relation: str) -> list[set[str]]:
    """Por cada nodo que lee ``relation``, los índices que usa (el suyo o, en un recorrido por
    mapa de bits, los de sus hijos). Un recorrido secuencial da un conjunto vacío."""
    return [
        {str(child["Index Name"]) for child in _nodes(node) if "Index Name" in child}
        for node in _nodes(plan)
        if node.get("Relation Name") == relation
    ]


async def _explain(connection: Any, organization_id: uuid.UUID, sql: str, *args: Any) -> Any:
    async with connection.transaction():
        await set_scope(connection, organization_id)
        raw = await connection.fetchval(f"EXPLAIN (FORMAT JSON) {sql}", *args)
    return json.loads(raw)[0]["Plan"]


def _month_partition(loop: Loop) -> str:
    return f"finding_state_{loop.month.year:04d}_{loop.month.month:02d}"


@pytest.mark.asyncio
async def test_the_inbox_query_uses_the_partial_index(
    app: Any, superuser: Any, planned: Loop
) -> None:
    scope = planned.a
    plan = await _explain(
        app,
        scope.organization_id,
        _INBOX,
        scope.organization_id,
        scope.plant_id,
        scope.zone_id,
        list(MIGRATION.ACTIVE_STATES),
    )
    inbox = await _partition_indexes(superuser, "finding_state_inbox")
    scans = _scans(plan, _month_partition(planned))
    assert scans, plan
    assert all(used and used <= inbox for used in scans), (scans, plan)


@pytest.mark.asyncio
async def test_the_overdue_query_uses_the_partial_index(
    app: Any, superuser: Any, planned: Loop
) -> None:
    scope = planned.a
    plan = await _explain(
        app,
        scope.organization_id,
        _OVERDUE,
        scope.organization_id,
        scope.plant_id,
        planned.now + dt.timedelta(days=40),
    )
    overdue = await _partition_indexes(superuser, "finding_state_overdue")
    scans = _scans(plan, _month_partition(planned))
    assert scans, plan
    assert all(used and used <= overdue for used in scans), (scans, plan)


@pytest.mark.asyncio
async def test_without_the_partial_index_the_plan_check_fails(
    superuser: Any, planned: Loop
) -> None:
    """Sonda negativa: sin ``finding_state_inbox`` la comprobación del plan ya no se cumple."""
    scope = planned.a
    month = _month_partition(planned)
    try:
        async with superuser.transaction():
            inbox = await _partition_indexes(superuser, "finding_state_inbox")
            await superuser.execute("DROP INDEX loop.finding_state_inbox")
            await superuser.execute("SET LOCAL ROLE vigia_app")
            plan = await _explain(
                superuser,
                scope.organization_id,
                _INBOX,
                scope.organization_id,
                scope.plant_id,
                scope.zone_id,
                list(MIGRATION.ACTIVE_STATES),
            )
            scans = _scans(plan, month)
            assert scans and not all(used and used <= inbox for used in scans), scans
            raise _Rollback
    except _Rollback:
        pass


# --- Concurrencia: las garantías de una sola vez -----
# Al final del módulo: la base sin guardas vuelve a migrar y cambia las contraseñas de los
# roles del clúster, así que ninguna prueba de la primera base puede venir después.


@dataclass(frozen=True)
class Race:
    """Dos escrituras que compiten; ``loser`` es el SQLSTATE de la que llega segunda."""

    first: tuple[str, list[Any]]
    second: tuple[str, list[Any]]
    loser: str
    organization_id: uuid.UUID | None = None
    """La organización del contexto, si no es la de la planta 0 de A."""


async def _committed(connection: Any, organization_id: uuid.UUID, *steps: Any) -> None:
    async with connection.transaction():
        await set_scope(connection, organization_id)
        for sql, args in steps:
            await connection.execute(sql, *args)


async def _race_scenarios(loop: Loop) -> dict[str, Race]:
    """Cada garantía con dos escrituras que chocan; lo que necesitan se siembra antes."""
    scope = loop.a
    identity = loop.seed.identity
    superuser = await loop.database.connect()
    try:
        attachment_id = await _attachment(superuser, scope)
        request_id = await _request(superuser, scope)
        plant = await insert_plant(
            superuser, scope.organization_id, scope.user_id, identity.operator_id
        )
        new_plant = LoopScope(
            scope.organization_id,
            plant.plant_id,
            plant.zone_id,
            plant.node_id,
            scope.user_id,
            scope.list_id,
        )
        other_organization = uuid.uuid4()
        await _insert_organization(superuser, other_organization, "client", identity.operator_id)
    finally:
        await superuser.close()
    other = LoopScope(
        other_organization,
        scope.plant_id,
        scope.zone_id,
        scope.node_id,
        scope.user_id,
        uuid.uuid4(),
    )
    term_value = f"carrera{uuid.uuid4().hex[:8]}"

    def term() -> tuple[str, list[Any]]:
        return insert(
            "blocked_term",
            {
                "term_id": uuid.uuid4(),
                "list_id": scope.list_id,
                "organization_id": scope.organization_id,
                "category": "absence_claim",
                "form": "lemma",
                "value": term_value,
                "suggestion_es": "Alternativa sintética",
                "origin": "customer_added",
                "added_in_version": 1,
                "added_reason": "Motivo sintético del término",
            },
        )

    source_event = uuid.uuid4()

    def queued() -> tuple[str, list[Any]]:
        sql, args = review_queue_item(scope)
        return insert(
            "review_queue_item",
            dict(zip(_columns(sql), args, strict=True)) | {"source_event_id": source_event},
        )

    subject = uuid.uuid4()

    def projected() -> tuple[str, list[Any]]:
        sql, args = finding_state(scope, loop.now)
        return insert(
            "finding_state", dict(zip(_columns(sql), args, strict=True)) | {"subject_id": subject}
        )

    def verify(minutes: int) -> tuple[str, list[Any]]:
        return (
            "UPDATE loop.closure_attachment SET verified_at = uploaded_at + $2::interval"
            " WHERE attachment_id = $1",
            [attachment_id, dt.timedelta(minutes=minutes)],
        )

    def resolve(state: str, cause: str | None) -> tuple[str, list[Any]]:
        return (
            "UPDATE loop.assistant_request SET state = $2, unavailable_cause = $3,"
            " resolved_at = requested_at + interval '1 minute' WHERE request_id = $1",
            [request_id, state, cause],
        )

    # La lista de la otra organización: su contexto es el suyo.
    lists = (blocked_vocabulary_list(other), blocked_vocabulary_list(other))
    return {
        "un término vigente por organización": Race(term(), term(), UNIQUE_VIOLATION),
        "una lista de vocabulario por organización": Race(
            *lists, UNIQUE_VIOLATION, organization_id=other_organization
        ),
        "una configuración del lazo por planta": Race(
            plant_loop_configuration(new_plant),
            plant_loop_configuration(new_plant),
            UNIQUE_VIOLATION,
        ),
        "un control de la proyección por planta": Race(
            loop_projection_checkpoint(new_plant),
            loop_projection_checkpoint(new_plant),
            UNIQUE_VIOLATION,
        ),
        "un elemento de la cola por evento": Race(queued(), queued(), UNIQUE_VIOLATION),
        "una fila de la proyección por sujeto": Race(projected(), projected(), UNIQUE_VIOLATION),
        "verified_at se cierra una vez": Race(verify(1), verify(2), RESTRICT_VIOLATION),
        "la solicitud sale de pending una vez": Race(
            resolve("ready", None), resolve("unavailable", "timeout"), RESTRICT_VIOLATION
        ),
    }


def _columns(sql: str) -> list[str]:
    return sql[sql.index("(") + 1 : sql.index(")")].split(", ")


RACES: Final = (
    "un término vigente por organización",
    "una lista de vocabulario por organización",
    "una configuración del lazo por planta",
    "un control de la proyección por planta",
    "un elemento de la cola por evento",
    "una fila de la proyección por sujeto",
    "verified_at se cierra una vez",
    "la solicitud sale de pending una vez",
)


def _race_organization(loop: Loop, race: Race) -> uuid.UUID:
    return race.organization_id or loop.a.organization_id


async def _race(database: MigratedDatabase, organization_id: uuid.UUID, race: Race) -> list[str]:
    """La primera escribe y sigue abierta; la segunda espera su candado; se confirma la primera."""
    first = await database.connect("vigia_app")
    second = await database.connect("vigia_app")
    observer = await database.connect()
    try:
        transaction = first.transaction()
        await transaction.start()
        try:
            await set_scope(first, organization_id)
            await first.execute(race.first[0], *race.first[1])

            async def contender() -> str:
                try:
                    await _committed(second, organization_id, race.second)
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
@pytest.mark.parametrize("name", RACES)
async def test_once_guarantees_hold_under_concurrency(name: str, loop: Loop) -> None:
    race = (await _race_scenarios(loop))[name]
    assert await _race(loop.database, _race_organization(loop, race), race) == ["ok", race.loser]


_GUARDS_REMOVED: Final = (
    "DROP INDEX loop.blocked_term_one_current",
    "ALTER TABLE loop.blocked_vocabulary_list"
    " DROP CONSTRAINT blocked_vocabulary_list_one_per_organization",
    "ALTER TABLE loop.plant_loop_configuration DROP CONSTRAINT plant_loop_configuration_scope",
    "ALTER TABLE loop.plant_loop_configuration DROP CONSTRAINT plant_loop_configuration_pkey",
    "ALTER TABLE loop.loop_projection_checkpoint DROP CONSTRAINT loop_projection_checkpoint_pkey",
    "ALTER TABLE loop.review_queue_item DROP CONSTRAINT review_queue_item_source_event",
    "ALTER TABLE loop.finding_state DROP CONSTRAINT finding_state_subject",
    "DROP TRIGGER append_only_update ON loop.closure_attachment",
    "DROP TRIGGER append_only_update ON loop.assistant_request",
)


@pytest.fixture(scope="module")
def unguarded(postgres_endpoint: PostgresEndpoint) -> Iterator[Loop]:
    """Otra base igual, sin las guardas de las carreras (sonda negativa)."""
    with seeded_identity(postgres_endpoint, "vigia_laz_0029_unguarded") as (database, identity):
        loop = _prepare(database, identity)

        async def strip() -> None:
            connection = await database.connect()
            try:
                for statement in _GUARDS_REMOVED:
                    await connection.execute(statement)
            finally:
                await connection.close()

        asyncio.run(strip())
        yield loop


@pytest.mark.asyncio
@pytest.mark.parametrize("name", RACES)
async def test_once_guarantees_without_their_guard(name: str, unguarded: Loop) -> None:
    race = (await _race_scenarios(unguarded))[name]
    # La misma carrera: sin la guarda la segunda no espera ningún candado y las dos pasan.
    assert await _race(unguarded.database, _race_organization(unguarded, race), race) == [
        "ok",
        "ok",
    ]
