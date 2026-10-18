"""Estructura esencial del esquema ``accreditation`` (TASK-338)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator

import pytest

from tests.identity_db import (
    MigratedDatabase,
    insert_concession,
    migrated_database,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

TABLES = {
    "export_request",
    "export_package",
    "monthly_package",
    "audit_sample",
    "audit_sample_item",
    "commitment",
    "program_metric_snapshot",
    "exposure_indicator",
    "adoption_counter",
}


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_laz_accreditation") as migrated:
        yield migrated


def test_all_accreditation_tables_have_forced_rls(database: MigratedDatabase) -> None:
    async def query() -> list[object]:
        connection = await database.connect()
        try:
            return await connection.fetch(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'accreditation' AND c.relkind = 'r'"
            )
        finally:
            await connection.close()

    rows = asyncio.run(query())
    assert {row["relname"] for row in rows} == TABLES
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows)


def test_all_accreditation_tables_have_restrictive_provider_policy(
    database: MigratedDatabase,
) -> None:
    async def query() -> list[object]:
        connection = await database.connect()
        try:
            return await connection.fetch(
                "SELECT tablename, policyname, permissive FROM pg_policies"
                " WHERE schemaname = 'accreditation'"
            )
        finally:
            await connection.close()

    policies: dict[str, dict[str, str]] = {}
    for row in asyncio.run(query()):
        policies.setdefault(str(row["tablename"]), {})[str(row["policyname"])] = str(
            row["permissive"]
        )
    assert set(policies) == TABLES
    for table in TABLES:
        assert policies[table] == {
            "organization_isolation": "PERMISSIVE",
            "provider_concession_scope": "RESTRICTIVE",
        }


def test_rls_never_exposes_another_organization(postgres_endpoint: PostgresEndpoint) -> None:
    """Sonda conductual mínima: el contador agregado sigue aislado por organización."""
    with seeded_identity(postgres_endpoint, "vigia_laz_accreditation_rls") as (database, identity):

        async def run() -> None:
            superuser = await database.connect()
            app = await database.connect("vigia_app")
            try:
                for tenant in (identity.a, identity.b):
                    plant = tenant.plants[0]
                    await superuser.execute(
                        "INSERT INTO accreditation.adoption_counter"
                        " (organization_id, plant_id, zone_id, day, activity_counts, session_count)"
                        " VALUES ($1, $2, $3, $4, $5, $6)",
                        tenant.organization_id,
                        plant.plant_id,
                        plant.zone_id,
                        dt.date(2026, 9, 1),
                        "{}",
                        0,
                    )
                async with app.transaction():
                    await set_scope(app, identity.a.organization_id)
                    assert (
                        await app.fetchval("SELECT count(*) FROM accreditation.adoption_counter")
                        == 1
                    )
                async with app.transaction():
                    await set_scope(app, identity.b.organization_id)
                    assert (
                        await app.fetchval("SELECT count(*) FROM accreditation.adoption_counter")
                        == 1
                    )
            finally:
                await app.close()
                await superuser.close()

        asyncio.run(run())


def test_provider_concession_only_sees_its_plant(postgres_endpoint: PostgresEndpoint) -> None:
    """La política RESTRICTIVE no permite cruzar a la otra planta concedida."""
    with seeded_identity(postgres_endpoint, "vigia_laz_accreditation_provider") as (
        database,
        identity,
    ):

        async def run() -> None:
            superuser = await database.connect()
            app = await database.connect("vigia_app")
            try:
                tenant = identity.a
                concession = await insert_concession(
                    superuser,
                    identity,
                    tenant.organization_id,
                    scope_level="plant",
                    scope_id=tenant.plants[0].plant_id,
                    status="active",
                )
                for plant in tenant.plants:
                    await superuser.execute(
                        "INSERT INTO accreditation.adoption_counter"
                        " (organization_id, plant_id, zone_id, day, activity_counts, session_count)"
                        " VALUES ($1, $2, $3, $4, $5, $6)",
                        tenant.organization_id,
                        plant.plant_id,
                        plant.zone_id,
                        dt.date(2026, 9, 1),
                        "{}",
                        0,
                    )
                async with app.transaction():
                    await set_scope(
                        app,
                        tenant.organization_id,
                        actor_kind="provider_user",
                        concession_id=concession,
                    )
                    assert (
                        await app.fetchval("SELECT count(*) FROM accreditation.adoption_counter")
                        == 1
                    )
            finally:
                await app.close()
                await superuser.close()

        asyncio.run(run())


def test_removing_organization_policy_is_detected(postgres_endpoint: PostgresEndpoint) -> None:
    """Sonda negativa: sin la política, la comprobación de aislamiento caería en rojo."""
    with seeded_identity(postgres_endpoint, "vigia_laz_accreditation_mutation") as (
        database,
        identity,
    ):

        async def run() -> None:
            connection = await database.connect()
            try:
                for tenant in (identity.a, identity.b):
                    plant = tenant.plants[0]
                    await connection.execute(
                        "INSERT INTO accreditation.adoption_counter"
                        " (organization_id, plant_id, zone_id, day, activity_counts, session_count)"
                        " VALUES ($1, $2, $3, $4, $5, $6)",
                        tenant.organization_id,
                        plant.plant_id,
                        plant.zone_id,
                        dt.date(2026, 9, 1),
                        "{}",
                        0,
                    )
                async with connection.transaction():
                    await connection.execute(
                        "DROP POLICY organization_isolation ON accreditation.adoption_counter"
                    )
                    await connection.execute("SET LOCAL ROLE vigia_app")
                    await set_scope(connection, identity.a.organization_id)
                    # Sin la única política PERMISSIVE no hay fila propia visible: la prueba
                    # normal (que exige exactamente una) detecta la mutación.
                    assert (
                        await connection.fetchval(
                            "SELECT count(*) FROM accreditation.adoption_counter"
                        )
                        == 0
                    )
            finally:
                await connection.close()

        asyncio.run(run())


def test_permissive_provider_policy_is_detected(postgres_endpoint: PostgresEndpoint) -> None:
    """Sonda negativa: la política de proveedor debe permanecer RESTRICTIVE."""
    with seeded_identity(postgres_endpoint, "vigia_laz_accreditation_permissive") as (
        database,
        identity,
    ):

        async def run() -> None:
            connection = await database.connect()
            try:
                tenant = identity.a
                concession = await insert_concession(
                    connection,
                    identity,
                    tenant.organization_id,
                    scope_level="plant",
                    scope_id=tenant.plants[0].plant_id,
                    status="active",
                )
                for plant in tenant.plants:
                    await connection.execute(
                        "INSERT INTO accreditation.adoption_counter"
                        " (organization_id, plant_id, zone_id, day, activity_counts, session_count)"
                        " VALUES ($1, $2, $3, $4, $5, $6)",
                        tenant.organization_id,
                        plant.plant_id,
                        plant.zone_id,
                        dt.date(2026, 9, 1),
                        "{}",
                        0,
                    )
                async with connection.transaction():
                    await connection.execute(
                        "DROP POLICY provider_concession_scope ON accreditation.adoption_counter"
                    )
                    await connection.execute(
                        "CREATE POLICY provider_concession_scope ON accreditation.adoption_counter"
                        " AS PERMISSIVE FOR ALL TO PUBLIC"
                        " USING (identity.rls_provider_scope_allows(organization_id, plant_id))"
                        " WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))"
                    )
                    await connection.execute("SET LOCAL ROLE vigia_app")
                    await set_scope(
                        connection,
                        tenant.organization_id,
                        actor_kind="provider_user",
                        concession_id=concession,
                    )
                    assert (
                        await connection.fetchval(
                            "SELECT count(*) FROM accreditation.adoption_counter"
                        )
                        == 2
                    )
            finally:
                await connection.close()

        asyncio.run(run())


def test_adoption_counter_has_no_user_column(database: MigratedDatabase) -> None:
    async def query() -> set[str]:
        connection = await database.connect()
        try:
            rows = await connection.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'accreditation' AND table_name = 'adoption_counter'"
            )
            return {str(row["column_name"]) for row in rows}
        finally:
            await connection.close()

    assert asyncio.run(query()) == {
        "organization_id",
        "plant_id",
        "zone_id",
        "day",
        "activity_counts",
        "session_count",
        "last_activity_at",
    }


@pytest.mark.asyncio
async def test_period_idempotency_keys_are_unique(database: MigratedDatabase) -> None:
    """Los dos sellos por zona y periodo no admiten una segunda materialización."""
    connection = await database.connect()
    organization, plant, zone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        for table, identifier, values in (
            (
                "monthly_package",
                "package_id",
                "state, generated_at, findings_summary, recurrence_summary, coverage_summary",
            ),
            (
                "program_metric_snapshot",
                "snapshot_id",
                "granularity, values, materialized_at, inputs_digest",
            ),
        ):
            common = [uuid.uuid4(), organization, plant, zone, dt.date(2026, 9, 1)]
            if table == "monthly_package":
                tail = ["sealed", dt.datetime(2026, 10, 1, tzinfo=dt.UTC), "{}", "{}", "{}"]
            else:
                tail = ["month", "[]", dt.datetime(2026, 10, 1, tzinfo=dt.UTC), "0" * 64]
            columns = f"{identifier}, organization_id, plant_id, zone_id, period, {values}"
            placeholders = ", ".join(f"${number}" for number in range(1, len(common + tail) + 1))
            await connection.execute(
                f"INSERT INTO accreditation.{table} ({columns}) VALUES ({placeholders})",  # noqa: S608
                *(common + tail),
            )
            with pytest.raises(Exception) as error:
                await connection.execute(
                    f"INSERT INTO accreditation.{table} ({columns}) VALUES ({placeholders})",  # noqa: S608
                    *([uuid.uuid4(), *common[1:], *tail]),
                )
            assert getattr(error.value, "sqlstate", None) == "23505"
    finally:
        await connection.close()
