"""Esquema ``notifications`` de ``laz_0030`` (TASK-339, LC-LAZ-02; NFR-LAZ-07 a 09;
PAT-LAZ-ESC-02; BR-LAZ-126 y 129).

Base migrada propia del módulo (``alembic upgrade head`` como proceso aparte) con los datos de
``tests/identity_db.py`` y una fila de cada tabla de ``notifications`` en cada planta
(``tests/notifications_db.py``). Criterios de TASK-339:

- ``alembic upgrade head`` aplica ``laz_0030``: esquema, las cinco entidades y sus dos claves de
  idempotencia con RLS forzada y sus políticas (organización en todas; proveedor por planta en el
  aviso, la entrega y su clave; ninguna fila bajo concesión en las de A-69), permisos sin
  ``DELETE`` ni ``TRUNCATE`` y ``UPDATE`` solo en las columnas que el diseño deja cambiar; las
  definiciones de SQLAlchemy Core coinciden con la base.
- ``user_notification_preference`` tiene exactamente las columnas del diseño: ninguna silencia
  ``immediate``.
- La unicidad de ``(organization_id, source_event_id, recipient_user_id)`` del aviso y de
  ``provider_message_id`` del correo, también entre particiones y bajo concurrencia (y cae sin su
  disparador). Un evento avisa a varios destinatarios.
- Índices de la lista del usuario, del límite exacto de reenvío y del lote.

Cada mutación corre en una transacción que se revierte siempre.
"""

from __future__ import annotations

import asyncio
import dataclasses
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

from tests.identity_db import MigratedDatabase, _insert_user, seeded_identity, set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_REVISION, HEAD_SCHEMA_VERSION
from tests.notifications_db import (
    ENTITY_TABLES,
    KEY_TABLES,
    NOTIFICATIONS_TABLES,
    ORGANIZATION_ONLY_TABLES,
    PLANT_TABLES,
    NotificationScope,
    NotificationsSeed,
    digest_batch,
    email_message,
    insert,
    notification,
    notification_delivery,
    seed_notifications,
    user_notification_preference,
)
from vigia_platform.notifications.adapters.postgres import tables as notifications_tables

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]
INSUFFICIENT_PRIVILEGE: Final = "42501"
CHECK_VIOLATION: Final = "23514"
UNIQUE_VIOLATION: Final = "23505"
FOREIGN_KEY_VIOLATION: Final = "23503"
WAIT_FOR_LOCK_SECONDS: Final = 30.0

PREFERENCE_COLUMNS: Final = frozenset(
    {
        # domain-entities §6.2
        "user_id",
        "organization_id",
        "email_enabled_normal",
        "email_enabled_digest",
        "weekly_digest_weekday",
        "updated_at",
        # nota fechada de 2026-09-21 (rebote duro y queja)
        "email_suppressed_hard_bounce",
        "email_suppressed_complaint",
        "email_suppressed_at",
    }
)
"""Las columnas de ``UserNotificationPreference``, literales del diseño (BR-LAZ-126)."""


def _load(name: str) -> Any:
    path = BACKEND / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load("laz_0030_notifications_schema.py")


@dataclass(frozen=True)
class Notifications:
    database: MigratedDatabase
    seed: NotificationsSeed
    month: dt.date
    """Mes en curso de la base (UTC): el de las particiones que dejó la migración."""

    @property
    def a(self) -> NotificationScope:
        return self.seed.plant(self.seed.identity.a.organization_id)

    @property
    def b(self) -> NotificationScope:
        return self.seed.plant(self.seed.identity.b.organization_id)

    @property
    def now(self) -> dt.datetime:
        """Un instante del mes en curso de la base, para caer en una partición mensual."""
        return dt.datetime(self.month.year, self.month.month, 2, 12, 0, tzinfo=dt.UTC)


def _prepare(database: MigratedDatabase, identity: Any) -> Notifications:
    async def run() -> Notifications:
        connection = await database.connect()
        try:
            seed = await seed_notifications(connection, identity)
            month = await connection.fetchval(
                "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"
            )
        finally:
            await connection.close()
        return Notifications(database, seed, month)

    return asyncio.run(run())


@pytest.fixture(scope="module")
def notifications(postgres_endpoint: PostgresEndpoint) -> Iterator[Notifications]:
    with seeded_identity(postgres_endpoint, "vigia_laz_0030") as (database, identity):
        yield _prepare(database, identity)


@pytest_asyncio.fixture
async def superuser(notifications: Notifications) -> AsyncIterator[Any]:
    connection = await notifications.database.connect()
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def app(notifications: Notifications) -> AsyncIterator[Any]:
    connection = await notifications.database.connect("vigia_app")
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


def _columns(sql: str) -> list[str]:
    return sql[sql.index("(") + 1 : sql.index(")")].split(", ")


def _with(statement: tuple[str, list[Any]], **changes: Any) -> tuple[str, list[Any]]:
    """La misma fila con ``changes`` aplicados."""
    sql, args = statement
    table = sql.split()[2].split(".")[1]
    return insert(table, dict(zip(_columns(sql), args, strict=True)) | changes)


# --- Criterio 1: alembic upgrade head aplica laz_0030 -----


@pytest.mark.asyncio
async def test_upgrade_head_applies_laz_0030(superuser: Any) -> None:
    assert HEAD_SCHEMA_VERSION >= 30
    assert await superuser.fetchval("SELECT version_num FROM public.alembic_version") == (
        HEAD_REVISION
    )
    assert await superuser.fetchval("SELECT shared.vigia_schema_version()") == HEAD_SCHEMA_VERSION
    assert (
        await superuser.fetchval(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'notifications'"
        )
        == "vigia_migrate"
    )
    rows = await superuser.fetch(
        "SELECT c.relname, c.relkind::text AS kind, c.relrowsecurity, c.relforcerowsecurity,"
        " pg_get_userbyid(c.relowner) AS owner FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'notifications' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
    )
    assert {row["relname"] for row in rows} == set(NOTIFICATIONS_TABLES) == set(MIGRATION.TABLES)
    # Las cinco entidades de §6.2 y sus dos claves de idempotencia.
    assert (
        set(ENTITY_TABLES)
        == set(MIGRATION.ENTITY_TABLES)
        == {
            "notification",
            "notification_delivery",
            "digest_batch",
            "user_notification_preference",
            "email_message",
        }
    )
    assert set(KEY_TABLES) == set(MIGRATION.KEY_TABLES)
    assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows), rows
    assert {row["owner"] for row in rows} == {"vigia_migrate"}
    partitioned = {row["relname"] for row in rows if row["kind"] == "p"}
    assert partitioned == {"notification", "notification_delivery", "email_message"}

    policies = await superuser.fetch(
        "SELECT tablename, policyname, permissive, cmd, qual, with_check FROM pg_policies"
        " WHERE schemaname = 'notifications'"
    )
    by_table: dict[str, dict[str, Any]] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], {})[policy["policyname"]] = policy
    assert set(by_table) == set(NOTIFICATIONS_TABLES)
    assert set(ORGANIZATION_ONLY_TABLES) == set(MIGRATION.ORGANIZATION_ONLY_TABLES)
    assert set(PLANT_TABLES) == set(MIGRATION.PLANT_TABLES)
    for table, named in by_table.items():
        expected = {"organization_isolation"}
        if table in PLANT_TABLES:
            expected.add("provider_concession_scope")
        if table in ORGANIZATION_ONLY_TABLES:
            expected.add("provider_context_denied")
        if table == "email_provider_message_key":
            # laz_0036 (TASK-325): igual que identity.invitation_lookup (nuc_0011), solo dentro
            # de notifications.email_provider_message_lookup.
            expected.add("email_provider_message_lookup")
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
        if "email_provider_message_lookup" in named:
            lookup = named["email_provider_message_lookup"]
            assert (lookup["permissive"], lookup["cmd"]) == ("PERMISSIVE", "SELECT"), table
            assert "vigia.email_provider_message_lookup" in lookup["qual"], table
            assert lookup["with_check"] is None, table

    # Ningún DELETE ni TRUNCATE; SELECT e INSERT en las entidades; las claves, solo SELECT.
    for table in NOTIFICATIONS_TABLES:
        privileges = {
            privilege: await superuser.fetchval(
                "SELECT has_table_privilege('vigia_app', $1, $2)",
                f"notifications.{table}",
                privilege,
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "TRUNCATE")
        }
        assert privileges == {
            "SELECT": True,
            "INSERT": table in ENTITY_TABLES,
            "DELETE": False,
            "TRUNCATE": False,
        }, table
    # Las particiones no se leen directamente: no tienen RLS propia y vigia_app no tiene nada.
    children = await superuser.fetch(
        "SELECT c.oid::regclass::text AS name FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'notifications' AND c.relispartition AND c.relkind = 'r'"
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
        " WHERE table_schema = 'notifications' AND grantee = 'vigia_app'"
        " AND privilege_type = 'UPDATE'"
    )
    granted: dict[str, set[str]] = {}
    for row in rows:
        granted.setdefault(row["table_name"], set()).add(row["column_name"])
    assert granted == {
        "notification": {"read_at", "archived_at"},
        "notification_delivery": {
            "status",
            "attempts",
            "scheduled_for",
            "sent_at",
            "failure_code",
            "digest_batch_id",
            "provider_message_id",
        },
        "digest_batch": {"sent_at", "status"},
        "user_notification_preference": PREFERENCE_COLUMNS - {"user_id", "organization_id"},
        "email_message": {
            "sent_at",
            "status",
            "failure_code",
            "provider_message_id",
            "bounce_kind",
            "complaint_at",
        },
    }
    assert granted == {
        table: set(MIGRATION.app_updatable_columns(table))
        for table in NOTIFICATIONS_TABLES
        if MIGRATION.app_updatable_columns(table)
    }


@pytest.mark.asyncio
async def test_core_tables_match_the_database(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT c.relname AS table_name, a.attname::text AS column_name, NOT a.attnotnull AS"
        " nullable FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'notifications' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
        " AND a.attnum > 0 AND NOT a.attisdropped"
    )
    database: dict[str, dict[str, bool]] = {}
    for row in rows:
        database.setdefault(row["table_name"], {})[row["column_name"]] = row["nullable"]
    core = {
        table.name: {column.name: bool(column.nullable) for column in table.columns}
        for table in notifications_tables.METADATA.tables.values()
    }
    assert core == database
    primary_keys = {
        row["table_name"]: set(row["columns"])
        for row in await superuser.fetch(
            "SELECT c.relname AS table_name, array_agg(a.attname::text) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)"
            " WHERE n.nspname = 'notifications' AND i.indisprimary AND NOT c.relispartition"
            " GROUP BY c.relname"
        )
    }
    assert primary_keys == {
        table.name: {column.name for column in table.primary_key.columns}
        for table in notifications_tables.METADATA.tables.values()
    }


# --- Criterio 2: ninguna columna silencia immediate (BR-LAZ-126) -----


@pytest.mark.asyncio
async def test_the_preference_has_exactly_the_design_columns(superuser: Any) -> None:
    columns = {
        row["column_name"]
        for row in await superuser.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'notifications' AND table_name = 'user_notification_preference'"
        )
    }
    assert columns == PREFERENCE_COLUMNS
    assert not [column for column in columns if "immediate" in column]
    # Las únicas que apagan el correo nombran el nivel normal o digest, o son las supresiones del
    # proveedor de correo, que solo afectan a esos dos niveles (nota de 2026-09-21).
    switches = {column for column in columns if column.startswith("email_")}
    assert switches == {
        "email_enabled_normal",
        "email_enabled_digest",
        "email_suppressed_hard_bounce",
        "email_suppressed_complaint",
        "email_suppressed_at",
    }
    # Los valores por defecto del diseño: correo activado y sin supresión.
    defaults = {
        row["column_name"]: row["column_default"]
        for row in await superuser.fetch(
            "SELECT column_name, column_default FROM information_schema.columns"
            " WHERE table_schema = 'notifications' AND table_name = 'user_notification_preference'"
        )
    }
    assert defaults["email_enabled_normal"] == "true"
    assert defaults["email_enabled_digest"] == "true"
    assert defaults["email_suppressed_hard_bounce"] == "false"
    assert defaults["email_suppressed_complaint"] == "false"


@pytest.mark.asyncio
async def test_a_column_to_silence_immediate_would_be_detected(superuser: Any) -> None:
    """Sonda negativa: añadir ``email_enabled_immediate`` rompe la igualdad del conjunto."""
    try:
        async with superuser.transaction():
            await superuser.execute(
                "ALTER TABLE notifications.user_notification_preference"
                " ADD COLUMN email_enabled_immediate boolean NOT NULL DEFAULT true"
            )
            columns = {
                row["column_name"]
                for row in await superuser.fetch(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'notifications'"
                    " AND table_name = 'user_notification_preference'"
                )
            }
            raise _Rollback
    except _Rollback:
        pass
    assert columns != PREFERENCE_COLUMNS
    assert [column for column in columns if "immediate" in column] == ["email_enabled_immediate"]


# --- Índices (PAT-LAZ-ESC-02, NFR-LAZ-09) -----


@pytest.mark.asyncio
async def test_the_list_resend_and_batch_indexes(superuser: Any) -> None:
    rows = await superuser.fetch(
        "SELECT i.relname AS index_name,"
        " (SELECT array_agg(a.attname::text ORDER BY k.ordinality)"
        "  FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ordinality)"
        "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum) AS columns"
        " FROM pg_index x JOIN pg_class c ON c.oid = x.indrelid"
        " JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'notifications' AND NOT c.relispartition"
    )
    by_name = {row["index_name"]: row["columns"] for row in rows}
    assert by_name["notification_recipient_list"] == [
        "organization_id",
        "recipient_user_id",
        "created_at",
        "notification_id",
    ]
    assert by_name["email_message_resend_rate"] == ["organization_id", "to_user_id", "queued_at"]
    assert by_name["notification_delivery_digest"] == ["digest_batch_id"]
    definitions = {
        row["name"]: row["definition"]
        for row in await superuser.fetch(
            "SELECT indexname AS name, indexdef AS definition FROM pg_indexes"
            " WHERE schemaname = 'notifications'"
        )
    }
    assert "created_at DESC" in definitions["notification_recipient_list"]
    assert definitions["notification_recipient_list"].endswith("WHERE (archived_at IS NULL)")
    # Cada partición del mes hereda los índices de su tabla padre.
    for parent in ("notification_recipient_list", "email_message_resend_rate"):
        attached = await superuser.fetchval(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = $1::regclass",
            f"notifications.{parent}",
        )
        assert attached == 1 + 1 + MIGRATION.PARTITION_MONTHS_AHEAD, parent


# --- Criterio 5: unicidad del aviso y del correo -----


@pytest.mark.asyncio
async def test_a_second_notification_for_the_same_event_and_recipient_is_rejected(
    app: Any, notifications: Notifications
) -> None:
    scope = notifications.a
    event = uuid.uuid4()
    first = notification(scope, notifications.now, source_event_id=event)
    # Otro instante y otra partición (la por defecto): la clave no depende de la partición.
    again = notification(scope, dt.datetime(2026, 9, 3, tzinfo=dt.UTC), source_event_id=event)
    assert await _steps(app, scope.organization_id, first, again) == UNIQUE_VIOLATION
    # El mismo evento avisa a otro destinatario de la organización (BR-LAZ-124).
    other_user = await _other_user(notifications)
    other = notification(
        scope, notifications.now, source_event_id=event, recipient_user_id=other_user
    )
    assert await _steps(app, scope.organization_id, first, other) == "ok"
    # Y el mismo identificador de evento en otra organización es otro evento.
    elsewhere = notification(notifications.b, notifications.now, source_event_id=event)
    assert await _steps(app, scope.organization_id, first) == "ok"
    assert await _steps(app, notifications.b.organization_id, elsewhere) == "ok"


async def _other_user(notifications: Notifications) -> uuid.UUID:
    """Otro usuario de la organización A (para un segundo destinatario)."""
    connection = await notifications.database.connect()
    try:
        return await _insert_user(connection, notifications.a.organization_id, uuid.uuid4().hex)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_the_source_key_follows_the_notification(
    app: Any, notifications: Notifications
) -> None:
    """El disparador deja la clave con la planta y la marca del aviso; vigia_app no la escribe."""
    scope = notifications.a
    event = uuid.uuid4()
    notification_id = uuid.uuid4()
    key: dict[str, Any] = {}
    try:
        async with app.transaction():
            await set_scope(app, scope.organization_id)
            sql, args = notification(
                scope, notifications.now, notification_id=notification_id, source_event_id=event
            )
            await app.execute(sql, *args)
            row = await app.fetchrow(
                "SELECT plant_id, notification_id, created_at"
                " FROM notifications.notification_source_key"
                " WHERE organization_id = $1 AND source_event_id = $2 AND recipient_user_id = $3",
                scope.organization_id,
                event,
                scope.user_id,
            )
            key = dict(row)
            for statement in (
                "DELETE FROM notifications.notification_source_key WHERE source_event_id = $1",
                "UPDATE notifications.notification_source_key SET plant_id = NULL"
                " WHERE source_event_id = $1",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with app.transaction():
                        await app.execute(statement, event)
            raise _Rollback
    except _Rollback:
        pass
    assert key == {
        "plant_id": scope.plant_id,
        "notification_id": notification_id,
        "created_at": notifications.now,
    }


@pytest.mark.asyncio
async def test_a_second_email_with_the_same_provider_message_id_is_rejected(
    app: Any, notifications: Notifications
) -> None:
    scope = notifications.a
    provider_id = f"proveedor-{uuid.uuid4().hex}"
    first = email_message(scope, notifications.now, provider_message_id=provider_id)
    # En otra partición y en otra organización: el identificador del proveedor es global.
    again = email_message(
        scope, dt.datetime(2026, 9, 3, tzinfo=dt.UTC), provider_message_id=provider_id
    )
    assert await _steps(app, scope.organization_id, first, again) == UNIQUE_VIOLATION
    elsewhere = email_message(notifications.b, notifications.now, provider_message_id=provider_id)
    switch = (
        "SELECT set_config('vigia.organization_id', $1, true)",
        [str(notifications.b.organization_id)],
    )
    assert await _steps(app, scope.organization_id, first, switch, elsewhere) == UNIQUE_VIOLATION
    # Fijarlo después del envío reclama la clave igual que al insertarlo.
    message_id = uuid.uuid4()
    queued = email_message(scope, notifications.now, message_id=message_id)
    set_id = (
        "UPDATE notifications.email_message SET provider_message_id = $2 WHERE message_id = $1",
        [message_id, provider_id],
    )
    assert await _steps(app, scope.organization_id, first, queued, set_id) == UNIQUE_VIOLATION
    # Sin identificador del proveedor (aún sin enviar), ninguna clave: varios correos en cola.
    assert (
        await _steps(app, scope.organization_id, email_message(scope), email_message(scope)) == "ok"
    )


@pytest.mark.asyncio
async def test_the_provider_message_id_is_set_once(app: Any, notifications: Notifications) -> None:
    scope = notifications.a
    message_id = uuid.uuid4()
    queued = email_message(scope, notifications.now, message_id=message_id)

    def set_id(value: str | None) -> tuple[str, list[Any]]:
        return (
            "UPDATE notifications.email_message SET provider_message_id = $2 WHERE message_id = $1",
            [message_id, value],
        )

    same = f"proveedor-{uuid.uuid4().hex}"
    assert await _steps(app, scope.organization_id, queued, set_id(same), set_id(same)) == "ok"
    for change in (f"proveedor-{uuid.uuid4().hex}", None):
        outcome = await _steps(app, scope.organization_id, queued, set_id(same), set_id(change))
        assert outcome == CHECK_VIOLATION, change


# --- Restricciones del diseño -----


@pytest.mark.asyncio
async def test_design_constraints(app: Any, notifications: Notifications) -> None:
    scope = notifications.a
    org = scope.organization_id
    now = notifications.now
    base = notification(scope, now)
    rejected = {
        "urgencia fuera de la lista": _with(base, urgency="critical"),
        "título de 121": _with(base, title_es="t" * 121),
        "título vacío": _with(base, title_es=""),
        "cuerpo de 1001": _with(base, body_es="c" * 1001),
        "evento con texto libre": _with(base, event_name="Hallazgo nuevo"),
        "enlace con URL": _with(
            base, deep_link_ref=json.dumps({"kind": "finding", "id": "https://x.invalid"})
        ),
        "enlace con clave extra": _with(
            base,
            deep_link_ref=json.dumps(
                {"kind": "finding", "id": str(uuid.uuid4()), "url": "https://x.invalid"}
            ),
        ),
        "enlace de tipo desconocido": _with(
            base, deep_link_ref=json.dumps({"kind": "person", "id": str(uuid.uuid4())})
        ),
        "zona sin planta": _with(base, plant_id=None),
        "leída antes de crearse": _with(base, read_at=now - dt.timedelta(seconds=1)),
        "preferencia con día 0": _with(
            user_notification_preference(scope), weekly_digest_weekday=0
        ),
        "preferencia con día 8": _with(
            user_notification_preference(scope), weekly_digest_weekday=8
        ),
        "supresión sin marca": _with(
            user_notification_preference(scope), email_suppressed_complaint=True
        ),
        "lote con cuenta distinta": _with(digest_batch(scope, now), item_count=2),
        "lote con ventana vacía": _with(digest_batch(scope, now), window_to=now),
        "lote de tipo desconocido": _with(digest_batch(scope, now), kind="daily"),
        "correo de asunto 121": _with(email_message(scope, now), subject_es="a" * 121),
        "correo de cuerpo 8001": _with(email_message(scope, now), body_es="b" * 8001),
        "correo de tipo desconocido": _with(email_message(scope, now), kind="marketing"),
        "rebote desconocido": _with(email_message(scope, now), bounce_kind="maybe"),
        "estado con texto libre": _with(email_message(scope, now), status="En cola"),
        "enviado antes de encolarse": _with(
            email_message(scope, now), sent_at=now - dt.timedelta(seconds=1)
        ),
        "identificadores con nulo": _with(email_message(scope, now), related_ids=[None]),
    }
    # La preferencia sembrada choca por clave: estas pruebas usan otro usuario.
    preference_user = await _other_user(notifications)
    for label in ("preferencia con día 0", "preferencia con día 8", "supresión sin marca"):
        rejected[label] = _with(rejected[label], user_id=preference_user)
    outcomes = {label: await _steps(app, org, statement) for label, statement in rejected.items()}
    assert outcomes == dict.fromkeys(rejected, CHECK_VIOLATION)

    # Un destinatario de otra organización no existe para la clave foránea (BR-LAZ-124).
    foreign = _with(base, recipient_user_id=notifications.b.user_id)
    assert await _steps(app, org, foreign) == FOREIGN_KEY_VIOLATION
    # Una entrega con otra planta que la de su aviso: el disparador la rechaza.
    delivery = notification_delivery(scope)
    other_plant = notifications.seed.plant(org, 1).plant_id
    other_zone = notifications.seed.plant(org, 1).zone_id
    assert (
        await _steps(app, org, _with(delivery, plant_id=other_plant, zone_id=other_zone))
        == FOREIGN_KEY_VIOLATION
    )
    assert await _steps(app, org, _with(delivery, plant_id=None, zone_id=None)) == (
        FOREIGN_KEY_VIOLATION
    )
    # El canal de aplicación no lleva identificador del proveedor.
    assert (
        await _steps(app, org, _with(delivery, channel="in_app", provider_message_id="x"))
        == CHECK_VIOLATION
    )
    # La entrega válida, con su lote, y el correo sin cuerpo (lo que deja la retención de 90 días).
    assert await _steps(app, org, delivery) == "ok"
    assert await _steps(app, org, _with(email_message(scope, now), body_es=None)) == "ok"
    # Un aviso de la organización (sin planta ni zona) y su entrega.
    plantless_id = uuid.uuid4()
    plantless = notification(scope, now, notification_id=plantless_id, with_plant=False)
    plantless_scope = dataclasses.replace(
        scope, notification_id=plantless_id, notification_created_at=now
    )
    plantless_delivery = _with(
        notification_delivery(plantless_scope, in_batch=False), plant_id=None, zone_id=None
    )
    assert await _steps(app, org, plantless, plantless_delivery) == "ok"


@pytest.mark.asyncio
async def test_the_app_cannot_change_fixed_columns(app: Any, notifications: Notifications) -> None:
    scope = notifications.a
    for statement in (
        "UPDATE notifications.notification SET recipient_user_id = recipient_user_id",
        "UPDATE notifications.notification SET plant_id = plant_id",
        "UPDATE notifications.notification SET body_es = body_es",
        "UPDATE notifications.notification_delivery SET plant_id = plant_id",
        "UPDATE notifications.email_message SET body_es = body_es",
        "UPDATE notifications.email_message SET to_user_id = to_user_id",
        "UPDATE notifications.user_notification_preference SET organization_id = organization_id",
        "UPDATE notifications.digest_batch SET notification_ids = notification_ids",
        "DELETE FROM notifications.notification",
        "DELETE FROM notifications.email_message",
    ):
        assert await _steps(app, scope.organization_id, (statement, [])) == (
            INSUFFICIENT_PRIVILEGE
        ), statement
    assert (
        await _steps(
            app,
            scope.organization_id,
            ("UPDATE notifications.notification SET read_at = created_at", []),
        )
        == "ok"
    )


# --- Concurrencia: la unicidad de las claves -----
# Al final del módulo: la base sin guardas vuelve a migrar y cambia las contraseñas de los
# roles del clúster, así que ninguna prueba de la primera base puede venir después.


@dataclass(frozen=True)
class Race:
    """Dos escrituras que compiten; ``loser`` es el SQLSTATE de la que llega segunda."""

    first: tuple[tuple[str, list[Any]], ...]
    second: tuple[tuple[str, list[Any]], ...]
    loser: str


async def _seeded_message(notifications: Notifications) -> uuid.UUID:
    """Un correo en cola sin identificador del proveedor, confirmado."""
    message_id = uuid.uuid4()
    connection = await notifications.database.connect()
    try:
        async with connection.transaction():
            await set_scope(connection, notifications.a.organization_id)
            sql, args = email_message(notifications.a, notifications.now, message_id=message_id)
            await connection.execute(sql, *args)
    finally:
        await connection.close()
    return message_id


async def _race_scenarios(notifications: Notifications) -> dict[str, Race]:
    scope = notifications.a
    event = uuid.uuid4()
    provider_id = f"proveedor-{uuid.uuid4().hex}"
    updated_id = f"proveedor-{uuid.uuid4().hex}"
    first_message = await _seeded_message(notifications)
    second_message = await _seeded_message(notifications)

    def set_id(message_id: uuid.UUID) -> tuple[str, list[Any]]:
        return (
            "UPDATE notifications.email_message SET provider_message_id = $2 WHERE message_id = $1",
            [message_id, updated_id],
        )

    return {
        "un aviso por evento y destinatario": Race(
            (notification(scope, notifications.now, source_event_id=event),),
            (notification(scope, dt.datetime(2026, 9, 3, tzinfo=dt.UTC), source_event_id=event),),
            UNIQUE_VIOLATION,
        ),
        "un correo por identificador del proveedor": Race(
            (email_message(scope, notifications.now, provider_message_id=provider_id),),
            (email_message(scope, notifications.now, provider_message_id=provider_id),),
            UNIQUE_VIOLATION,
        ),
        "el identificador del proveedor fijado en dos correos": Race(
            (set_id(first_message),), (set_id(second_message),), UNIQUE_VIOLATION
        ),
    }


RACES: Final = (
    "un aviso por evento y destinatario",
    "un correo por identificador del proveedor",
    "el identificador del proveedor fijado en dos correos",
)


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
            for sql, args in race.first:
                await first.execute(sql, *args)

            async def contender() -> str:
                try:
                    async with second.transaction():
                        await set_scope(second, organization_id)
                        for sql, args in race.second:
                            await second.execute(sql, *args)
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
async def test_the_keys_hold_under_concurrency(name: str, notifications: Notifications) -> None:
    race = (await _race_scenarios(notifications))[name]
    assert await _race(notifications.database, notifications.a.organization_id, race) == [
        "ok",
        race.loser,
    ]


_GUARDS_REMOVED: Final = (
    "DROP TRIGGER claim_source_key ON notifications.notification",
    "DROP TRIGGER claim_provider_message_key ON notifications.email_message",
)


@pytest.fixture(scope="module")
def unguarded(postgres_endpoint: PostgresEndpoint) -> Iterator[Notifications]:
    """Otra base igual, sin los disparadores de las claves (sonda negativa)."""
    with seeded_identity(postgres_endpoint, "vigia_laz_0030_unguarded") as (database, identity):
        seeded = _prepare(database, identity)

        async def strip() -> None:
            connection = await database.connect()
            try:
                for statement in _GUARDS_REMOVED:
                    await connection.execute(statement)
            finally:
                await connection.close()

        asyncio.run(strip())
        yield seeded


@pytest.mark.asyncio
@pytest.mark.parametrize("name", RACES)
async def test_the_keys_without_their_trigger(name: str, unguarded: Notifications) -> None:
    race = (await _race_scenarios(unguarded))[name]
    # La misma carrera: sin el disparador la segunda no espera ningún candado y las dos pasan.
    assert await _race(unguarded.database, unguarded.a.organization_id, race) == ["ok", "ok"]
