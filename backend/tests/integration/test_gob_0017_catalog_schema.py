"""Esquema ``catalog`` de ``gob_0017`` (TASK-202, LC-GOB-21 parte 1, R-GOB-14, NFR-GOB-16).

Base migrada propia del módulo (``alembic upgrade head`` como proceso aparte) con los datos de
``tests/identity_db.py`` y una fila de cada tabla de ``catalog`` en cada planta
(``tests/catalog_db.py``). Criterios de TASK-202:

- ``alembic upgrade head`` aplica ``gob_0017``: esquema, 18 tablas, RLS forzada con las dos
  políticas y disparadores (``test_upgrade_head_applies_gob_0017``); las definiciones de
  SQLAlchemy Core coinciden con la base (``test_core_tables_match_the_database``) y
  ``migrations/append_only.py`` lista las ⛓.
- R-GOB-14: ``test_btree_gist_and_gist_exclusion`` falla si falta ``btree_gist`` o si
  ``gate_state_no_overlap`` no es una exclusión GiST con ``=``, ``=`` y ``&&``.
- Solo anexar con ``vigia_app``: ``DELETE``, ``TRUNCATE``, ``UPDATE`` fuera de la lista blanca,
  segundo cierre y ``effective_until`` acotado → otro valor o nulo fallan; el primer cierre pasa.
  Con el dueño (``vigia_migrate``), que tiene todos los privilegios, falla el disparador.
- Transiciones de estado solo hacia adelante (``use_agreement``, ``occlusion_test``,
  ``document_upload_grant``) y bordes de las restricciones que fija el diseño.
- NFR-GOB-16: una lectura de ``gate_history`` de 366 días devuelve completos los intervalos
  escritos con fechas de hace 24 meses.

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
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from tests.catalog_db import (
    APPEND_ONLY_TABLES,
    CATALOG_TABLES,
    CatalogSeed,
    PlantScope,
    document_upload_grant,
    gate_interval,
    occlusion_test,
    seed_catalog,
    use_agreement,
    walk_test_session,
    walk_test_step,
)
from tests.identity_db import BASE_TIME, MigratedDatabase, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_REVISION, HEAD_SCHEMA_VERSION
from vigia_platform.catalog.adapters.postgres import tables as catalog_tables

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
INSUFFICIENT_PRIVILEGE = "42501"
RESTRICT_VIOLATION = "23001"
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
EXCLUSION_VIOLATION = "23P01"


def _load_migration() -> Any:
    path = BACKEND / "migrations" / "versions" / "gob_0017_catalog_schema.py"
    spec = importlib.util.spec_from_file_location("gob_0017_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


@dataclass(frozen=True)
class Catalog:
    database: MigratedDatabase
    seed: CatalogSeed

    @property
    def a(self) -> PlantScope:
        return self.seed.plant(self.seed.identity.a.organization_id)


@pytest.fixture(scope="module")
def catalog(postgres_endpoint: PostgresEndpoint) -> Iterator[Catalog]:
    with seeded_identity(postgres_endpoint, "vigia_gob_0017") as (database, seed):

        async def prepare() -> CatalogSeed:
            connection = await database.connect()
            try:
                return await seed_catalog(connection, seed)
            finally:
                await connection.close()

        yield Catalog(database, asyncio.run(prepare()))


@pytest_asyncio.fixture
async def superuser(catalog: Catalog) -> AsyncIterator[Any]:
    connection = await catalog.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(catalog: Catalog) -> AsyncIterator[Any]:
    connection = await catalog.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def owner(catalog: Catalog) -> AsyncIterator[Any]:
    connection = await catalog.database.connect("vigia_migrate")
    try:
        yield connection
    finally:
        await connection.close()


class _Rollback(Exception):
    """Revierte la transacción de la prueba."""


async def _sqlstate(connection: Any, organization_id: uuid.UUID, sql: str, *args: Any) -> str:
    """``ok`` o el SQLSTATE de ``sql`` en una transacción con el contexto, siempre revertida."""
    try:
        async with connection.transaction():
            await set_scope(connection, organization_id)
            try:
                await connection.execute(sql, *args)
            except asyncpg.PostgresError as error:
                return str(error.sqlstate)
            raise _Rollback
    except _Rollback:
        return "ok"


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


# --- Criterio 1: alembic upgrade head aplica gob_0017 -----


@pytest.mark.asyncio
async def test_upgrade_head_applies_gob_0017(superuser: Any) -> None:
    assert HEAD_SCHEMA_VERSION >= 17
    assert await superuser.fetchval("SELECT version_num FROM public.alembic_version") == (
        HEAD_REVISION
    )
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'catalog'"
        )
        == "vigia_migrate"
    )
    rows = await superuser.fetch(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
        " pg_get_userbyid(c.relowner) AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'catalog' AND c.relkind IN ('r', 'p')"
    )
    assert {row["relname"] for row in rows} == set(CATALOG_TABLES)
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows), rows
    assert {row["owner"] for row in rows} == {"vigia_migrate"}

    policies = await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'catalog'"
    )
    by_table: dict[str, dict[str, Any]] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], {})[policy["policyname"]] = policy
    assert set(by_table) == set(CATALOG_TABLES)
    for table, named in by_table.items():
        assert set(named) == {"organization_isolation", "provider_concession_scope"}, table
        isolation, provider = named["organization_isolation"], named["provider_concession_scope"]
        assert (isolation["permissive"], isolation["cmd"]) == ("PERMISSIVE", "ALL"), table
        assert "vigia_current_organization()" in isolation["qual"]
        assert (provider["permissive"], provider["cmd"]) == ("RESTRICTIVE", "ALL"), table
        for clause in (provider["qual"], provider["with_check"]):
            assert "rls_provider_scope_allows(organization_id, plant_id)" in clause, table

    # Ningún DELETE ni TRUNCATE para vigia_app; SELECT e INSERT en todas.
    for table in CATALOG_TABLES:
        privileges = {
            privilege: await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)", f"catalog.{table}", privilege
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
        }
        assert privileges == {"SELECT": True, "INSERT": True, "DELETE": False, "TRUNCATE": False}


@pytest.mark.asyncio
async def test_app_update_privileges_are_exactly_the_whitelist(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT table_name, column_name FROM information_schema.column_privileges"
        " WHERE table_schema = 'catalog' AND grantee = 'vigia_app' AND privilege_type = 'UPDATE'"
    )
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(row["table_name"], set()).add(row["column_name"])
    expected = {
        table: set(MIGRATION.app_updatable_columns(table))
        for table in CATALOG_TABLES
        if MIGRATION.app_updatable_columns(table)
    }
    assert granted == expected
    # Tablas ⛓ sin cierre ni estado: ningún UPDATE.
    for table in ("family_admission", "mounting_gate_record", "agreement_confirmation"):
        assert table not in granted


def test_append_only_registry_lists_every_append_only_table() -> None:
    spec = importlib.util.spec_from_file_location(
        "append_only_under_test", BACKEND / "migrations" / "append_only.py"
    )
    assert spec is not None and spec.loader is not None
    registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registry)
    listed = {name for name in registry.APPEND_ONLY_TABLES if name.startswith("catalog.")}
    assert listed == {f"catalog.{table}" for table in APPEND_ONLY_TABLES}
    assert set(MIGRATION.APPEND_ONLY_TABLES) == set(APPEND_ONLY_TABLES)


@pytest.mark.asyncio
async def test_append_only_triggers_are_always_enabled(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname, t.tgname, t.tgenabled::text AS tgenabled FROM pg_trigger t"
        " JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'catalog' AND NOT t.tgisinternal"
    )
    triggers: dict[str, dict[str, str]] = {}
    for row in rows:
        triggers.setdefault(row["relname"], {})[row["tgname"]] = row["tgenabled"]
    for table in APPEND_ONLY_TABLES:
        assert triggers[table] == {
            "append_only_update": "A",
            "append_only_delete": "A",
            "append_only_no_truncate": "A",
        }, table
    assert triggers["document_upload_grant"] == {"state_transition": "A"}
    assert set(triggers) == {*APPEND_ONLY_TABLES, "document_upload_grant"}


@pytest.mark.asyncio
async def test_core_tables_match_the_database(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT table_name, column_name, is_nullable, is_generated FROM information_schema.columns"
        " WHERE table_schema = 'catalog'"
    )
    database: dict[str, dict[str, tuple[bool, bool]]] = {}
    for row in rows:
        database.setdefault(row["table_name"], {})[row["column_name"]] = (
            row["is_nullable"] == "YES",
            row["is_generated"] == "ALWAYS",
        )
    core = {
        table.name: {
            column.name: (bool(column.nullable), column.computed is not None)
            for column in table.columns
        }
        for table in catalog_tables.METADATA.tables.values()
    }
    assert core == database
    primary_keys = {
        row["table_name"]: set(row["columns"])
        for row in await superuser.fetch(
            "SELECT c.relname AS table_name, array_agg(a.attname::text) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
            " WHERE n.nspname = 'catalog' AND i.indisprimary GROUP BY c.relname"
        )
    }
    assert primary_keys == {
        table.name: {column.name for column in table.primary_key.columns}
        for table in catalog_tables.METADATA.tables.values()
    }


@pytest.mark.asyncio
async def test_every_index_starts_with_the_scope_or_is_a_key(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, i.relname AS index_name,"
        " (SELECT array_agg(a.attname::text ORDER BY k.ordinality)"
        "  FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ordinality)"
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS columns,"
        " x.indisunique OR x.indisprimary OR x.indisexclusion AS constraint_index"
        " FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid"
        " JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'catalog'"
    )
    scoped = {
        row["table_name"] for row in rows if row["columns"][:2] == ["organization_id", "plant_id"]
    }
    assert scoped == set(CATALOG_TABLES)
    loose = [
        row["index_name"]
        for row in rows
        if row["columns"][:2] != ["organization_id", "plant_id"] and not row["constraint_index"]
    ]
    assert loose == []


# --- R-GOB-14 -----


@pytest.mark.asyncio
async def test_btree_gist_and_gist_exclusion(superuser: Any) -> None:
    assert (
        await superuser.fetchval(
            "SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'btree_gist'"
        )
        == "public"
    )
    constraint = await superuser.fetchrow(
        "SELECT con.contype::text AS contype, am.amname,"
        " pg_get_constraintdef(con.oid) AS definition"
        " FROM pg_constraint con JOIN pg_class idx ON idx.oid = con.conindid"
        " JOIN pg_am am ON am.oid = idx.relam"
        " WHERE con.conname = 'gate_state_no_overlap'"
        " AND con.conrelid = 'catalog.gate_state_history'::regclass"
    )
    assert constraint is not None
    assert (constraint["contype"], constraint["amname"]) == ("x", "gist")
    assert constraint["definition"] == (
        "EXCLUDE USING gist (zone_id WITH =, gate WITH =, effective WITH &&)"
    )
    # La columna de rango es la generada [effective_from, effective_until).
    generation = await superuser.fetchval(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d"
        " JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum"
        " WHERE d.adrelid = 'catalog.gate_state_history'::regclass AND a.attname = 'effective'"
    )
    assert generation == "tstzrange(effective_from, effective_until, '[)'::text)"
    # La tabla no está particionada: la exclusión vale sobre la tabla completa.
    assert (
        await superuser.fetchval(
            "SELECT relkind::text FROM pg_class WHERE oid = 'catalog.gate_state_history'::regclass"
        )
        == "r"
    )


@pytest.mark.asyncio
async def test_overlapping_interval_is_an_exclusion_violation(app: Any, catalog: Catalog) -> None:
    a = catalog.a
    zone = await _new_zone(catalog)
    first = gate_interval(a, BASE_TIME, BASE_TIME + dt.timedelta(hours=2), zone_id=zone)
    overlapping = gate_interval(
        a, BASE_TIME + dt.timedelta(hours=1), None, zone_id=zone, status="pending"
    )
    touching = gate_interval(a, BASE_TIME + dt.timedelta(hours=2), None, zone_id=zone)
    other_gate = gate_interval(a, BASE_TIME, None, zone_id=zone, gate="usage")
    assert await _steps(app, a.organization_id, first, overlapping) == EXCLUSION_VIOLATION
    # [from, until): dos intervalos que se tocan en un instante no se solapan.
    assert await _steps(app, a.organization_id, first, touching, other_gate) == "ok"


async def _new_zone(catalog: Catalog) -> uuid.UUID:
    """Zona nueva de la planta 0 de A, sin intervalos (como superusuario)."""
    a = catalog.a
    zone_id = uuid.uuid4()
    connection = await catalog.database.connect()
    try:
        await connection.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            a.organization_id,
            a.plant_id,
            f"ZG-{zone_id.hex[:8].upper()}",
            BASE_TIME,
            a.user_id,
        )
    finally:
        await connection.close()
    return zone_id


# --- Solo anexar con vigia_app -----


@pytest.mark.asyncio
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_delete_and_truncate_fail_for_the_app_and_the_owner(
    table: str, app: Any, owner: Any, catalog: Catalog
) -> None:
    organization_id = catalog.a.organization_id
    assert await _sqlstate(app, organization_id, f"DELETE FROM catalog.{table}") == (  # noqa: S608
        INSUFFICIENT_PRIVILEGE
    )
    assert await _sqlstate(app, organization_id, f"TRUNCATE catalog.{table}") == (
        INSUFFICIENT_PRIVILEGE
    )
    # El dueño tiene todos los privilegios: lo para el disparador, también con FORCE RLS.
    assert await _sqlstate(owner, organization_id, f"DELETE FROM catalog.{table}") == (  # noqa: S608
        RESTRICT_VIOLATION
    )
    # CASCADE: una tabla referenciada por otra no se vacía sola; el disparador para igual.
    assert await _sqlstate(owner, organization_id, f"TRUNCATE catalog.{table} CASCADE") == (
        RESTRICT_VIOLATION
    )


_OUTSIDE_WHITELIST = {
    "zone_catalog_version": ("reason_es", "'Otro motivo sintético'"),
    "declared_standard_version": ("declared_text", "'Otro texto'"),
    "family_admission": ("justification_es", "'Otra justificación'"),
    "gate_state_history": ("status", "'pending'"),
    "mounting_gate_record": ("scope_text_es", "'Otro alcance'"),
    "use_agreement": ("signatories", "'[{}, {}, {}, {}]'::jsonb"),
    "agreement_confirmation": ("origin", "'transparency'"),
    "plant_policy": ("criteria_summary_es", "'Otro resumen'"),
    "walk_test_step": ("step_kind", "'other'"),
    "walk_test_pass": ("result", "'missed'"),
    "occlusion_test": ("camera_id", "gen_random_uuid()"),
    "commissioning_record": ("total_hours", "1"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_update_outside_the_whitelist_fails(
    table: str, app: Any, owner: Any, catalog: Catalog
) -> None:
    column, value = _OUTSIDE_WHITELIST[table]
    assert column not in MIGRATION.app_updatable_columns(table)
    sql = f"UPDATE catalog.{table} SET {column} = {value}"  # noqa: S608 - constantes de la prueba
    organization_id = catalog.a.organization_id
    assert await _sqlstate(app, organization_id, sql) == INSUFFICIENT_PRIVILEGE
    assert await _sqlstate(owner, organization_id, sql) == RESTRICT_VIOLATION


_CLOSINGS: dict[tuple[str, str], tuple[str, str]] = {
    # (tabla, columna): (primer valor, segundo valor)
    ("zone_catalog_version", "superseded_at"): (
        "$1::timestamptz + interval '1 day'",
        "$1::timestamptz + interval '2 days'",
    ),
    ("declared_standard_version", "retired_in_catalog_version"): ("2", "3"),
    ("walk_test_step", "ended_at"): (
        "$1::timestamptz + interval '1 hour'",
        "$1::timestamptz + interval '2 hours'",
    ),
    ("walk_test_step", "correction"): (
        """'{"reason_es": "Corrección sintética"}'::jsonb""",
        """'{"reason_es": "Otra corrección"}'::jsonb""",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "column"), list(_CLOSINGS))
async def test_closing_is_written_once(table: str, column: str, app: Any, catalog: Catalog) -> None:
    first, second = _CLOSINGS[(table, column)]
    organization_id = catalog.a.organization_id
    prepare: list[tuple[str, list[Any]]] = []
    if table == "walk_test_step":
        step_id = uuid.uuid4()
        prepare.append(walk_test_step(catalog.a, step_id))
        where = f"step_id = '{step_id}'"
    else:
        where = f"organization_id = '{organization_id}' AND plant_id = '{catalog.a.plant_id}'"
    update = f"UPDATE catalog.{table} SET {column} = {{}} WHERE {where}"  # noqa: S608

    def set_to(value: str) -> tuple[str, list[Any]]:
        return update.format(value), [BASE_TIME] if "$1" in value else []

    once, again, back, same = set_to(first), set_to(second), set_to("NULL"), set_to(first)
    assert await _steps(app, organization_id, *prepare, once) == "ok"
    assert await _steps(app, organization_id, *prepare, once, same) == "ok"
    assert await _steps(app, organization_id, *prepare, once, again) == RESTRICT_VIOLATION
    assert await _steps(app, organization_id, *prepare, once, back) == RESTRICT_VIOLATION


@pytest.mark.asyncio
async def test_effective_until_closes_once_from_null_to_a_later_instant(
    app: Any, catalog: Catalog
) -> None:
    a = catalog.a
    zone = await _new_zone(catalog)
    open_interval = gate_interval(a, BASE_TIME, None, zone_id=zone)
    close = (
        "UPDATE catalog.gate_state_history SET effective_until = $1"
        " WHERE zone_id = $2 AND gate = 'mounting' AND effective_from = $3",
    )

    def until(value: dt.datetime | None) -> tuple[str, list[Any]]:
        return close[0], [value, zone, BASE_TIME]

    later, much_later = BASE_TIME + dt.timedelta(hours=1), BASE_TIME + dt.timedelta(hours=2)
    organization_id = a.organization_id
    assert await _steps(app, organization_id, open_interval, until(later)) == "ok"
    # El rango generado sigue al cierre.
    rows = await _closed_effective(app, organization_id, open_interval, until(later), zone)
    assert rows == [(BASE_TIME, later)]
    # Acotado → otro valor, acotado → nulo: nunca.
    assert (
        await _steps(app, organization_id, open_interval, until(later), until(much_later))
        == RESTRICT_VIOLATION
    )
    assert (
        await _steps(app, organization_id, open_interval, until(later), until(None))
        == RESTRICT_VIOLATION
    )
    # Cierre no posterior al inicio: lo rechaza la restricción.
    assert await _steps(app, organization_id, open_interval, until(BASE_TIME)) == CHECK_VIOLATION
    # Ya acotado al insertarlo: tampoco se mueve.
    bounded = gate_interval(a, BASE_TIME, later, zone_id=zone)
    assert await _steps(app, organization_id, bounded, until(much_later)) == RESTRICT_VIOLATION


async def _closed_effective(
    app: Any, organization_id: uuid.UUID, *steps: Any
) -> list[tuple[dt.datetime, dt.datetime | None]]:
    *writes, zone = steps
    try:
        async with app.transaction():
            await set_scope(app, organization_id)
            for sql, args in writes:
                await app.execute(sql, *args)
            rows = await app.fetch(
                "SELECT lower(effective) AS lo, upper(effective) AS hi"
                " FROM catalog.gate_state_history WHERE zone_id = $1",
                zone,
            )
            result = [(row["lo"], row["hi"]) for row in rows]
            raise _Rollback
    except _Rollback:
        return result


# --- Transiciones de estado -----


@pytest.mark.asyncio
async def test_use_agreement_only_moves_forward(app: Any, catalog: Catalog) -> None:
    a = catalog.a
    agreement_id = uuid.uuid4()
    create = use_agreement(a, agreement_id)
    approve = (
        "UPDATE catalog.use_agreement SET status = 'approved', approved_at = $2,"
        " approved_by = $3, ledger_record_id = gen_random_uuid() WHERE agreement_id = $1",
        [agreement_id, BASE_TIME + dt.timedelta(days=1), a.user_id],
    )

    def to(status: str, column: str | None) -> tuple[str, list[Any]]:
        assignment = f", {column} = $2" if column else ""
        return (
            f"UPDATE catalog.use_agreement SET status = '{status}'{assignment}"  # noqa: S608
            " WHERE agreement_id = $1",
            [agreement_id, BASE_TIME + dt.timedelta(days=2)] if column else [agreement_id],
        )

    organization_id = a.organization_id
    assert await _steps(app, organization_id, create, approve) == "ok"
    assert (
        await _steps(app, organization_id, create, approve, to("superseded", "superseded_at"))
        == "ok"
    )
    assert await _steps(app, organization_id, create, approve, to("revoked", "revoked_at")) == "ok"
    # Nunca atrás ni saltando: pending → revoked/superseded, approved → pending, fin → approved.
    assert (
        await _steps(app, organization_id, create, to("revoked", "revoked_at"))
        == RESTRICT_VIOLATION
    )
    assert (
        await _steps(app, organization_id, create, approve, to("pending_signatures", None))
        == RESTRICT_VIOLATION
    )
    assert (
        await _steps(
            app, organization_id, create, approve, to("revoked", "revoked_at"), to("approved", None)
        )
        == RESTRICT_VIOLATION
    )
    assert (
        await _steps(
            app,
            organization_id,
            create,
            approve,
            to("superseded", "superseded_at"),
            to("revoked", "revoked_at"),
        )
        == RESTRICT_VIOLATION
    )
    # Aprobar sin sus cierres no cumple la restricción.
    assert await _steps(app, organization_id, create, to("approved", None)) == CHECK_VIOLATION


@pytest.mark.asyncio
async def test_occlusion_verification_leaves_pending_once(app: Any, catalog: Catalog) -> None:
    a = catalog.a
    test_id = uuid.uuid4()
    create = occlusion_test(a, test_id)
    verified = (
        "UPDATE catalog.occlusion_test SET verification = 'verified',"
        " correlated_event_ids = ARRAY[gen_random_uuid()], ledger_record_id = gen_random_uuid()"
        " WHERE test_id = $1",
        [test_id],
    )
    failed = (
        "UPDATE catalog.occlusion_test SET verification = 'failed',"
        " ledger_record_id = gen_random_uuid() WHERE test_id = $1",
        [test_id],
    )
    declared = (
        "UPDATE catalog.occlusion_test SET verification = 'declared',"
        " declared_reason_es = 'Declaración manual sintética', ledger_record_id = gen_random_uuid()"
        " WHERE test_id = $1",
        [test_id],
    )
    back = (
        "UPDATE catalog.occlusion_test SET verification = 'pending' WHERE test_id = $1",
        [test_id],
    )
    organization_id = a.organization_id
    for result in (verified, failed, declared):
        assert await _steps(app, organization_id, create, result) == "ok"
    assert await _steps(app, organization_id, create, verified, failed) == RESTRICT_VIOLATION
    assert await _steps(app, organization_id, create, failed, back) == RESTRICT_VIOLATION
    assert await _steps(app, organization_id, create, declared, verified) == RESTRICT_VIOLATION
    # verified sin eventos y declared sin motivo: las restricciones lo rechazan.
    no_events = (
        "UPDATE catalog.occlusion_test SET verification = 'verified',"
        " ledger_record_id = gen_random_uuid() WHERE test_id = $1",
        [test_id],
    )
    assert await _steps(app, organization_id, create, no_events) == CHECK_VIOLATION


@pytest.mark.asyncio
async def test_document_upload_grant_goes_from_issued_to_used_or_expired(
    app: Any, catalog: Catalog
) -> None:
    a = catalog.a
    document_id = uuid.uuid4()
    create = document_upload_grant(a, document_id)

    def to(status: str) -> tuple[str, list[Any]]:
        return (
            "UPDATE catalog.document_upload_grant SET status = $2 WHERE document_id = $1",
            [document_id, status],
        )

    organization_id = a.organization_id
    assert await _steps(app, organization_id, create, to("used")) == "ok"
    assert await _steps(app, organization_id, create, to("expired")) == "ok"
    for steps in ((to("used"), to("issued")), (to("expired"), to("used")), (to("orphan"),)):
        assert await _steps(app, organization_id, create, *steps) == RESTRICT_VIOLATION
    # Fuera del estado, nada cambia.
    resized = (
        "UPDATE catalog.document_upload_grant SET size_bytes = 2 WHERE document_id = $1",
        [document_id],
    )
    assert await _steps(app, organization_id, create, resized) == INSUFFICIENT_PRIVILEGE


# --- Bordes de las restricciones del diseño -----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("aggregation_window_minutes = 14", CHECK_VIOLATION),
        ("aggregation_window_minutes = 15", "ok"),
        ("aggregation_window_minutes = 480", "ok"),
        ("aggregation_window_minutes = 481", CHECK_VIOLATION),
        ("changed_fields = '{}'", CHECK_VIOLATION),
        ("changed_fields = ARRAY['cameras']", "ok"),
        (
            "changed_fields = ARRAY['standards', 'cameras', 'minimum_coverage', 'signals',"
            " 'thresholds', 'clip_window', 'episode', 'single_occupancy']",
            "ok",
        ),
        ("changed_fields = ARRAY['cameras', 'cameras']", CHECK_VIOLATION),
        ("changed_fields = ARRAY['cameras', NULL]", CHECK_VIOLATION),
        ("changed_fields = ARRAY[['cameras'], ['signals']]", CHECK_VIOLATION),
        ("changed_fields = ARRAY['predicate']", CHECK_VIOLATION),
        ("reason_es = repeat('x', 9)", CHECK_VIOLATION),
        ("reason_es = repeat('x', 10)", "ok"),
        ("reason_es = repeat('x', 500)", "ok"),
        ("reason_es = repeat('x', 501)", CHECK_VIOLATION),
        ("catalog_version = 0", CHECK_VIOLATION),
        ("role_in_use = 'worker'", CHECK_VIOLATION),
    ],
)
async def test_zone_catalog_version_bounds(
    assignment: str, expected: str, superuser: Any, catalog: Catalog
) -> None:
    # El borde se comprueba al insertar (la versión emitida no se edita): copia con el cambio.
    a = catalog.a
    sql = (
        "INSERT INTO catalog.zone_catalog_version SELECT (row_).* FROM ("
        " SELECT jsonb_populate_record(NULL::catalog.zone_catalog_version,"
        " to_jsonb(v) || jsonb_build_object('catalog_version', 1000)) AS row_"
        " FROM catalog.zone_catalog_version AS v WHERE zone_id = $1 AND catalog_version = 1) AS s"
    )
    update = f"UPDATE catalog.zone_catalog_version SET {assignment} WHERE catalog_version = 1000"  # noqa: S608
    steps = await _owner_steps(superuser, a.organization_id, (sql, [a.zone_id]), (update, []))
    assert steps == expected


async def _owner_steps(connection: Any, organization_id: uuid.UUID, *steps: Any) -> str:
    """Como superusuario, revertido, sin la guarda de ``UPDATE`` de ``zone_catalog_version``.

    La versión emitida no se edita: para comprobar cada ``CHECK`` por separado sobre una copia,
    la guarda se desactiva solo dentro de esta transacción, que se revierte siempre.
    """
    try:
        async with connection.transaction():
            await set_scope(connection, organization_id)
            await connection.execute(
                "ALTER TABLE catalog.zone_catalog_version DISABLE TRIGGER append_only_update"
            )
            for sql, args in steps:
                try:
                    await connection.execute(sql, *args)
                except asyncpg.PostgresError as error:
                    return str(error.sqlstate)
            raise _Rollback
    except _Rollback:
        return "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "values", "expected"),
    [
        ("zone_camera", "declared_min_fps = 0.99", CHECK_VIOLATION),
        ("zone_camera", "declared_min_fps = 1", "ok"),
        ("zone_camera", "declared_min_fps = 60", "ok"),
        ("zone_camera", "declared_min_fps = 60.01", CHECK_VIOLATION),
        ("zone_camera", "stream_reference = 'Cam-1'", CHECK_VIOLATION),
        ("zone_camera", "stream_reference = 'rtsp://user:pw@10.0.0.1/s'", CHECK_VIOLATION),
        ("zone_camera", "stream_reference = repeat('c', 64)", "ok"),
        ("zone_camera", "stream_reference = repeat('c', 65)", CHECK_VIOLATION),
        ("zone_camera", "role_in_zone = 'redundant'", "ok"),
        ("zone_camera", "role_in_zone = 'backup'", CHECK_VIOLATION),
        ("plant_signatory_policy", "minimum = 2", CHECK_VIOLATION),
        ("plant_signatory_policy", "minimum = 3", "ok"),
        ("plant_signatory_policy", "required_roles = ARRAY['plant_manager']", CHECK_VIOLATION),
        ("plant_signatory_policy", "required_roles = ARRAY['copasst']", "ok"),
        ("plant_signatory_policy", "required_roles = ARRAY['copasst', 'copasst']", CHECK_VIOLATION),
        ("plant_signatory_policy", "required_roles = ARRAY['copasst', 'auditor']", CHECK_VIOLATION),
        ("plant_signatory_policy", "workers_role = 'plant_manager'", INSUFFICIENT_PRIVILEGE),
        (
            "zone_gate_state",
            "mounting = '{\"status\": \"revoked\"}', resulting_mode = 'no_capture'",
            "ok",
        ),
        (
            "zone_gate_state",
            "mounting = '{\"status\": \"revoked\"}', resulting_mode = 'commissioning'",
            CHECK_VIOLATION,
        ),
        (
            "zone_gate_state",
            "usage = '{\"status\": \"approved\"}', resulting_mode = 'productive'",
            "ok",
        ),
        (
            "zone_gate_state",
            "usage = '{\"status\": \"revoked\"}', resulting_mode = 'productive'",
            CHECK_VIOLATION,
        ),
        ("zone_gate_state", 'usage = \'{"status": "closed"}\'', CHECK_VIOLATION),
        ("zone_gate_state", "usage = '{}'", CHECK_VIOLATION),
        ("zone_gate_state", "mounting = '{\"decided_at\": null}'", CHECK_VIOLATION),
        ("zone_gate_state", "usage = '[]'", CHECK_VIOLATION),
        ("zone_gate_state", "valid_until = valid_until + interval '1 second'", CHECK_VIOLATION),
        ("walk_test_regression", "state = 'pending'", CHECK_VIOLATION),
        (
            "walk_test_regression",
            "state = 'pending', marked_at = '2026-09-01T08:00:00Z', cause = 'framing_recaptured',"
            " affected_row_ids = '\"all\"'",
            "ok",
        ),
        (
            "walk_test_regression",
            "state = 'pending', marked_at = '2026-09-01T08:00:00Z', cause = 'catalog_change',"
            " affected_row_ids = '\"some\"'",
            CHECK_VIOLATION,
        ),
        ("walk_test_regression", "cleared_at = '2026-09-01T08:00:00Z'", CHECK_VIOLATION),
    ],
)
async def test_projection_bounds(
    table: str, values: str, expected: str, app: Any, catalog: Catalog
) -> None:
    # Las proyecciones 🔒 se actualizan con vigia_app sobre sus columnas mutables.
    a = catalog.a
    sql = f"UPDATE catalog.{table} SET {values} WHERE plant_id = $1"  # noqa: S608
    assert await _sqlstate(app, a.organization_id, sql, a.plant_id) == expected


@pytest.mark.asyncio
async def test_family_admission_shape_and_uniqueness(app: Any, catalog: Catalog) -> None:
    a = catalog.a

    def admission(
        answers: dict[str, Any], result: str, failed: str | None
    ) -> tuple[str, list[Any]]:
        return (
            "INSERT INTO catalog.family_admission (admission_id, organization_id, plant_id,"
            " family, answers, result, failed_criterion, evaluated_by, role_in_use, evaluated_at,"
            " ledger_record_id) VALUES (gen_random_uuid(), $1, $2, 'guard_bypass', $3, $4, $5,"
            " $6, 'administrator', $7, gen_random_uuid())",
            [
                a.organization_id,
                a.plant_id,
                json.dumps(answers),
                result,
                failed,
                a.user_id,
                BASE_TIME,
            ],
        )

    yes = {"standard": True, "remedy": True, "subject": True}
    no_subject = {"standard": True, "remedy": True, "subject": False}
    organization_id = a.organization_id
    assert await _steps(app, organization_id, admission(yes, "admitted", None)) == "ok"
    assert await _steps(app, organization_id, admission(no_subject, "rejected", "subject")) == "ok"
    for answers, result, failed in (
        (yes, "rejected", "standard"),  # criterio fallido con respuesta afirmativa
        (no_subject, "admitted", None),  # admitida sin las tres
        (no_subject, "rejected", None),  # rechazada sin criterio
        (no_subject, "rejected", "remedy"),  # el criterio no es la respuesta negativa
        (yes, "admitted", "standard"),  # admitida con criterio
        ({**yes, "extra": True}, "admitted", None),  # clave desconocida
        ({"standard": True, "remedy": True}, "admitted", None),  # falta una respuesta
        ({**yes, "subject": "true"}, "admitted", None),  # texto en vez de booleano
    ):
        assert (
            await _steps(app, organization_id, admission(answers, result, failed))
            == CHECK_VIOLATION
        ), (answers, result, failed)
    # Una familia se admite una sola vez por planta; los rechazos se repiten.
    twice = (admission(yes, "admitted", None), admission(yes, "admitted", None))
    assert await _steps(app, organization_id, *twice) == UNIQUE_VIOLATION
    rejected_twice = (admission(no_subject, "rejected", "subject"),) * 2
    assert await _steps(app, organization_id, *rejected_twice) == "ok"


@pytest.mark.asyncio
async def test_one_open_walk_test_session_per_zone(app: Any, catalog: Catalog) -> None:
    a = catalog.a
    zone = await _new_zone(catalog)
    scope = PlantScope(a.organization_id, a.plant_id, zone, a.node_id, a.user_id)
    catalog_version = (
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields, payload,"
        " envelope, single_occupancy, ledger_record_id) VALUES ($1, $2, $3, 1, $4, $5,"
        " 'administrator', 'Motivo sintético', ARRAY['cameras'], '{}', '{}', false,"
        " gen_random_uuid())",
        [a.organization_id, a.plant_id, zone, BASE_TIME, a.user_id],
    )
    organization_id = a.organization_id
    open_ = walk_test_session(scope)
    reopened = walk_test_session(scope, status="reopened")
    closed = walk_test_session(scope, status="closed")
    incomplete = walk_test_session(scope, status="incomplete")
    assert await _steps(app, organization_id, catalog_version, open_, closed, incomplete) == "ok"
    assert await _steps(app, organization_id, catalog_version, open_, reopened) == UNIQUE_VIOLATION
    assert (
        await _steps(app, organization_id, catalog_version, open_, walk_test_session(scope))
        == UNIQUE_VIOLATION
    )


@pytest.mark.asyncio
async def test_one_current_version_per_standard_in_one_zone(app: Any, catalog: Catalog) -> None:
    a = catalog.a
    other_zone = catalog.seed.plant(a.organization_id, 1)
    standard_id = uuid.uuid4()

    def version(
        scope: PlantScope, number: int, retired: int | None = None
    ) -> tuple[str, list[Any]]:
        return (
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, retired_in_catalog_version, reason_es)"
            " VALUES ($1, $2, $3, $4, $5, 'dwell', 'Título', 'Texto', '{}', $6, '{}', 1, $7,"
            " 'Motivo sintético')",
            [
                scope.organization_id,
                scope.plant_id,
                scope.zone_id,
                standard_id,
                number,
                BASE_TIME,
                retired,
            ],
        )

    retire = (
        "UPDATE catalog.declared_standard_version SET retired_in_catalog_version = 2"
        " WHERE standard_id = $1 AND version = 1",
        [standard_id],
    )
    organization_id = a.organization_id
    assert await _steps(app, organization_id, version(a, 1), retire, version(a, 2)) == "ok"
    assert await _steps(app, organization_id, version(a, 1), version(a, 2)) == UNIQUE_VIOLATION
    assert await _steps(app, organization_id, version(a, 1, 2), version(other_zone, 2)) == (
        EXCLUSION_VIOLATION
    )
    assert await _steps(app, organization_id, version(a, 1, 1)) == CHECK_VIOLATION


# --- NFR-GOB-16 -----


@pytest.mark.asyncio
async def test_gate_history_of_366_days_returns_intervals_from_24_months_ago(
    app: Any, catalog: Catalog
) -> None:
    """Intervalos contiguos de 30 días desde hace 30 meses (respecto de ``BASE_TIME``): la ventana
    de 366 días que empieza hace 24 meses devuelve exactamente los que la tocan, completos."""
    a = catalog.a
    zone = await _new_zone(catalog)
    start = BASE_TIME - dt.timedelta(days=30 * 30)
    edges = [start + dt.timedelta(days=30 * k) for k in range(31)]
    intervals = list(zip(edges, [*edges[1:-1], None], strict=False))
    statuses = ["approved", "revoked", "pending"]
    async with app.transaction():
        await set_scope(app, a.organization_id)
        for index, (low, high) in enumerate(intervals):
            sql, args = gate_interval(
                a, low, high, zone_id=zone, gate="usage", status=statuses[index % 3]
            )
            await app.execute(sql, *args)
    window_from = BASE_TIME - dt.timedelta(days=730)
    window_to = window_from + dt.timedelta(days=366)
    async with app.transaction():
        await set_scope(app, a.organization_id)
        rows = await app.fetch(
            "SELECT effective_from, effective_until, status FROM catalog.gate_state_history"
            " WHERE zone_id = $1 AND effective && tstzrange($2, $3, '[)')"
            " ORDER BY effective_from",
            zone,
            window_from,
            window_to,
        )
    expected = [
        (low, high, statuses[index % 3])
        for index, (low, high) in enumerate(intervals)
        if low < window_to and (high is None or high > window_from)
    ]
    assert [(row[0], row[1], row[2]) for row in rows] == expected
    assert expected[0][0] < window_from  # el primero empieza antes y vuelve completo
    assert len(expected) >= 12


# --- Garantías de una sola vez, concurrentes -----
#
# Cada garantía de «a lo sumo uno» del esquema, con dos transacciones a la vez: la primera escribe
# y sigue abierta, la segunda escribe y queda esperando (``pg_stat_activity``, sin topes de pared);
# al confirmar la primera, la segunda falla. La sonda ``test_once_guarantees_without_their_guard``
# repite los mismos escenarios en una base sin la guarda y comprueba que las dos confirman: la
# prueba concurrente detecta que se quite el mecanismo.

WAIT_FOR_LOCK_SECONDS = 30.0


@dataclass(frozen=True)
class Race:
    """Dos escrituras que compiten; ``loser`` es el SQLSTATE de la que llega segunda."""

    first: tuple[str, list[Any]]
    second: tuple[str, list[Any]]
    loser: str


async def _committed(connection: Any, organization_id: uuid.UUID, *steps: Any) -> None:
    async with connection.transaction():
        await set_scope(connection, organization_id)
        for sql, args in steps:
            await connection.execute(sql, *args)


def _family(scope: PlantScope, family: str) -> tuple[str, list[Any]]:
    return (
        "INSERT INTO catalog.family_admission (admission_id, organization_id, plant_id, family,"
        " answers, result, evaluated_by, role_in_use, evaluated_at, ledger_record_id)"
        ' VALUES (gen_random_uuid(), $1, $2, $3, \'{"standard": true, "remedy": true,'
        " \"subject\": true}', 'admitted', $4, 'administrator', $5, gen_random_uuid())",
        [scope.organization_id, scope.plant_id, family, scope.user_id, BASE_TIME],
    )


async def _race_scenarios(database: MigratedDatabase, a: PlantScope) -> dict[str, Race]:
    """Prepara (confirmado) lo que cada carrera necesita, en una zona y una planta nuevas de A."""
    superuser = await database.connect()
    try:
        zone_id, plant_id = uuid.uuid4(), uuid.uuid4()
        # Planta nueva: ninguna familia admitida todavía.
        await superuser.execute(
            "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
            " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
            " 'Planta sintética', 'CO', 'us-east-1', 'America/Bogota', $4, $5)",
            plant_id,
            a.organization_id,
            f"RP-{plant_id.hex[:8].upper()}",
            BASE_TIME,
            a.user_id,
        )
        await superuser.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            a.organization_id,
            a.plant_id,
            f"RC-{zone_id.hex[:8].upper()}",
            BASE_TIME,
            a.user_id,
        )
        zone = PlantScope(a.organization_id, a.plant_id, zone_id, a.node_id, a.user_id)
        new_plant = PlantScope(a.organization_id, plant_id, zone_id, a.node_id, a.user_id)
        session_id, test_id, standard_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        catalog_version = (
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, ledger_record_id) VALUES ($1, $2, $3, 1, $4,"
            " $5, 'administrator', 'Motivo sintético', ARRAY['cameras'], '{}', '{}', false,"
            " gen_random_uuid())",
            [a.organization_id, a.plant_id, zone_id, BASE_TIME, a.user_id],
        )
        session_zone = PlantScope(
            a.organization_id, a.plant_id, zone_id, a.node_id, a.user_id, session_id=session_id
        )
        await _committed(
            superuser,
            a.organization_id,
            catalog_version,
            walk_test_session(zone, session_id, status="closed"),
            occlusion_test(session_zone, test_id),
            gate_interval(zone, BASE_TIME, None),
        )
    finally:
        await superuser.close()

    def standard(version: int) -> tuple[str, list[Any]]:
        return (
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, reason_es) VALUES ($1, $2, $3, $4, $5, 'dwell',"
            " 'Título', 'Texto', '{}', $6, '{}', 1, 'Motivo sintético')",
            [a.organization_id, a.plant_id, zone_id, standard_id, version, BASE_TIME],
        )

    def record() -> tuple[str, list[Any]]:
        return (
            "INSERT INTO catalog.commissioning_record (commissioning_record_id, organization_id,"
            " plant_id, zone_id, session_id, catalog_version, matrix_results,"
            " false_negatives_total, false_alarm_rate_observed, false_alarm_threshold, latency,"
            " installer_measurements, cameras_measured, occlusion_summary, total_hours,"
            " steps_summary, signatures, closed_at, ledger_record_id) VALUES (gen_random_uuid(),"
            " $1, $2, $3, $4, 1, '[]', 0, 0, 0, '{}', '{}', '[]', '[]', 0, '[]', '[]', $5,"
            " gen_random_uuid())",
            [a.organization_id, a.plant_id, zone_id, session_id, BASE_TIME],
        )

    def close(hours: int) -> tuple[str, list[Any]]:
        return (
            "UPDATE catalog.zone_catalog_version SET superseded_at = $2"
            " WHERE zone_id = $1 AND catalog_version = 1",
            [zone_id, BASE_TIME + dt.timedelta(hours=hours)],
        )

    def until(hours: int) -> tuple[str, list[Any]]:
        return (
            "UPDATE catalog.gate_state_history SET effective_until = $2"
            " WHERE zone_id = $1 AND gate = 'mounting'",
            [zone_id, BASE_TIME + dt.timedelta(hours=hours)],
        )

    def resolve(verification: str) -> tuple[str, list[Any]]:
        reason = "'Declaración manual sintética'" if verification == "declared" else "NULL"
        return (
            f"UPDATE catalog.occlusion_test SET verification = '{verification}',"  # noqa: S608
            f" declared_reason_es = {reason}, ledger_record_id = gen_random_uuid()"
            " WHERE test_id = $1",
            [test_id],
        )

    return {
        "una familia admitida por planta": Race(
            _family(new_plant, "dwell"), _family(new_plant, "dwell"), UNIQUE_VIOLATION
        ),
        "una sesión abierta por zona": Race(
            walk_test_session(zone), walk_test_session(zone, status="reopened"), UNIQUE_VIOLATION
        ),
        "una versión vigente por estándar": Race(standard(1), standard(2), UNIQUE_VIOLATION),
        "un acta por sesión": Race(record(), record(), UNIQUE_VIOLATION),
        "un intervalo abierto por compuerta": Race(
            gate_interval(zone, BASE_TIME, None, gate="usage"),
            gate_interval(zone, BASE_TIME + dt.timedelta(hours=1), None, gate="usage"),
            EXCLUSION_VIOLATION,
        ),
        "superseded_at se cierra una vez": Race(close(1), close(2), RESTRICT_VIOLATION),
        "effective_until se cierra una vez": Race(until(1), until(2), RESTRICT_VIOLATION),
        "la oclusión sale de pending una vez": Race(
            resolve("failed"), resolve("declared"), RESTRICT_VIOLATION
        ),
    }


RACES = (
    "una familia admitida por planta",
    "una sesión abierta por zona",
    "una versión vigente por estándar",
    "un acta por sesión",
    "un intervalo abierto por compuerta",
    "superseded_at se cierra una vez",
    "effective_until se cierra una vez",
    "la oclusión sale de pending una vez",
)


async def _race(database: MigratedDatabase, organization_id: uuid.UUID, race: Race) -> list[str]:
    """La primera escribe y sigue abierta; la segunda espera; se confirma la primera."""
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
async def test_once_guarantees_hold_under_concurrency(name: str, catalog: Catalog) -> None:
    race = (await _race_scenarios(catalog.database, catalog.a))[name]
    assert await _race(catalog.database, catalog.a.organization_id, race) == ["ok", race.loser]


_GUARDS_REMOVED = (
    "DROP INDEX catalog.family_admission_admitted_once",
    "DROP INDEX catalog.walk_test_session_one_open_per_zone",
    "DROP INDEX catalog.declared_standard_version_one_current",
    "ALTER TABLE catalog.commissioning_record DROP CONSTRAINT commissioning_record_one_per_session",
    "ALTER TABLE catalog.gate_state_history DROP CONSTRAINT gate_state_no_overlap",
    "DROP TRIGGER append_only_update ON catalog.zone_catalog_version",
    "DROP TRIGGER append_only_update ON catalog.gate_state_history",
    "DROP TRIGGER append_only_update ON catalog.occlusion_test",
)


@pytest.fixture(scope="module")
def unguarded(postgres_endpoint: PostgresEndpoint) -> Iterator[Catalog]:
    """Otra base igual, sin las guardas de las carreras (sonda negativa)."""
    with seeded_identity(postgres_endpoint, "vigia_gob_0017_unguarded") as (database, seed):

        async def prepare() -> CatalogSeed:
            connection = await database.connect()
            try:
                catalog_seed = await seed_catalog(connection, seed)
                for statement in _GUARDS_REMOVED:
                    await connection.execute(statement)
                return catalog_seed
            finally:
                await connection.close()

        yield Catalog(database, asyncio.run(prepare()))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", RACES)
async def test_once_guarantees_without_their_guard(name: str, unguarded: Catalog) -> None:
    race = (await _race_scenarios(unguarded.database, unguarded.a))[name]
    assert await _race(unguarded.database, unguarded.a.organization_id, race) == ["ok", "ok"]
