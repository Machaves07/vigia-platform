"""PR-GOB-26: ida y vuelta del archivo de las particiones de ``fleet`` y bit alterado (TASK-203).

PR-GOB-26: «para cualquier partición generada de ``HeartbeatHistory``, ``EnrollmentAttempt`` o
``FleetAlarm``, ``restaurar(exportar(p)) = p`` en recuento y contenido; con un bit alterado en el
archivo la partición **sigue adjunta** y no se registra como archivada; ninguna migración generada
contiene ``DROP TABLE``, ``TRUNCATE`` ni ``DELETE`` sobre tabla de solo anexar» (PAT-GOB-ESC-02).

- **Contra la base** (``test_pr_gob_26_round_trip_and_flipped_bit_on_a_real_partition``): por
  ejemplo, una partición mensual nueva de una de las tres tablas, llenada como ``vigia_app`` con
  filas generadas (de dos organizaciones, con resúmenes JSON difíciles, nodos conocidos y
  desconocidos, alarmas de varias clases ya cerradas). ``TableArchiver.snapshot`` (las funciones de
  gob_0018) → ``build_table_archive`` → ``read_table_archive`` → ``restore_table_rows`` es
  exactamente lo que devuelve ``to_jsonb`` de cada fila de la partición. Después, el archivado con
  un almacén que altera un bit generado del objeto: falla, la partición sigue adjunta y no hay
  ``audit_partition_archived``; con el almacén sano se desprende y queda un registro.
- **Sin base**: cualquier bit alterado de un archivo hace fallar ``verify_table_download``; un
  archivo reescrito de forma coherente (SHA-256 y manifiesto recalculados) con otra fila no pasa la
  comparación con la base; bytes arbitrarios solo dan ``ArchiveVerificationFailed``.
- **Metapropiedad de las migraciones** (además de MIG002 del lint): ninguna migración ``gob_*``
  contiene ``DROP TABLE``, ``DELETE FROM`` ni un ``TRUNCATE`` que no sea el evento de un
  disparador; y cualquier migración generada con una de esas sentencias sobre una tabla ⛓ de
  ``fleet`` o una de sus particiones la rechaza el lint con MIG002.

Perfil ``ci`` de Hypothesis; la semilla queda registrada en la salida (``tests/conftest.py``).
Solo datos generados.
"""

from __future__ import annotations

import ast
import hashlib
import io
import itertools
import json
import re
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fleet_db import FleetScope, enrollment_attempt, exact, fleet_alarm, heartbeat
from tests.fleet_db import json_order as _order
from tests.identity_db import set_scope
from tests.integration.conftest import PostgresEndpoint
from tests.properties.envelope_strategies import texts
from tests.writer_support import Place, WriterEnvironment, unit_context, writer_environment
from tools.lint_migrations import check_directory, load_registry
from vigia_platform.shared.archive.audit_archive import ARCHIVED_RECORD_TYPE
from vigia_platform.shared.archive.errors import ArchiveFailure, ArchiveVerificationFailed
from vigia_platform.shared.archive.partitions import add_months
from vigia_platform.shared.archive.table_archive import (
    ARCHIVED_TABLES,
    ArchivedTable,
    PartitionStillOpen,
    TableArchiver,
    TablePartition,
    TableSnapshot,
    build_table_archive,
    read_table_archive,
    restore_table_rows,
    verify_table_archive,
    verify_table_download,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext

BACKEND = Path(__file__).resolve().parents[3]
VERSIONS = BACKEND / "migrations" / "versions"
EXPORTED_AT = datetime(2031, 7, 2, 3, 0, tzinfo=UTC)
FIRST_MONTH = date(1901, 1, 1)
"""Las particiones de los ejemplos empiezan aquí: meses que ninguna otra prueba crea."""
ALARM_KINDS = (
    "node_mute",
    "queue_over_threshold",
    "clock_drift",
    "version_retiring",
    "simulated_adapter_in_productive",
    "certificate_expiring",
    "camera_below_min_fps",
    "orphan_clips_growing",
)

_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    texts(max_size=16),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(texts(max_size=10), children, max_size=3),
    ),
    max_leaves=8,
)
_summaries = st.dictionaries(
    st.from_regex(r"[a-z_]{1,12}", fullmatch=True), _json_values, max_size=5
)


# --- Sin base: formato, bit alterado y archivos hostiles -----------------------------------------


@st.composite
def snapshots(draw: st.DrawFn) -> TableSnapshot:
    """Una partición generada como la devuelve la base: una línea JSON de objeto por fila."""
    table = draw(st.sampled_from(ARCHIVED_TABLES))
    month = date(draw(st.integers(1990, 2090)), draw(st.integers(1, 12)), 1)
    rows = draw(
        st.lists(
            st.dictionaries(texts(max_size=12), _json_values, min_size=1, max_size=6),
            max_size=8,
        )
    )
    lines = tuple(
        json.dumps(row, ensure_ascii=draw(st.booleans()), separators=(", ", ": ")).encode("utf-8")
        for row in rows
    )
    return TableSnapshot(TablePartition(table, month), lines)


@given(snapshots())
def test_pr_gob_26_restore_of_export_is_the_partition(snapshot: TableSnapshot) -> None:
    data = build_table_archive(snapshot, EXPORTED_AT)
    contents = verify_table_download(data, hashlib.sha256(data).hexdigest(), snapshot)
    assert contents.rows == snapshot.rows
    restored = restore_table_rows(contents)
    assert len(restored) == snapshot.row_count
    assert list(restored) == [exact(row) for row in snapshot.rows]


@given(snapshots(), st.data())
def test_pr_gob_26_any_flipped_bit_fails_the_verification(
    snapshot: TableSnapshot, data: st.DataObject
) -> None:
    archive = build_table_archive(snapshot, EXPORTED_AT)
    position = data.draw(st.integers(0, len(archive) - 1))
    bit = data.draw(st.integers(0, 7))
    flipped = bytearray(archive)
    flipped[position] ^= 1 << bit
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_table_download(bytes(flipped), hashlib.sha256(archive).hexdigest(), snapshot)
    assert raised.value.reason is ArchiveFailure.DIGEST_MISMATCH


def _rezip(archive: bytes, rows: bytes) -> bytes:
    """El mismo archivo con otras filas y el manifiesto recalculado (alteración coherente)."""
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        manifest = json.loads(source.read("archive.json"))
    manifest["rows_sha256"] = hashlib.sha256(rows).hexdigest()
    manifest["row_count"] = rows.count(b"\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        target.writestr("archive.json", json.dumps(manifest).encode("utf-8"))
        target.writestr("rows.jsonl", rows)
    return buffer.getvalue()


@given(snapshots(), st.data())
def test_a_consistently_rewritten_archive_never_passes_with_other_rows(
    snapshot: TableSnapshot, data: st.DataObject
) -> None:
    archive = build_table_archive(snapshot, EXPORTED_AT)
    extra = json.dumps({"x": data.draw(_json_values)}).encode("utf-8")
    rows = list(snapshot.rows)
    change = data.draw(st.sampled_from(["append", "drop", "replace"] if rows else ["append"]))
    if change == "append":
        rows.append(extra)
    elif change == "drop":
        rows.pop(data.draw(st.integers(0, len(rows) - 1)))
    else:
        index = data.draw(st.integers(0, len(rows) - 1))
        if rows[index] == extra:
            extra = b'{"x": "otra"}'
        rows[index] = extra
    rewritten = _rezip(archive, b"".join(row + b"\n" for row in rows))
    contents = read_table_archive(rewritten)
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_table_archive(contents, snapshot)
    assert raised.value.reason in {ArchiveFailure.COUNT_MISMATCH, ArchiveFailure.ENTRY_MISMATCH}


@given(st.binary(max_size=4096))
def test_arbitrary_bytes_only_raise_verification_failed(data: bytes) -> None:
    with pytest.raises(ArchiveVerificationFailed):
        read_table_archive(data)


def test_hostile_archives_are_rejected() -> None:
    snapshot = TableSnapshot(
        TablePartition(ARCHIVED_TABLES[0], date(2026, 1, 1)), (b'{"a": 1}', b'{"b": [2]}')
    )
    archive = build_table_archive(snapshot, EXPORTED_AT)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        members = {name: source.read(name) for name in source.namelist()}

    def zipped(items: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as target:
            for name, body in items.items():
                target.writestr(name, body)
        return buffer.getvalue()

    manifest = json.loads(members["archive.json"])
    hostile = {
        "miembro de más": {**members, "../fuera.txt": b"x"},
        "sin filas": {"archive.json": members["archive.json"]},
        "manifiesto con campo de más": {
            **members,
            "archive.json": json.dumps({**manifest, "extra": 1}).encode(),
        },
        "otro formato": {
            **members,
            "archive.json": json.dumps({**manifest, "format": "vigia-audit-archive"}).encode(),
        },
        "fila que no es objeto": {
            "archive.json": json.dumps(
                {
                    **manifest,
                    "rows_sha256": hashlib.sha256(b"[1]\n").hexdigest(),
                    "row_count": 1,
                }
            ).encode(),
            "rows.jsonl": b"[1]\n",
        },
        "recuento falso": {
            **members,
            "archive.json": json.dumps({**manifest, "row_count": 3}).encode(),
        },
    }
    for name, items in hostile.items():
        with pytest.raises(ArchiveVerificationFailed):
            read_table_archive(zipped(items))
        assert name


def test_regression_large_numbers_are_restored_exactly() -> None:
    """Contraejemplo reducido de la propiedad contra la base: un ``numeric`` de ``jsonb`` que no
    cabe en un doble (``88967200801719700``, ``1e+400``) se restauraba como doble y perdía
    cifras."""
    snapshot = TableSnapshot(
        TablePartition(ARCHIVED_TABLES[0], date(1952, 4, 1)),
        (b'{"a": 88967200801719700, "b": 1e+400, "c": 0.1000000000000000055511151231257827}',),
    )
    data = build_table_archive(snapshot, EXPORTED_AT)
    (restored,) = restore_table_rows(read_table_archive(data))
    assert restored == exact(snapshot.rows[0])
    assert restored["a"] == 88967200801719700
    assert str(restored["b"]) == "1E+400"


# --- Contra la base: partición real, ida y vuelta, bit alterado ---------------------------------


@dataclass
class World:
    env: WriterEnvironment
    places: tuple[FleetScope, FleetScope]
    provider: uuid.UUID
    months: Iterator[int]

    def system(self) -> ScopeContext:
        return unit_context(self.provider, ActorUnit.U02, kind=ActorKind.SYSTEM)

    def archiver(self, storage: Any) -> TableArchiver:
        return TableArchiver(
            database=self.env.database,
            storage=storage,
            writer=self.env.writer,
            clock=SimulatedClock(EXPORTED_AT),
        )

    def fetch(self, query: str, *args: Any) -> list[Any]:
        async def run() -> list[Any]:
            connection = await self.env.migrated.connect()
            try:
                return list(await connection.fetch(query, *args))
            finally:
                await connection.close()

        return self.env.loop.run(run())


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    from tests.identity_db import migrated_database

    with (
        migrated_database(postgres_endpoint, "vigia_pr_gob_26") as migrated,
        writer_environment(migrated) as env,
    ):
        provider = env.provider_organization_id
        Place.new(provider)
        places = tuple(
            FleetScope(p.organization_id, p.plant_id, p.zone_id, p.node_id, uuid.uuid4())
            for p in (Place.new(provider), Place.new())
        )

        async def nodes() -> None:
            connection = await migrated.connect()
            try:
                for place in places:
                    await connection.execute(
                        "INSERT INTO identity.node_identity (node_id, organization_id, plant_id,"
                        " code, status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                        place.node_id,
                        place.organization_id,
                        place.plant_id,
                        f"ND-{place.node_id.hex[:20].upper()}",
                        EXPORTED_AT,
                    )
            finally:
                await connection.close()

        env.loop.run(nodes())
        yield World(env, (places[0], places[1]), provider, itertools.count())


class MemoryStorage:
    """Almacén en memoria; con ``flip``, altera ese bit del objeto al guardarlo (un fallo del
    medio)."""

    def __init__(self, flip: tuple[float, int] | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self._flip = flip

    async def put_object(self, key: str, body: bytes, content_type: str, **_: Any) -> None:
        stored = bytearray(body)
        if self._flip is not None:
            position, bit = self._flip
            stored[int((len(stored) - 1) * position)] ^= 1 << bit
        self.objects[key] = bytes(stored)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return self.objects[key]


@dataclass(frozen=True)
class GeneratedRow:
    place: int
    offset: timedelta
    known_node: bool
    kind: str
    summary: dict[str, Any]


_rows = st.lists(
    st.builds(
        GeneratedRow,
        place=st.integers(0, 1),
        offset=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=27, hours=23)),
        known_node=st.booleans(),
        kind=st.sampled_from(ALARM_KINDS),
        summary=_summaries,
    ),
    max_size=12,
)


def _new_partition(world: World, table: ArchivedTable) -> TablePartition:
    """Una partición mensual nueva (como ``vigia_migrate``), protegida como las de la tarea."""
    month = add_months(FIRST_MONTH, next(world.months))
    partition = TablePartition(table, month)
    end = add_months(month, 1)

    async def create() -> None:
        connection = await world.env.migrated.connect()
        try:
            async with connection.transaction():
                await connection.execute("SET LOCAL ROLE vigia_migrate")
                await connection.execute(
                    f"CREATE TABLE {partition.qualified_name} PARTITION OF {table.name}"
                    f" FOR VALUES FROM ('{month.isoformat()} 00:00+00')"
                    f" TO ('{end.isoformat()} 00:00+00')"
                )
                await connection.execute(
                    "SELECT shared.vigia_protect_append_only_partition($1::regclass)",
                    partition.qualified_name,
                )
        finally:
            await connection.close()

    world.env.loop.run(create())
    return partition


def _fill(world: World, partition: TablePartition, rows: list[GeneratedRow]) -> None:
    start = datetime(partition.month.year, partition.month.month, 1, tzinfo=UTC)

    async def insert() -> None:
        connection = await world.env.migrated.connect("vigia_app")
        try:
            for row in rows:
                place = world.places[row.place]
                at = start + row.offset
                async with connection.transaction():
                    await set_scope(connection, place.organization_id)
                    if partition.table.name == "fleet.heartbeat_history":
                        sql, args = heartbeat(place, at, summary=row.summary)
                    elif partition.table.name == "fleet.enrollment_attempt":
                        sql, args = enrollment_attempt(place, at, known_node=row.known_node)
                    else:
                        sql, args = fleet_alarm(place, at, kind=row.kind)
                    await connection.execute(sql, *args)
        finally:
            await connection.close()

    world.env.loop.run(insert())


def _attached(world: World, partition: TablePartition) -> bool:
    return bool(
        world.fetch(
            "SELECT 1 FROM pg_inherits AS inheritance JOIN pg_class AS child"
            " ON child.oid = inheritance.inhrelid"
            " WHERE inheritance.inhparent = $1::regclass AND child.relname = $2",
            partition.table.name,
            partition.name,
        )
    )


def _database_rows(world: World, partition: TablePartition) -> list[dict[str, Any]]:
    found = world.fetch(
        f"SELECT to_jsonb(t)::text AS row FROM {partition.qualified_name} AS t"  # noqa: S608
    )
    return sorted((exact(row["row"]) for row in found), key=_order)


def _records(world: World, partition: TablePartition) -> list[Any]:
    return world.fetch(
        "SELECT content_json FROM ledger.ledger_record WHERE record_type = $1"
        " AND content_json->>'partition_name' = $2",
        ARCHIVED_RECORD_TYPE,
        partition.qualified_name,
    )


@pytest.mark.integration
@given(
    table=st.sampled_from(ARCHIVED_TABLES),
    rows=_rows,
    flip=st.tuples(st.floats(min_value=0, max_value=1), st.integers(0, 7)),
)
def test_pr_gob_26_round_trip_and_flipped_bit_on_a_real_partition(
    world: World, table: ArchivedTable, rows: list[GeneratedRow], flip: tuple[float, int]
) -> None:
    partition = _new_partition(world, table)
    _fill(world, partition, rows)
    expected = _database_rows(world, partition)
    assert len(expected) == len(rows)

    # restaurar(exportar(p)) = p, en recuento y contenido.
    snapshot = world.env.loop.run(
        world.archiver(MemoryStorage()).snapshot(world.system(), partition)
    )
    archive = build_table_archive(snapshot, EXPORTED_AT)
    restored = restore_table_rows(read_table_archive(archive))
    assert len(restored) == len(expected)
    assert sorted(restored, key=_order) == expected

    # Un bit alterado: la partición sigue adjunta y no se registra como archivada.
    with pytest.raises(ArchiveVerificationFailed):
        world.env.loop.run(world.archiver(MemoryStorage(flip)).archive(world.system(), partition))
    assert _attached(world, partition)
    assert _records(world, partition) == []
    assert _database_rows(world, partition) == expected

    # Con el almacén sano: se desprende, con su registro y las mismas filas.
    storage = MemoryStorage()
    archived = world.env.loop.run(world.archiver(storage).archive(world.system(), partition))
    assert not _attached(world, partition)
    assert archived.entry_count == len(expected)
    assert len(_records(world, partition)) == 1
    stored = read_table_archive(storage.objects[partition.object_key])
    assert sorted(restore_table_rows(stored), key=_order) == _database_rows(world, partition)


@pytest.mark.integration
def test_an_open_alarm_keeps_its_partition_from_being_archived(world: World) -> None:
    partition = _new_partition(world, ARCHIVED_TABLES[2])
    start = datetime(partition.month.year, partition.month.month, 3, tzinfo=UTC)

    async def raise_open() -> None:
        connection = await world.env.migrated.connect("vigia_app")
        try:
            place = world.places[0]
            async with connection.transaction():
                await set_scope(connection, place.organization_id)
                sql, args = fleet_alarm(place, start, kind="certificate_expiring", cleared=False)
                await connection.execute(sql, *args)
        finally:
            await connection.close()

    world.env.loop.run(raise_open())
    with pytest.raises(PartitionStillOpen):
        world.env.loop.run(world.archiver(MemoryStorage()).archive(world.system(), partition))
    assert _attached(world, partition)
    assert _records(world, partition) == []


# --- Metapropiedad de las migraciones ------------------------------------------------------------

_STATEMENT = re.compile(r"\b(DROP\s+TABLE|DELETE\s+FROM|TRUNCATE)\b", re.IGNORECASE)
_TRIGGER_EVENT = re.compile(r"\b(BEFORE|AFTER|OR)\s+$", re.IGNORECASE)


def _sql_strings(path: Path) -> Iterator[str]:
    """Cada literal de cadena de la migración (también las partes de las f-strings), sin las
    docstrings ni los comentarios de SQL."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield re.sub(r"--[^\n]*", " ", node.value)


def test_no_gob_migration_drops_truncates_or_deletes() -> None:
    migrations = sorted(VERSIONS.glob("gob_*.py"))
    assert [path.name for path in migrations][:2] == [
        "gob_0017_catalog_schema.py",
        "gob_0018_fleet_schema.py",
    ]
    for path in migrations:
        for sql in _sql_strings(path):
            for match in _STATEMENT.finditer(sql):
                is_trigger_event = match.group(1).upper() == "TRUNCATE" and _TRIGGER_EVENT.search(
                    sql[: match.start()]
                )
                assert is_trigger_event, f"{path.name}: {sql[match.start() : match.start() + 80]!r}"


_APPEND_ONLY_FLEET = (
    "fleet.enrollment_attempt",
    "fleet.heartbeat_history",
    "fleet.fleet_alarm",
    "fleet.target_version_publication",
    "fleet.update_result",
    "fleet.verification_clip",
)
_PARTITIONED_FLEET = _APPEND_ONLY_FLEET[:3]


@st.composite
def destructive_statements(draw: st.DrawFn) -> str:
    table = draw(st.sampled_from(_APPEND_ONLY_FLEET))
    if table in _PARTITIONED_FLEET and draw(st.booleans()):
        suffix = draw(
            st.one_of(
                st.just("default"),
                st.builds("{:04d}_{:02d}".format, st.integers(1990, 2099), st.integers(1, 12)),
            )
        )
        table = f"{table}_{suffix}"
    if draw(st.booleans()):
        schema, name = table.split(".")
        table = f'"{schema}"."{name}"' if draw(st.booleans()) else f'{schema}."{name}"'
    template = draw(
        st.sampled_from(
            (
                "DROP TABLE {t}",
                "DROP TABLE IF EXISTS {t} CASCADE",
                "TRUNCATE {t}",
                "TRUNCATE TABLE ONLY {t}",
                "DELETE FROM {t} WHERE true",
                "DELETE FROM ONLY {t}",
            )
        )
    )
    statement = template.format(t=table)
    return statement.lower() if draw(st.booleans()) else statement


@given(destructive_statements())
def test_any_generated_migration_with_a_destructive_statement_is_rejected(statement: str) -> None:
    registry = load_registry()
    # El eslabón siguiente a la cabeza de la cadena: el lint exige que el número sea la posición.
    chain = sorted(VERSIONS.glob("*_[0-9][0-9][0-9][0-9]_*.py"), key=lambda p: p.name[4:8])
    head = chain[-1].name[:8]
    following = f"gob_{int(head[4:]) + 1:04d}"
    with tempfile.TemporaryDirectory() as directory:
        versions = Path(directory)
        for path in sorted(VERSIONS.glob("*.py")):
            (versions / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        (versions / f"{following}_generated.py").write_text(
            "\n".join(
                [
                    '"""Migración generada."""',
                    "from alembic import op",
                    f"revision = '{following}'",
                    f"down_revision = '{head}'",
                    "branch_labels = None",
                    "depends_on = None",
                    "",
                    "def upgrade() -> None:",
                    f"    op.execute({statement!r})",
                    "",
                    "def downgrade() -> None:",
                    "    raise NotImplementedError('solo hacia adelante')",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        violations = check_directory(versions, registry)
    assert [violation.rule for violation in violations] == ["MIG002"], (statement, violations)
