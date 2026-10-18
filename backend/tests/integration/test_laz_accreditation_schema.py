"""Estructura esencial del esquema ``accreditation`` (TASK-338)."""

from __future__ import annotations

import asyncio
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
