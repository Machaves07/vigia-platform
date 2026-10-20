"""Metapropiedad de retención de U-04 (TASK-301; NFR-LAZ-08 y 34; PAT-LAZ-ESC-04).

Toda tabla de los esquemas de U-04 (``loop``, ``notifications`` y ``accreditation``) declara
**exactamente una** de las tres
categorías (``ledger_projection``, ``operational_state``, ``operational_state_with_retention``)
en su ``COMMENT ON TABLE`` con la marca ``retention_category=…``
(``migrations/laz_policies.py``):

- la comprobación pura (``retention_violations``) detecta una tabla sin categoría, con dos o con
  una desconocida;
- la migración ``laz_0029`` clasifica sus 19 tablas, una categoría por tabla, y ``laz_0030`` sus
  7 con el plazo de la nota de ``domain-entities.md`` §6.2 (24 meses, 90 días del cuerpo del
  correo, 12 meses y sin caducidad) y ``laz_0031`` sus 9 (la solicitud de exportación 24 meses,
  la adopción sin caducidad y el resto proyección, DE §9);
- contra la base migrada (``information_schema`` de los tres esquemas), ninguna tabla queda sin
  categoría o con dos; quitar la marca o duplicarla en un comentario la hace fallar.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from migrations.laz_policies import (
    RETENTION_CATEGORIES,
    U04_SCHEMAS,
    retention_tag,
    retention_violations,
)
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint

BACKEND: Final = Path(__file__).resolve().parents[2]


def _load(name: str) -> Any:
    path = BACKEND / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION: Final = _load("laz_0029_loop_schema.py")
NOTIFICATIONS: Final = _load("laz_0030_notifications_schema.py")
ACCREDITATION: Final = _load("laz_0031_accreditation_schema.py")


# --- La comprobación pura -----


def test_one_known_category_per_table_is_accepted() -> None:
    comments = {
        f"loop.t{index}": f"Entidad; {retention_tag(category)}"
        for index, category in enumerate(RETENTION_CATEGORIES)
    }
    assert retention_violations(comments) == []


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        (None, "loop.x: sin categoría de retención"),
        ("", "loop.x: sin categoría de retención"),
        ("Entidad sin marca", "loop.x: sin categoría de retención"),
        (
            "retention_category=ledger_projection; retention_category=operational_state",
            "loop.x: 2 categorías de retención (ledger_projection, operational_state)",
        ),
        (
            "retention_category=ledger_projection; retention_category=ledger_projection",
            "loop.x: 2 categorías de retención (ledger_projection, ledger_projection)",
        ),
        ("retention_category=forever", "loop.x: categoría de retención desconocida (forever)"),
    ],
)
def test_a_table_without_one_known_category_is_a_violation(
    comment: str | None, expected: str
) -> None:
    assert retention_violations({"loop.x": comment}) == [expected]


def test_retention_tag_rejects_an_unknown_category() -> None:
    with pytest.raises(ValueError, match="desconocida"):
        retention_tag("forever")


# --- La declaración de laz_0029 -----


def test_the_migration_classifies_each_loop_table_once() -> None:
    assert set(MIGRATION.RETENTION) == set(MIGRATION.TABLES)
    assert set(MIGRATION.RETENTION.values()) <= set(RETENTION_CATEGORIES)
    # domain-entities §9 y NFR-LAZ-08: el asistente es estado operativo con 24 meses; el control
    # de la proyección, estado operativo sin retención; el resto, proyección del expediente.
    assert MIGRATION.RETENTION["assistant_request"] == "operational_state_with_retention"
    assert MIGRATION.RETENTION["assistant_draft"] == "operational_state_with_retention"
    assert MIGRATION.RETENTION["loop_projection_checkpoint"] == "operational_state"
    assert MIGRATION.RETENTION["closure_attachment"] == "ledger_projection"
    assert MIGRATION.RETENTION["finding_state"] == "ledger_projection"


# --- La declaración de laz_0030 -----


def test_the_migration_classifies_each_notifications_table_once() -> None:
    assert set(NOTIFICATIONS.RETENTION) == set(NOTIFICATIONS.TABLES)
    assert set(NOTIFICATIONS.RETENTION_PERIODS) == set(NOTIFICATIONS.TABLES)
    # domain-entities §6.2 (nota de 2026-09-20) y §9: estado operativo; solo la preferencia, que
    # es configuración vigente, no caduca. Ninguna tabla de notifications es proyección.
    assert NOTIFICATIONS.RETENTION == {
        "notification": "operational_state_with_retention",
        "notification_delivery": "operational_state_with_retention",
        "digest_batch": "operational_state_with_retention",
        "user_notification_preference": "operational_state",
        "email_message": "operational_state_with_retention",
        "notification_source_key": "operational_state_with_retention",
        "email_provider_message_key": "operational_state_with_retention",
    }
    periods = NOTIFICATIONS.RETENTION_PERIODS
    assert periods["notification"].startswith("24 meses")
    assert periods["notification_delivery"].startswith("24 meses")
    assert periods["digest_batch"] == "12 meses"
    assert periods["user_notification_preference"].startswith("sin caducidad")
    assert "metadatos 24 meses" in periods["email_message"]
    assert "90 días" in periods["email_message"]


# --- La declaración de laz_0031 -----


def test_the_migration_classifies_each_accreditation_table_once() -> None:
    assert set(ACCREDITATION.RETENTION) == set(ACCREDITATION.TABLES)
    assert set(ACCREDITATION.RETENTION_PERIODS) == set(ACCREDITATION.TABLES)
    # domain-entities §9: la solicitud de exportación (24 meses) y el contador de adopción (sin
    # caducidad) son estado operativo; el resto, proyección del expediente.
    assert ACCREDITATION.RETENTION == {
        "export_request": "operational_state_with_retention",
        "export_package": "ledger_projection",
        "monthly_package": "ledger_projection",
        "audit_sample": "ledger_projection",
        "audit_sample_item": "ledger_projection",
        "commitment": "ledger_projection",
        "program_metric_snapshot": "ledger_projection",
        "exposure_indicator": "ledger_projection",
        "adoption_counter": "operational_state",
    }
    periods = ACCREDITATION.RETENTION_PERIODS
    assert periods["export_request"].startswith("24 meses")
    assert "el paquete producido no se borra" in periods["export_request"]
    assert periods["adoption_counter"] == "sin caducidad"


# --- Contra la base migrada -----


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_laz_retention") as migrated:
        yield migrated


_COMMENTS: Final = (
    "SELECT t.table_schema || '.' || t.table_name AS name,"
    " obj_description(format('%I.%I', t.table_schema, t.table_name)::regclass, 'pg_class')"
    " AS comment"
    " FROM information_schema.tables AS t"
    " JOIN pg_catalog.pg_class AS c"
    " ON c.oid = format('%I.%I', t.table_schema, t.table_name)::regclass"
    " WHERE t.table_schema = ANY ($1::text[]) AND t.table_type = 'BASE TABLE'"
    " AND NOT c.relispartition"
)
"""Cada tabla (no partición) de los esquemas de U-04 y su comentario."""


class _Rollback(Exception):
    """Revierte la transacción de la sonda."""


async def _comments(connection: Any) -> dict[str, str | None]:
    rows = await connection.fetch(_COMMENTS, list(U04_SCHEMAS))
    return {str(row["name"]): row["comment"] for row in rows}


@pytest.mark.integration
def test_every_u04_table_declares_exactly_one_category(database: MigratedDatabase) -> None:
    async def run() -> dict[str, str | None]:
        connection = await database.connect()
        try:
            return await _comments(connection)
        finally:
            await connection.close()

    comments = asyncio.run(run())
    # No pasa por un esquema vacío: las 19 de loop, las 7 de notifications y las 9 de
    # accreditation están, también las particionadas.
    # episode_rebuild_shadow, finding_state_rebuild_shadow y loop_projection_sample_run
    # (laz_0037, TASK-306) no son de laz_0029: una migración aditiva solo añade (BR-CTR-23),
    # igual que monthly_package_lease sobre laz_0031 (más abajo).
    assert {name for name in comments if name.startswith("loop.")} == {
        f"loop.{table}" for table in MIGRATION.TABLES
    } | {
        "loop.episode_rebuild_shadow",
        "loop.finding_state_rebuild_shadow",
        "loop.loop_projection_sample_run",
    }
    assert {name for name in comments if name.startswith("notifications.")} == {
        f"notifications.{table}" for table in NOTIFICATIONS.TABLES
    }
    # monthly_package_lease (laz_0035, VIG-239) y adoption_alert (laz_0038, TASK-323) no son de
    # laz_0031: una migración aditiva solo añade, nunca quita de lo que laz_0031 ya dejó
    # (BR-CTR-23).
    assert {name for name in comments if name.startswith("accreditation.")} == {
        f"accreditation.{table}" for table in ACCREDITATION.TABLES
    } | {"accreditation.monthly_package_lease", "accreditation.adoption_alert"}
    assert retention_violations(comments) == []
    # La categoría de la base es la que declara la migración, con su plazo.
    for schema, migration in (("notifications", NOTIFICATIONS), ("accreditation", ACCREDITATION)):
        for table, category in migration.RETENTION.items():
            comment = comments[f"{schema}.{table}"] or ""
            assert retention_tag(category) in comment, table
            assert migration.RETENTION_PERIODS[table] in comment, table


@pytest.mark.integration
@pytest.mark.parametrize(
    "table", ["loop.finding_state", "notifications.email_message", "accreditation.export_request"]
)
@pytest.mark.parametrize(
    ("comment", "fragment"),
    [
        ("Entidad sin categoría", "sin categoría"),
        (
            "Entidad; retention_category=ledger_projection;"
            " retention_category=operational_state_with_retention",
            "2 categorías",
        ),
    ],
)
def test_a_table_comment_without_one_category_is_detected(
    table: str, comment: str, fragment: str, database: MigratedDatabase
) -> None:
    async def run() -> list[str]:
        connection = await database.connect()
        violations: list[str] = []
        try:
            async with connection.transaction():
                await connection.execute(f"COMMENT ON TABLE {table} IS '{comment}'")
                violations = retention_violations(await _comments(connection))
                raise _Rollback
        except _Rollback:
            pass
        finally:
            await connection.close()
        return violations

    violations = asyncio.run(run())
    assert len(violations) == 1
    assert violations[0].startswith(f"{table}:") and fragment in violations[0]
