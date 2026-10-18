"""Estructura esencial del esquema ``accreditation`` (TASK-338)."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator

import pytest

from tests.identity_db import MigratedDatabase, migrated_database
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
