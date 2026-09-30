"""PR-NUC-18: ninguna tabla de solo anexar admite ``UPDATE``, ``DELETE`` ni ``TRUNCATE`` (TASK-108).

Metapropiedad (BR-NUC-43, PAT-NUC-SEG-05) sobre las tablas ⛓ de ``nuc_0002`` y ``nuc_0003``:
``ledger.ledger_record``, ``ledger.record_source_key``, ``ledger.record_identity``,
``ledger.evidence``, ``ledger.evidence_identity``, ``ledger.label``, ``shared.audit_entry``,
``shared.audit_entry_identity``, ``shared.outbox_event`` y ``shared.dead_letter``.

- Con ``vigia_app``, cualquier sentencia de mutación generada falla por permisos (``42501``),
  salvo el ``UPDATE`` de las columnas de la marca de ``ledger.evidence`` (``nuc_0006``), que
  para el disparador (``23001``).
- Con ``vigia_migrate`` (dueño) o con el superusuario del contenedor (el "rol privilegiado de
  prueba" del criterio), falla por el disparador (``23001``), también sobre las particiones y con
  ``session_replication_role = replica``. Los datos quedan intactos.

Además, los criterios del particionado y del encadenado que se comprueban sin concurrencia:

- una fila con la marca fuera del rango cubierto cae en la partición por defecto sin rechazarse;
- la migración crea la partición por defecto y las del mes en curso y los tres siguientes;
- el disparador ignora los hashes y la secuencia que aporta la aplicación, rechaza la fila de otra
  organización, la de ``plant_id`` incoherente con el tipo y la que cambió de mes esperando la
  exclusión; la clave de idempotencia es única por organización y tipo; la marca nunca es menor
  que la de la cabeza;
- ``record_id``, ``entry_id`` y ``evidence_id`` son únicos aunque la marca sea otra;
- la seguridad a nivel de fila está forzada: el dueño de las tablas (``vigia_migrate``) con el
  contexto de otra organización no ve ninguna fila.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    APPEND_ONLY_TABLES,
    INSUFFICIENT_PRIVILEGE,
    KEYED_RECORD_TYPE,
    ORGANIZATION_RECORD_TYPE,
    PLANT_RECORD_TYPE,
    RESTRICT_VIOLATION,
    SERIALIZATION_FAILURE,
    UNIQUE_VIOLATION,
    DatabaseLoop,
    MigratedDatabase,
    audit_values,
    evidence_values,
    insert_audit,
    insert_evidence,
    insert_record,
    migrated_database,
    record_values,
    register_record_types,
    set_organization,
)

pytestmark = pytest.mark.integration

ORGANIZATION = uuid.UUID("0a0a0a0a-0000-4000-8000-000000000001")
PLANT = uuid.UUID("0b0b0b0b-0000-4000-8000-000000000001")
FAR_FUTURE = datetime(2031, 6, 15, 12, 0, tzinfo=UTC)
PRIVILEGED = ("vigia_migrate", None)
"""El dueño de las tablas y el superusuario del contenedor."""

CHECK_VIOLATION = "23514"
CLIENT_TABLES = (
    "ledger.chain_head",
    "ledger.ledger_record",
    "ledger.record_source_key",
    "ledger.record_identity",
    "ledger.evidence",
    "ledger.evidence_identity",
    "ledger.label",
    "shared.audit_entry",
    "shared.audit_entry_identity",
    "shared.outbox_event",
    "shared.dead_letter",
)
"""Tablas de cliente con filas de ``ORGANIZATION`` tras ``_seed``."""


# --- Fixtures -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_append_only") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def loop() -> Iterator[DatabaseLoop]:
    runner = DatabaseLoop()
    yield runner
    runner.close()


async def _seed(superuser: Any, app: Any) -> None:
    """Una fila, o más, en cada tabla de solo anexar, escrita por ``vigia_app``."""
    await register_record_types(superuser)
    await superuser.execute(
        "INSERT INTO shared.event_type VALUES ('zone_created', 'U-02', '{}', 'Zona creada')"
    )
    await superuser.execute(
        "INSERT INTO shared.consumer (consumer_name, unit) VALUES ('probe', 'U-02')"
    )
    async with app.transaction():
        await set_organization(app, ORGANIZATION)
        record = await insert_record(app, record_values(ORGANIZATION, PLANT))
        await insert_record(
            app,
            record_values(ORGANIZATION, PLANT, record_type=KEYED_RECORD_TYPE, source_key="F-1"),
        )
        await insert_record(app, record_values(ORGANIZATION, None))
        await insert_audit(app, audit_values(ORGANIZATION, filters={"zone": "Z-01"}))
        for verified_at in (record["received_at"], FAR_FUTURE):
            await insert_evidence(
                app, evidence_values(ORGANIZATION, PLANT, record["record_id"], verified_at)
            )
        await app.execute(
            "INSERT INTO ledger.label (label_id, organization_id, plant_id, zone_id,"
            " source_record_id, subject_record_id, family, outcome, reason_category, labeled_at,"
            " labeled_by) VALUES ($1, $2, $3, $4, $5, $6, 'ppe', 'confirmed', 'helmet', now(),"
            " '{\"kind\": \"user\"}')",
            uuid.uuid4(), ORGANIZATION, PLANT, uuid.uuid4(), uuid.uuid4(), record["record_id"],
        )  # fmt: skip
        event_id = uuid.uuid4()
        await app.execute(
            "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
            " ledger_sequence, payload, correlation_id, created_at)"
            " VALUES ($1, $2, $3, 'zone_created', 1, '{\"zone\": 1}', $4, now())",
            event_id, ORGANIZATION, PLANT, uuid.uuid4(),
        )  # fmt: skip
        await app.execute(
            "INSERT INTO shared.dead_letter (event_id, consumer_name, organization_id, failed_at,"
            " attempts, last_error_code) VALUES ($1, 'probe', $2, now(), 8, 'timeout')",
            event_id, ORGANIZATION,
        )  # fmt: skip


@pytest.fixture(scope="module")
def connections(database: MigratedDatabase, loop: DatabaseLoop) -> Iterator[dict[str | None, Any]]:
    opened = {
        role: loop.run(database.connect(role)) for role in ("vigia_app", "vigia_migrate", None)
    }
    loop.run(_seed(opened[None], opened["vigia_app"]))
    yield opened
    for connection in opened.values():
        loop.run(connection.close())


async def _targets(superuser: Any) -> list[str]:
    """Cada tabla ⛓ y cada partición suya con filas (para que actúe el disparador de fila)."""
    targets = list(APPEND_ONLY_TABLES)
    for table in APPEND_ONLY_TABLES:
        rows = await superuser.fetch(
            f"SELECT DISTINCT tableoid::regclass::text AS name FROM {table}"  # noqa: S608
        )
        targets.extend(row["name"] for row in rows if row["name"] != table)
    return sorted(set(targets))


@pytest.fixture(scope="module")
def targets(connections: dict[str | None, Any], loop: DatabaseLoop) -> list[str]:
    return loop.run(_targets(connections[None]))


async def _snapshot(superuser: Any) -> dict[str, str]:
    """Contenido de cada tabla ⛓ (visto por el superusuario, sin seguridad a nivel de fila)."""
    snapshot = {}
    for table in APPEND_ONLY_TABLES:
        snapshot[table] = await superuser.fetchval(
            f"SELECT count(*)::text || ':' || coalesce(md5(string_agg(t::text, '|'"  # noqa: S608
            f" ORDER BY t::text)), '') FROM {table} AS t"
        )
    return snapshot


async def _columns(superuser: Any, table: str) -> list[str]:
    schema, name = table.split(".")
    rows = await superuser.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = $1"
        " AND table_name = $2 AND is_generated = 'NEVER' ORDER BY ordinal_position",
        schema,
        name,
    )
    return [row["column_name"] for row in rows]


async def _attempt(
    connection: Any, statement: str, *, replica: bool = False
) -> asyncpg.PostgresError | None:
    """Ejecuta ``statement`` con el contexto de la organización; siempre revierte."""
    transaction = connection.transaction()
    await transaction.start()
    try:
        await set_organization(connection, ORGANIZATION)
        if replica:
            await connection.execute("SET LOCAL session_replication_role = replica")
        try:
            await connection.execute(statement)
        except asyncpg.PostgresError as error:
            return error
        return None
    finally:
        await transaction.rollback()


# --- PR-NUC-18 ------------------------------------------------------------------------------------

_WHERE = ("", " WHERE true", f" WHERE organization_id = '{ORGANIZATION}'")


@st.composite
def mutation_statements(draw: st.DrawFn, targets: list[str], columns: dict[str, list[str]]) -> str:
    """``UPDATE``, ``DELETE`` o ``TRUNCATE`` sobre una tabla ⛓ o una partición con filas."""
    target = draw(st.sampled_from(targets))
    # La más larga: ``ledger.evidence_identity`` también empieza por ``ledger.evidence_``.
    parent = max((table for table in APPEND_ONLY_TABLES if target.startswith(table)), key=len)
    kind = draw(st.sampled_from(("update", "delete", "truncate")))
    where = draw(st.sampled_from(_WHERE))
    if kind == "update":
        column = draw(st.sampled_from(columns[parent]))
        return f"UPDATE {target} SET {column} = {column}{where}"  # noqa: S608 - nombres fijos
    if kind == "delete":
        return f"DELETE FROM {target}{where}"  # noqa: S608 - nombres fijos
    return f"TRUNCATE {target}" + draw(st.sampled_from(("", " CASCADE")))


@pytest.fixture(scope="module")
def columns(connections: dict[str | None, Any], loop: DatabaseLoop) -> dict[str, list[str]]:
    return {table: loop.run(_columns(connections[None], table)) for table in APPEND_ONLY_TABLES}


_MARKER_COLUMNS = (
    "marker_verification_result",
    "marker_verified_at",
    "container_marker_sampled_at",
)
"""Columnas que ``vigia_app`` puede actualizar en ``ledger.evidence`` (privilegio por columna)."""


def _marker_update(statement: str) -> bool:
    prefix = "UPDATE ledger.evidence SET "
    return statement.startswith(prefix) and statement[len(prefix) :].split(" ")[0] in (
        _MARKER_COLUMNS
    )


@given(data=st.data(), role=st.sampled_from(("vigia_app", "vigia_migrate", None)))
def test_mutations_on_append_only_tables_always_fail(
    loop: DatabaseLoop,
    connections: dict[str | None, Any],
    targets: list[str],
    columns: dict[str, list[str]],
    data: st.DataObject,
    role: str | None,
) -> None:
    statement = data.draw(mutation_statements(targets, columns), label="statement")
    before = loop.run(_snapshot(connections[None]))
    error = loop.run(_attempt(connections[role], statement))
    assert error is not None, f"{role or 'superusuario'} ejecutó {statement}"
    if role == "vigia_app" and _marker_update(statement):
        # nuc_0006 (TASK-121): vigia_app puede actualizar las columnas de la marca de
        # ledger.evidence, pero el disparador solo deja pasar pending → intact | broken.
        assert error.sqlstate == RESTRICT_VIOLATION, (statement, error)
    elif role == "vigia_app":
        assert error.sqlstate == INSUFFICIENT_PRIVILEGE, (statement, error)
    elif not statement.startswith("TRUNCATE"):
        # Rol privilegiado: lo impide el disparador, no los permisos.
        assert error.sqlstate == RESTRICT_VIOLATION, (statement, error)
    assert loop.run(_snapshot(connections[None])) == before


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
@pytest.mark.parametrize("role", PRIVILEGED, ids=("vigia_migrate", "superuser"))
def test_privileged_update_delete_truncate_are_rejected_by_the_trigger(
    loop: DatabaseLoop, connections: dict[str | None, Any], table: str, role: str | None
) -> None:
    connection = connections[role]
    column = "organization_id"
    for statement in (
        f"UPDATE {table} SET {column} = {column}",  # noqa: S608 - nombre fijo
        f"DELETE FROM {table}",  # noqa: S608 - nombre fijo
        f"TRUNCATE {table} CASCADE",
    ):
        error = loop.run(_attempt(connection, statement))
        assert error is not None and error.sqlstate == RESTRICT_VIOLATION, (statement, error)


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_replication_role_replica_does_not_skip_the_trigger(
    loop: DatabaseLoop, connections: dict[str | None, Any], table: str
) -> None:
    """``ENABLE ALWAYS``: ni siquiera ``session_replication_role = replica`` los salta."""
    error = loop.run(_attempt(connections[None], f"DELETE FROM {table}", replica=True))  # noqa: S608
    assert error is not None and error.sqlstate == RESTRICT_VIOLATION, (table, error)


def test_every_partition_is_protected(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    """Cada partición tiene sus disparadores activos siempre (``A``) y el de vaciado propio."""
    rows = loop.run(
        connections[None].fetch(
            "SELECT c.oid::regclass::text AS name,"
            " array_agg(t.tgname ORDER BY t.tgname) FILTER (WHERE NOT t.tgisinternal) AS names,"
            " bool_and(t.tgenabled = 'A') FILTER (WHERE NOT t.tgisinternal) AS always"
            " FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " LEFT JOIN pg_trigger t ON t.tgrelid = c.oid"
            " WHERE i.inhparent IN ('ledger.ledger_record'::regclass, 'ledger.evidence'::regclass,"
            " 'shared.audit_entry'::regclass) GROUP BY 1"
        )
    )
    assert len(rows) == 3 * 5  # partición por defecto, mes en curso y tres siguientes
    for row in rows:
        assert row["always"] is True, row
        assert "append_only_truncate" in row["names"], row
        assert any(name.endswith("_append_only_row") for name in row["names"]), row


def test_new_partition_protected_by_the_helper_rejects_mutations(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    """Lo que hará ``create_partitions`` (TASK-131): partición nueva y función de protección."""

    async def scenario() -> tuple[asyncpg.PostgresError | None, asyncpg.PostgresError | None]:
        superuser = connections[None]
        transaction = superuser.transaction()
        await transaction.start()
        try:
            await superuser.execute("SET LOCAL ROLE vigia_migrate")
            await superuser.execute(
                "CREATE TABLE ledger.evidence_2035_01 PARTITION OF ledger.evidence"
                " FOR VALUES FROM ('2035-01-01 00:00:00+00') TO ('2035-02-01 00:00:00+00')"
            )
            await superuser.execute(
                "SELECT shared.vigia_protect_append_only_partition('ledger.evidence_2035_01')"
            )
            await superuser.execute("RESET ROLE")
            await superuser.execute("SAVEPOINT probe")
            try:
                await superuser.execute("TRUNCATE ledger.evidence_2035_01")
                truncate = None
            except asyncpg.PostgresError as error:
                truncate = error
            await superuser.execute("ROLLBACK TO SAVEPOINT probe")
            enabled = await superuser.fetchval(
                "SELECT bool_and(tgenabled = 'A') FROM pg_trigger"
                " WHERE tgrelid = 'ledger.evidence_2035_01'::regclass AND NOT tgisinternal"
            )
            assert enabled is True
            return truncate, None
        finally:
            await transaction.rollback()

    truncate, _ = loop.run(scenario())
    assert truncate is not None and truncate.sqlstate == RESTRICT_VIOLATION


# --- Particiones ----------------------------------------------------------------------------------


def test_migration_creates_default_and_next_months(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    now = loop.run(connections[None].fetchval("SELECT now()"))
    first = datetime(now.year, now.month, 1, tzinfo=UTC)
    months = []
    for offset in range(4):
        year, month = divmod(first.month - 1 + offset, 12)
        months.append(f"{first.year + year:04d}_{month + 1:02d}")
    for parent in ("ledger.ledger_record", "ledger.evidence", "shared.audit_entry"):
        names = {
            row["name"]
            for row in loop.run(
                connections[None].fetch(
                    "SELECT inhrelid::regclass::text AS name FROM pg_inherits"
                    " WHERE inhparent = $1::regclass",
                    parent,
                )
            )
        }
        assert names == {f"{parent}_default"} | {f"{parent}_{month}" for month in months}


def test_rows_outside_the_covered_range_fall_into_the_default_partition(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    """Sin partición para la marca, la fila va a la partición por defecto y no se rechaza."""

    async def scenario() -> dict[str, str]:
        superuser = connections[None]
        placed: dict[str, str] = {}
        # Evidencia: la marca la aporta la aplicación (verified_at), en un mes sin partición.
        placed["ledger.evidence"] = await superuser.fetchval(
            "SELECT tableoid::regclass::text FROM ledger.evidence WHERE verified_at = $1",
            FAR_FUTURE,
        )
        # Expediente y auditoría: la marca la pone el disparador (ahora); se retira la partición
        # del mes en curso dentro de una transacción que se revierte.
        transaction = superuser.transaction()
        await transaction.start()
        try:
            for parent in ("ledger.ledger_record", "shared.audit_entry"):
                current = await _current_partition(superuser, parent)
                await superuser.execute(f"ALTER TABLE {parent} DETACH PARTITION {current}")
            await set_organization(superuser, ORGANIZATION)
            record = await insert_record(superuser, record_values(ORGANIZATION, PLANT))
            entry = await insert_audit(superuser, audit_values(ORGANIZATION))
            placed["ledger.ledger_record"] = await superuser.fetchval(
                "SELECT tableoid::regclass::text FROM ledger.ledger_record WHERE record_id = $1",
                record["record_id"],
            )
            placed["shared.audit_entry"] = await superuser.fetchval(
                "SELECT tableoid::regclass::text FROM shared.audit_entry WHERE entry_id = $1",
                entry["entry_id"],
            )
        finally:
            await transaction.rollback()
        return placed

    assert loop.run(scenario()) == {
        "ledger.evidence": "ledger.evidence_default",
        "ledger.ledger_record": "ledger.ledger_record_default",
        "shared.audit_entry": "shared.audit_entry_default",
    }


async def _current_partition(superuser: Any, parent: str) -> str:
    name: str = await superuser.fetchval(
        "SELECT $1 || to_char(now() AT TIME ZONE 'UTC', '\"_\"YYYY\"_\"MM')", parent
    )
    return name


# --- El disparador de encadenado ------------------------------------------------------------------


async def _in_context(connection: Any, organization_id: uuid.UUID, statement: Any) -> Any:
    transaction = connection.transaction()
    await transaction.start()
    try:
        await set_organization(connection, organization_id)
        try:
            return await statement(connection)
        except asyncpg.PostgresError as error:
            return error
    finally:
        await transaction.rollback()


def test_trigger_overrides_application_supplied_chain_columns(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    organization_id = uuid.uuid4()
    values = record_values(organization_id, uuid.uuid4())
    row = loop.run(
        _in_context(connections["vigia_app"], organization_id, lambda c: insert_record(c, values))
    )
    assert row["chain_sequence"] == 1
    for column in ("content_hash", "previous_hash", "record_hash"):
        assert row[column] != "0" * 64, column


def test_row_of_another_organization_is_rejected(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    other = uuid.uuid4()
    for role in ("vigia_app", None):
        error = loop.run(
            _in_context(
                connections[role],
                ORGANIZATION,
                lambda c: insert_record(c, record_values(other, uuid.uuid4())),
            )
        )
        assert isinstance(error, asyncpg.PostgresError)
        assert error.sqlstate == INSUFFICIENT_PRIVILEGE


def test_insert_without_context_is_rejected(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    async def insert(connection: Any) -> Any:
        async with connection.transaction():
            return await insert_record(connection, record_values(ORGANIZATION, PLANT))

    with pytest.raises(asyncpg.PostgresError) as caught:
        loop.run(insert(connections["vigia_app"]))
    assert caught.value.sqlstate == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    ("record_type", "plant"),
    [(PLANT_RECORD_TYPE, False), (ORGANIZATION_RECORD_TYPE, True)],
    ids=("plant-type-without-plant", "organization-type-with-plant"),
)
def test_plant_must_match_the_chain_level_of_the_type(
    loop: DatabaseLoop, connections: dict[str | None, Any], record_type: str, plant: bool
) -> None:
    values = record_values(ORGANIZATION, uuid.uuid4() if plant else None, record_type=record_type)
    error = loop.run(
        _in_context(connections["vigia_app"], ORGANIZATION, lambda c: insert_record(c, values))
    )
    assert isinstance(error, asyncpg.PostgresError) and error.sqlstate == CHECK_VIOLATION


def test_mark_from_another_month_is_a_transient_failure(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    """La fila se enruta con la marca por defecto; si no es del mes de la exclusión, ``40001``."""
    database_now: datetime = loop.run(connections[None].fetchval("SELECT now()"))
    values = record_values(ORGANIZATION, PLANT) | {"received_at": database_now - timedelta(days=45)}
    error = loop.run(
        _in_context(connections["vigia_app"], ORGANIZATION, lambda c: insert_record(c, values))
    )
    assert isinstance(error, asyncpg.PostgresError) and error.sqlstate == SERIALIZATION_FAILURE


def test_source_key_is_unique_per_organization_and_type(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    async def twice(connection: Any, first: uuid.UUID, second: uuid.UUID) -> Any:
        await set_organization(connection, first)
        await insert_record(
            connection, record_values(first, PLANT, record_type=KEYED_RECORD_TYPE, source_key="K")
        )
        await set_organization(connection, second)
        return await insert_record(
            connection, record_values(second, PLANT, record_type=KEYED_RECORD_TYPE, source_key="K")
        )

    same = uuid.uuid4()
    error = loop.run(_in_context(connections["vigia_app"], same, lambda c: twice(c, same, same)))
    assert isinstance(error, asyncpg.PostgresError) and error.sqlstate == UNIQUE_VIOLATION
    first, second = uuid.uuid4(), uuid.uuid4()
    row = loop.run(_in_context(connections["vigia_app"], first, lambda c: twice(c, first, second)))
    assert not isinstance(row, asyncpg.PostgresError) and row["source_key"] == "K"


def test_application_only_reads_chain_heads_and_its_own_organization(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    app = connections["vigia_app"]
    for statement in (
        "UPDATE ledger.chain_head SET last_sequence = 0",
        "INSERT INTO ledger.chain_head VALUES (gen_random_uuid(), NULL, 'audit', 0,"
        " repeat('0', 64), now())",
    ):
        error = loop.run(_attempt(app, statement))
        assert error is not None and error.sqlstate == INSUFFICIENT_PRIVILEGE, statement

    async def visible(organization_id: uuid.UUID | None) -> int:
        async with app.transaction():
            if organization_id is not None:
                await set_organization(app, organization_id)
            count: int = await app.fetchval("SELECT count(*) FROM ledger.ledger_record")
            return count

    assert loop.run(visible(None)) == 0
    assert loop.run(visible(uuid.uuid4())) == 0
    assert loop.run(visible(ORGANIZATION)) >= 3


@pytest.mark.parametrize("table", CLIENT_TABLES)
def test_row_security_is_forced_even_for_the_owner(
    loop: DatabaseLoop, connections: dict[str | None, Any], table: str
) -> None:
    """``FORCE ROW LEVEL SECURITY``: el dueño solo ve la organización del contexto."""

    async def visible(organization_id: uuid.UUID) -> int:
        migrate = connections["vigia_migrate"]
        async with migrate.transaction():
            await set_organization(migrate, organization_id)
            count: int = await migrate.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
            return count

    assert loop.run(visible(uuid.uuid4())) == 0
    assert loop.run(visible(ORGANIZATION)) >= 1


# --- Identificadores únicos aunque la marca sea otra ----------------------------------------------


async def _twice(connection: Any, insert: Any) -> asyncpg.PostgresError | None:
    """La misma fila en dos transacciones confirmadas por separado (reintento tras un COMMIT)."""
    try:
        for _ in range(2):
            async with connection.transaction():
                await set_organization(connection, ORGANIZATION)
                await insert(connection)
            await connection.execute("SELECT pg_sleep(0.01)")
    except asyncpg.PostgresError as error:
        return error
    return None


def test_record_id_is_unique_across_marks(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    values = record_values(ORGANIZATION, PLANT)
    error = loop.run(_twice(connections["vigia_app"], lambda c: insert_record(c, values)))
    assert error is not None and error.sqlstate == UNIQUE_VIOLATION
    rows = loop.run(
        connections[None].fetchval(
            "SELECT count(*) FROM ledger.ledger_record WHERE record_id = $1", values["record_id"]
        )
    )
    assert rows == 1


def test_entry_id_is_unique_across_marks(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    values = audit_values(ORGANIZATION)
    error = loop.run(_twice(connections["vigia_app"], lambda c: insert_audit(c, values)))
    assert error is not None and error.sqlstate == UNIQUE_VIOLATION
    rows = loop.run(
        connections[None].fetchval(
            "SELECT count(*) FROM shared.audit_entry WHERE entry_id = $1", values["entry_id"]
        )
    )
    assert rows == 1


def test_evidence_id_is_unique_across_marks(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    first = evidence_values(ORGANIZATION, PLANT, uuid.uuid4(), FAR_FUTURE)
    second = first | {"verified_at": FAR_FUTURE + timedelta(days=40)}
    marks = iter((first, second))
    error = loop.run(_twice(connections["vigia_app"], lambda c: insert_evidence(c, next(marks))))
    assert error is not None and error.sqlstate == UNIQUE_VIOLATION


def test_mark_never_goes_below_the_chain_head(
    loop: DatabaseLoop, connections: dict[str | None, Any]
) -> None:
    """Si el reloj retrocede (aquí, la cabeza adelantada a mano), la marca sigue la de la cabeza."""
    organization_id = uuid.uuid4()

    async def scenario() -> tuple[datetime, datetime]:
        superuser = connections[None]
        async with superuser.transaction():
            await set_organization(superuser, organization_id)
            await insert_record(superuser, record_values(organization_id, None))
            ahead: datetime = await superuser.fetchval(
                "UPDATE ledger.chain_head SET updated_at = updated_at + interval '1 second'"
                " WHERE organization_id = $1 RETURNING updated_at",
                organization_id,
            )
            second = await insert_record(superuser, record_values(organization_id, None))
            return ahead, second["received_at"]

    ahead, received_at = loop.run(scenario())
    assert received_at >= ahead
