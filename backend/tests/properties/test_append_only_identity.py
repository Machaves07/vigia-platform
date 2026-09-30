"""PR-NUC-18 sobre las tablas ⛓ de ``identity`` y la actualización de cierre (TASK-107).

PR-NUC-18: cualquier ``UPDATE``, ``DELETE`` o ``TRUNCATE`` generado sobre cualquier tabla de solo
anexar falla, con ``vigia_app`` (sin privilegio: ``42501``) y también con el dueño
``vigia_migrate`` y con un superusuario (el disparador: ``23001 restrict_violation``). La única
excepción es la **actualización de cierre**, que la propiedad excluye y prueban los ejemplos:

- ``zone_node_assignment``: fijar una vez ``unassigned_at`` nulo, sin tocar nada más (criterio
  5 de TASK-107, ``test_unassigned_at_is_set_once_and_nothing_else_changes``);
- ``role_assignment``: fijar una vez ``removed_at`` y ``removed_by``;
- ``provider_concession``: de ``active`` a ``revoked`` (con ``revoked_*``) o a ``expired``.

Base migrada propia del módulo, con los datos de ``tests/identity_db.py`` más una fila cerrada
de cada tabla con cierre. Cada mutación corre en una transacción que se revierte siempre.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.identity_db import (
    APPEND_ONLY_TABLES,
    BASE_TIME,
    PRIMARY_KEYS,
    IdentitySeed,
    MigratedDatabase,
    insert_concession,
    seeded_identity,
    set_scope,
)
from tests.integration.conftest import PostgresEndpoint

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"
RESTRICT_VIOLATION = "23001"

ROLES = ("vigia_app", "vigia_migrate", "superuser")

CLOSING_COLUMNS = {
    "zone_node_assignment": frozenset({"unassigned_at"}),
    "role_assignment": frozenset({"removed_at", "removed_by"}),
    "provider_concession": frozenset({"status", "revoked_at", "revoked_by", "revoked_by_side"}),
}

_OPEN_QUERY = {
    "zone_node_assignment": "unassigned_at IS NULL",
    "role_assignment": "removed_at IS NULL",
    "provider_concession": "status = 'active' AND revoked_at IS NULL",
}


@dataclass(frozen=True)
class Row:
    key: Any
    open: bool
    values: dict[str, Any]


@dataclass(frozen=True)
class AppendOnly:
    database: MigratedDatabase
    seed: IdentitySeed
    columns: dict[str, dict[str, str]]
    """Columnas de cada tabla ⛓ con su tipo (``information_schema``)."""
    rows: dict[str, tuple[Row, ...]]
    """Filas de la organización A de cada tabla ⛓, abiertas y cerradas."""


async def _close_some_rows(connection: Any, seed: IdentitySeed) -> None:
    """Una fila ya cerrada de cada tabla con cierre, en la organización A (como superusuario)."""
    a = seed.a
    plant = a.plants[0]
    await connection.execute(
        "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
        " zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        uuid.uuid4(),
        a.organization_id,
        plant.plant_id,
        plant.zone_id,
        plant.node_id,
        BASE_TIME - dt.timedelta(days=10),
        BASE_TIME - dt.timedelta(days=9),
        a.user_id,
    )
    await connection.execute(
        "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
        " scope_level, scope_id, assigned_at, assigned_by, removed_at, removed_by)"
        " VALUES ($1, $2, $3, 'copasst', 'plant', $4, $5, $6, $7, $6)",
        uuid.uuid4(),
        a.organization_id,
        a.user_id,
        plant.plant_id,
        BASE_TIME,
        seed.operator_id,
        BASE_TIME + dt.timedelta(days=1),
    )
    await insert_concession(connection, seed, a.organization_id, status="revoked")
    await insert_concession(
        connection,
        seed,
        a.organization_id,
        status="expired",
        granted_offset=-dt.timedelta(days=10),
        duration=dt.timedelta(days=2),
    )


@pytest.fixture(scope="module")
def runner() -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as loop:
        yield loop


@pytest.fixture(scope="module")
def append_only(postgres_endpoint: PostgresEndpoint) -> Iterator[AppendOnly]:
    with seeded_identity(postgres_endpoint, "vigia_append_only") as (database, seed):

        async def prepare() -> AppendOnly:
            connection = await database.connect()
            try:
                await _close_some_rows(connection, seed)
                columns: dict[str, dict[str, str]] = {}
                rows: dict[str, tuple[Row, ...]] = {}
                for table in APPEND_ONLY_TABLES:
                    columns[table] = {
                        record["column_name"]: record["data_type"]
                        for record in await connection.fetch(
                            "SELECT column_name, data_type FROM information_schema.columns"
                            " WHERE table_schema = 'identity' AND table_name = $1",
                            table,
                        )
                    }
                    open_query = _OPEN_QUERY.get(table, "false")
                    rows[table] = tuple(
                        Row(record["key"], record["open"], json.loads(record["row"]))
                        for record in await connection.fetch(
                            f"SELECT {PRIMARY_KEYS[table]} AS key, ({open_query}) AS open,"  # noqa: S608
                            f" row_to_json(t)::jsonb AS row FROM identity.{table} AS t"
                            " WHERE organization_id = $1",
                            seed.a.organization_id,
                        )
                    )
                    assert rows[table], table
                for table in CLOSING_COLUMNS:
                    assert {row.open for row in rows[table]} == {True, False}, table
            finally:
                await connection.close()
            return AppendOnly(database, seed, columns, rows)

        yield asyncio.run(prepare())


@pytest.fixture(scope="module")
def connections(append_only: AppendOnly, runner: asyncio.Runner) -> Iterator[dict[str, Any]]:
    opened = {
        role: runner.run(append_only.database.connect(None if role == "superuser" else role))
        for role in ROLES
    }
    try:
        yield opened
    finally:
        for connection in opened.values():
            runner.run(connection.close())


# --- Generación de mutaciones -------------------------------------------------------------------

_TEXT = st.one_of(
    st.sampled_from(
        ["active", "revoked", "expired", "client", "provider", "administrator", "x", ""]
    ),
    st.text(st.characters(codec="utf-8", exclude_characters="\x00"), max_size=24),
)
_VALUES = {
    "uuid": st.uuids(),
    "timestamp with time zone": st.datetimes(  # Hypothesis pide límites sin zona y la añade
        min_value=dt.datetime(2000, 1, 1),  # noqa: DTZ001
        max_value=dt.datetime(2100, 1, 1),  # noqa: DTZ001
        timezones=st.just(dt.UTC),
    ),
    "text": _TEXT,
    "jsonb": st.sampled_from(["[]", "{}", '{"keys": []}', "null", "1"]),
}


@dataclass(frozen=True)
class Mutation:
    role: str
    table: str
    kind: str
    """``update``, ``delete`` o ``truncate``."""
    target: Row | None
    """La fila por clave, o ``None`` para todas las de la organización."""
    assignments: tuple[tuple[str, Any], ...] = ()

    def is_closing(self, rows: tuple[Row, ...], types: dict[str, str]) -> bool:
        """``True`` si el disparador admite la sentencia en **alguna** de las filas que alcanza.

        Es el modelo del cierre: fila abierta, lo que cambia de verdad (un ``SET c = c`` no
        cambia nada) está dentro del grupo de cierre y la marca de fin queda fijada. Basta una
        fila: el resultado de la sentencia dependería entonces del orden de las filas y de los
        ``CHECK`` del valor, y eso lo prueban los ejemplos, no la propiedad.
        """
        group = CLOSING_COLUMNS.get(self.table)
        if self.kind != "update" or group is None:
            return False
        targets = rows if self.target is None else (self.target,)
        return any(self._closes(row, group, types) for row in targets)

    def _closes(self, row: Row, group: frozenset[str], types: dict[str, str]) -> bool:
        if not row.open:
            return False
        changed = {
            column: value
            for column, value in self.assignments
            if not _same(types[column], value, _from_json(types[column], row.values[column]))
        }
        if not set(changed) <= group:
            return False
        if self.table == "provider_concession":
            return changed.get("status", row.values["status"]) in ("revoked", "expired")
        marker = "unassigned_at" if self.table == "zone_node_assignment" else "removed_at"
        return changed.get(marker) is not None

    def statement(self) -> tuple[str, list[Any]]:
        name = f"identity.{self.table}"
        where, arguments = "", []
        if self.target is not None:
            where = f" WHERE {PRIMARY_KEYS[self.table]} = ${len(self.assignments) + 1}"
            arguments = [self.target.key]
        if self.kind == "truncate":
            return f"TRUNCATE {name}", []
        if self.kind == "delete":
            return f"DELETE FROM {name}{where}", arguments  # noqa: S608
        setters = ", ".join(f"{column} = ${i}" for i, (column, _) in enumerate(self.assignments, 1))
        values = [value for _, value in self.assignments]
        return f"UPDATE {name} SET {setters}{where}", values + arguments  # noqa: S608


def _same(data_type: str, new: Any, current: Any) -> bool:
    """¿Deja la asignación la columna igual? (``jsonb`` se compara como documento)."""
    if data_type == "jsonb" and new is not None and current is not None:
        return bool(json.loads(new) == json.loads(current))
    return bool(new == current)


def _from_json(data_type: str, value: Any) -> Any:
    """El valor actual de la columna (leído con ``row_to_json``) en el tipo que espera asyncpg."""
    if value is None or data_type == "text":
        return value
    if data_type == "uuid" and isinstance(value, str):
        return uuid.UUID(value)
    if data_type == "timestamp with time zone" and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    if data_type == "jsonb" and not isinstance(value, str):
        return json.dumps(value)
    return value


@st.composite
def mutations(draw: st.DrawFn, state: AppendOnly) -> Mutation:
    role = draw(st.sampled_from(ROLES))
    table = draw(st.sampled_from(APPEND_ONLY_TABLES))
    kind = draw(st.sampled_from(["update", "update", "update", "delete", "truncate"]))
    target = draw(st.one_of(st.none(), st.sampled_from(state.rows[table])))
    if kind != "update":
        return Mutation(role, table, kind, None if kind == "truncate" else target)
    columns = sorted(state.columns[table])
    # Los grupos de cierre salen más a menudo: son los que más se parecen a la excepción.
    group = sorted(CLOSING_COLUMNS.get(table, ()))
    if group and draw(st.booleans()):
        # Un cierre válido más otra columna: el caso que la excepción nunca debe dejar pasar.
        closing = {
            "zone_node_assignment": (("unassigned_at", dt.datetime(2099, 1, 1, tzinfo=dt.UTC)),),
            "role_assignment": (
                ("removed_at", dt.datetime(2099, 1, 1, tzinfo=dt.UTC)),
                ("removed_by", state.seed.operator_id),
            ),
            "provider_concession": (("status", "expired"),),
        }[table]
        extra = draw(st.sampled_from([column for column in columns if column not in group]))
        value = draw(_VALUES[state.columns[table][extra]])
        return Mutation(role, table, kind, target, (*closing, (extra, value)))
    chosen = draw(
        st.one_of(
            st.lists(st.sampled_from(columns), min_size=1, max_size=4, unique=True),
            st.lists(st.sampled_from(group or columns), min_size=1, max_size=4, unique=True),
        )
    )
    assignments = []
    for column in chosen:
        current = None if target is None else target.values.get(column)
        value = draw(
            st.one_of(
                st.none(),
                _VALUES[state.columns[table][column]],
                st.just(current),  # el mismo valor: un UPDATE sin cambio también es UPDATE
            )
        )
        assignments.append((column, _from_json(state.columns[table][column], value)))
    return Mutation(role, table, kind, target, tuple(assignments))


async def _attempt(connection: Any, seed: IdentitySeed, mutation: Mutation) -> str | None:
    """SQLSTATE del rechazo, o ``None`` si la mutación se aplicó (se revierte igual)."""
    sql, arguments = mutation.statement()
    transaction = connection.transaction()
    await transaction.start()
    try:
        await set_scope(connection, seed.a.organization_id)
        await connection.execute(sql, *arguments)
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)
    finally:
        await transaction.rollback()
    return None


def test_mutation_strategy_reaches_every_table_role_and_kind(append_only: AppendOnly) -> None:
    """Control de la estrategia: la base tiene filas abiertas y cerradas donde hay cierre."""
    assert set(append_only.rows) == set(APPEND_ONLY_TABLES)
    assert set(CLOSING_COLUMNS) <= set(APPEND_ONLY_TABLES)


@given(data=st.data())
def test_any_generated_mutation_on_an_append_only_table_fails(
    append_only: AppendOnly,
    connections: dict[str, Any],
    runner: asyncio.Runner,
    data: st.DataObject,
) -> None:
    mutation = data.draw(mutations(append_only))
    rows, types = append_only.rows[mutation.table], append_only.columns[mutation.table]
    assume(not mutation.is_closing(rows, types))
    sqlstate = runner.run(_attempt(connections[mutation.role], append_only.seed, mutation))
    allowed = {RESTRICT_VIOLATION}
    if mutation.role == "vigia_app":
        allowed.add(INSUFFICIENT_PRIVILEGE)
    assert sqlstate in allowed, (mutation.statement(), sqlstate)


# --- Criterio 5 y los otros cierres: una vez, sin tocar nada más -------------------------------


@pytest_asyncio.fixture
async def app(append_only: AppendOnly) -> AsyncIterator[Any]:
    connection = await append_only.database.connect("vigia_app")
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def owner(append_only: AppendOnly) -> AsyncIterator[Any]:
    connection = await append_only.database.connect("vigia_migrate")
    try:
        yield connection
    finally:
        await connection.close()


async def _fails(connection: Any, sql: str, *arguments: Any) -> str:
    try:
        async with connection.transaction():  # punto de guardado dentro de la prueba
            await connection.execute(sql, *arguments)
    except asyncpg.PostgresError as error:
        return str(error.sqlstate)
    raise AssertionError(f"debió fallar: {sql}")


@pytest.mark.asyncio
async def test_unassigned_at_is_set_once_and_nothing_else_changes(
    append_only: AppendOnly, app: Any, owner: Any
) -> None:
    seed = append_only.seed
    plant = seed.a.plants[1]
    assignment = next(
        row.key
        for row in append_only.rows["zone_node_assignment"]
        if row.open and row.values["zone_id"] == str(plant.zone_id)
    )
    close = "UPDATE identity.zone_node_assignment SET unassigned_at = $2 WHERE assignment_id = $1"
    end = BASE_TIME + dt.timedelta(days=3)
    for connection in (app, owner):
        transaction = connection.transaction()
        await transaction.start()
        try:
            await set_scope(connection, seed.a.organization_id)
            # Cierre junto a otra columna: falla (vigia_app, sin privilegio; dueño, disparador).
            both = (
                "UPDATE identity.zone_node_assignment SET unassigned_at = $2, assigned_by = $3"
                " WHERE assignment_id = $1"
            )
            assert await _fails(connection, both, assignment, end, seed.operator_id) in {
                INSUFFICIENT_PRIVILEGE,
                RESTRICT_VIOLATION,
            }
            # Fijar a NULL una marca ya nula no es un cierre.
            assert await _fails(connection, close, assignment, None) == RESTRICT_VIOLATION
            # El cierre: se acepta una vez.
            assert await connection.execute(close, assignment, end) == "UPDATE 1"
            # Volver a cambiarla, reabrirla o repetir el mismo valor: falla.
            for again in (end + dt.timedelta(hours=1), None, end):
                assert await _fails(connection, close, assignment, again) == RESTRICT_VIOLATION
            # Cualquier otra columna de la fila, ya cerrada: falla.
            other = (
                "UPDATE identity.zone_node_assignment SET node_id = node_id"
                " WHERE assignment_id = $1"
            )
            assert await _fails(connection, other, assignment) in {
                INSUFFICIENT_PRIVILEGE,
                RESTRICT_VIOLATION,
            }
            stored = await connection.fetchval(
                "SELECT unassigned_at FROM identity.zone_node_assignment WHERE assignment_id = $1",
                assignment,
            )
            assert stored == end
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_unassigned_at_before_assigned_at_is_rejected(
    append_only: AppendOnly, app: Any
) -> None:
    seed = append_only.seed
    assignment = seed.a.plants[1].assignment_id
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, seed.a.organization_id)
        close = (
            "UPDATE identity.zone_node_assignment SET unassigned_at = $2 WHERE assignment_id = $1"
        )
        assert await _fails(app, close, assignment, BASE_TIME) == "23514"  # rango vacío
        assert await _fails(app, close, assignment, BASE_TIME - dt.timedelta(days=1)) == "23514"
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_role_is_removed_once_with_its_author(append_only: AppendOnly, app: Any) -> None:
    seed = append_only.seed
    assignment = next(row.key for row in append_only.rows["role_assignment"] if row.open)
    remove = (
        "UPDATE identity.role_assignment SET removed_at = $2, removed_by = $3"
        " WHERE assignment_id = $1"
    )
    at = BASE_TIME + dt.timedelta(days=2)
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, seed.a.organization_id)
        # Sin autor, la fila queda incoherente (CHECK); sin fecha no es un cierre.
        assert await _fails(app, remove, assignment, at, None) == "23514"
        assert await _fails(app, remove, assignment, None, seed.operator_id) == RESTRICT_VIOLATION
        assert (
            await _fails(
                app,
                "UPDATE identity.role_assignment SET role = 'copasst' WHERE assignment_id = $1",
                assignment,
            )
            == INSUFFICIENT_PRIVILEGE
        )
        assert await app.execute(remove, assignment, at, seed.operator_id) == "UPDATE 1"
        assert await _fails(app, remove, assignment, at, seed.operator_id) == RESTRICT_VIOLATION
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", ["revoked", "expired"])
async def test_concession_is_closed_once(append_only: AppendOnly, app: Any, closing: str) -> None:
    seed = append_only.seed
    concession = seed.concessions[seed.a.organization_id]
    revoke = (
        "UPDATE identity.provider_concession SET status = 'revoked', revoked_at = now(),"
        " revoked_by = $2, revoked_by_side = 'client' WHERE concession_id = $1"
    )
    expire = "UPDATE identity.provider_concession SET status = 'expired' WHERE concession_id = $1"
    transaction = app.transaction()
    await transaction.start()
    try:
        await set_scope(app, seed.a.organization_id)
        # Revocar sin autor, o marcar revocada sin fecha: la fila sería incoherente (CHECK).
        assert (
            await _fails(
                app,
                "UPDATE identity.provider_concession SET status = 'revoked'"
                " WHERE concession_id = $1",
                concession,
            )
            == "23514"
        )
        # Ampliar el plazo o el alcance: nunca.
        assert (
            await _fails(
                app,
                "UPDATE identity.provider_concession SET expires_at = expires_at + interval '1 day'"
                " WHERE concession_id = $1",
                concession,
            )
            == INSUFFICIENT_PRIVILEGE
        )
        if closing == "revoked":
            assert await app.execute(revoke, concession, seed.a.user_id) == "UPDATE 1"
        else:
            assert await app.execute(expire, concession) == "UPDATE 1"
        # Cerrada una vez: ni reabrir, ni volver a cerrar, ni cambiar de cierre.
        for sql, arguments in (
            (revoke, (concession, seed.a.user_id)),
            (expire, (concession,)),
            (
                "UPDATE identity.provider_concession SET status = 'active', revoked_at = NULL,"
                " revoked_by = NULL, revoked_by_side = NULL WHERE concession_id = $1",
                (concession,),
            ),
        ):
            assert await _fails(app, sql, *arguments) == RESTRICT_VIOLATION
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "setters"),
    [
        (
            "zone_node_assignment",
            "unassigned_at = '2099-01-01Z', assigned_at = assigned_at - interval '1 second'",
        ),
        (
            "role_assignment",
            "removed_at = '2099-01-01Z', removed_by = assigned_by, role = 'copasst'",
        ),
        ("provider_concession", "status = 'expired', reason = 'Motivo reescrito del cierre'"),
        ("provider_concession", "status = 'expired', expires_at = expires_at + interval '1 day'"),
    ],
)
async def test_closing_together_with_another_column_fails_even_for_the_owner(
    append_only: AppendOnly, owner: Any, table: str, setters: str
) -> None:
    """El dueño tiene privilegio sobre todo: solo el disparador impide colar otra columna."""
    seed = append_only.seed
    key = next(row.key for row in append_only.rows[table] if row.open)
    transaction = owner.transaction()
    await transaction.start()
    try:
        await set_scope(owner, seed.a.organization_id)
        sql = f"UPDATE identity.{table} SET {setters} WHERE {PRIMARY_KEYS[table]} = $1"  # noqa: S608
        assert await _fails(owner, sql, key) == RESTRICT_VIOLATION
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_closing_that_rewrites_another_column_with_its_own_value_is_accepted(
    append_only: AppendOnly, owner: Any
) -> None:
    """``SET c = c`` no cambia ninguna otra columna: sigue siendo la actualización de cierre."""
    seed = append_only.seed
    key = next(row.key for row in append_only.rows["zone_node_assignment"] if row.open)
    transaction = owner.transaction()
    await transaction.start()
    try:
        await set_scope(owner, seed.a.organization_id)
        closed = await owner.execute(
            "UPDATE identity.zone_node_assignment"
            " SET unassigned_at = '2099-01-01Z', assigned_at = assigned_at"
            " WHERE assignment_id = $1",
            key,
        )
        assert closed == "UPDATE 1"
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
async def test_delete_and_truncate_fail_even_for_the_owner(
    append_only: AppendOnly, app: Any, owner: Any, table: str
) -> None:
    seed = append_only.seed
    for connection, expected in ((app, INSUFFICIENT_PRIVILEGE), (owner, RESTRICT_VIOLATION)):
        transaction = connection.transaction()
        await transaction.start()
        try:
            await set_scope(connection, seed.a.organization_id)
            assert await connection.fetchval(f"SELECT count(*) FROM identity.{table}")  # noqa: S608
            assert await _fails(connection, f"DELETE FROM identity.{table}") == expected  # noqa: S608
            assert await _fails(connection, f"TRUNCATE identity.{table}") == expected
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["key_set_publication", "live_view_token_issuance", "privacy_notice_acceptance"]
)
async def test_tables_without_end_mark_never_update(
    append_only: AppendOnly, owner: Any, table: str
) -> None:
    """Sin marca de fin no hay cierre: ni el dueño cambia una columna (disparador)."""
    seed = append_only.seed
    transaction = owner.transaction()
    await transaction.start()
    try:
        await set_scope(owner, seed.a.organization_id)
        key = PRIMARY_KEYS[table]
        assert await _fails(owner, f"UPDATE identity.{table} SET {key} = {key}") == (  # noqa: S608
            RESTRICT_VIOLATION
        )
    finally:
        await transaction.rollback()
