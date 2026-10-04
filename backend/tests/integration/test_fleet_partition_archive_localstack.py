"""Archivado de las particiones de ``fleet`` por la lista ampliada de ``archive_audit_partitions``
contra PostgreSQL 16 y LocalStack reales (TASK-203, PAT-GOB-ESC-02, NFR-GOB-16, 39).

Base migrada hasta ``gob_0018``; escritor, auditoría y bandeja reales como ``vigia_app`` (nunca
superusuario); ``vigia-archive`` es un depósito de LocalStack con versionado (sumas SHA-256) y el
archivo se sube cifrado con una clave KMS de LocalStack. El reloj del archivado es el día 3 del
mes en curso de la base (``M``); la prueba crea, como ``vigia_migrate``, las particiones de meses
pasados que necesita y las llena con filas de dos organizaciones:

- ``heartbeat_history``: ``M-4`` (su mes terminó hace más de 90 días: vence), ``M-3`` (aún no) y
  ``M-1`` (el mes anterior).
- ``enrollment_attempt``: ``M-25`` (vence a los 24 meses) y ``M-23`` (sigue en línea).
- ``fleet_alarm``: ``M-25`` con alarmas cerradas (vence) y ``M-26`` con una abierta (vence, pero
  no se archiva mientras siga abierta).

Criterios: la tarea (un solo manejador, ninguna tarea nueva) desprende ``M-4`` de latidos y no toca
la del mes anterior; la de intentos de hace 23 meses sigue adjunta; cada partición desprendida
queda archivada con prefijo por tabla, cifrada, con su ``audit_partition_archived`` en la cadena de
la proveedora y con las mismas filas, restaurables; si la verificación de vuelta falla (un bit
alterado en el almacén) se emite ``security_alert`` y la partición sigue adjunta sin registro.
Guardas de las funciones de gob_0018: solo el sistema lee y desprende, con el recuento verificado,
sin filas posteriores ni alarmas abiertas, y solo particiones adjuntas con el nombre del convenio.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.fleet_db import FleetScope, enrollment_attempt, fleet_alarm, heartbeat
from tests.identity_db import migrated_database, set_scope
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.writer_support import (
    Place,
    WriterEnvironment,
    unit_context,
    verify_ledger_chains,
    writer_environment,
)
from vigia_platform.shared.archive.audit_archive import (
    ARCHIVE_AUDIT_PARTITIONS,
    ARCHIVED_RECORD_TYPE,
    ArchiveFailure,
    AuditArchiveFailed,
    AuditArchiver,
    archive_audit_partitions_handler,
    register_archive_audit_partitions,
)
from vigia_platform.shared.archive.partitions import add_months
from vigia_platform.shared.archive.table_archive import (
    ARCHIVED_TABLES,
    ENROLLMENT_ATTEMPT,
    FLEET_ALARM,
    HEARTBEAT_HISTORY,
    ArchivedTable,
    TablePartition,
    restore_table_partition,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

VERIFIER = b"# vigia_verify.py de prueba\n"
INSUFFICIENT_PRIVILEGE = "42501"
NOT_IN_PREREQUISITE_STATE = "55000"
RESTRICT_VIOLATION = "23001"
ROWS_PER_PARTITION = 3


@dataclass
class World:
    env: WriterEnvironment
    storage: S3Storage
    s3: Any
    bucket: str
    kms_key_id: str
    month: date
    provider: uuid.UUID
    places: tuple[FleetScope, FleetScope]

    def system(self, organization_id: uuid.UUID | None = None) -> ScopeContext:
        return unit_context(organization_id or self.provider, ActorUnit.U02, kind=ActorKind.SYSTEM)

    def partition(self, table: ArchivedTable, back: int) -> TablePartition:
        return TablePartition(table, add_months(self.month, -back))

    def archiver(
        self, storage: Any = None, tables: tuple[ArchivedTable, ...] = ARCHIVED_TABLES
    ) -> AuditArchiver:
        clock = SimulatedClock(datetime(self.month.year, self.month.month, 3, 3, 0, tzinfo=UTC))
        return AuditArchiver(
            database=self.env.database,
            storage=storage or self.storage,
            writer=self.env.writer,
            audit=self.env.audit,
            outbox=self.env.outbox,  # type: ignore[arg-type]
            checkpoint_keys=lambda: (),
            verifier=VERIFIER,
            clock=clock,
            kms_key_id=self.kms_key_id,
            tables=tables,
        )

    def fetch(self, query: str, *args: Any) -> list[Any]:
        async def run() -> list[Any]:
            connection = await self.env.migrated.connect()
            try:
                return list(await connection.fetch(query, *args))
            finally:
                await connection.close()

        return self.env.loop.run(run())

    def attached(self, partition: TablePartition) -> bool:
        rows = self.fetch(
            "SELECT 1 FROM pg_inherits AS inheritance JOIN pg_class AS child"
            " ON child.oid = inheritance.inhrelid"
            " WHERE inheritance.inhparent = $1::regclass AND child.relname = $2",
            partition.table.name,
            partition.name,
        )
        return bool(rows)

    def rows(self, partition: TablePartition) -> list[dict[str, Any]]:
        """Las filas de la tabla (adjunta o desprendida) como documentos de ``to_jsonb``."""
        found = self.fetch(
            "SELECT to_jsonb(t)::text AS row FROM "  # noqa: S608 - nombre fijo de la prueba
            f"{partition.qualified_name} AS t"
        )
        return sorted((json.loads(row["row"]) for row in found), key=json.dumps)

    def archived_records(self) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT content_json, organization_id, schema_version FROM ledger.ledger_record"
            " WHERE record_type = $1",
            ARCHIVED_RECORD_TYPE,
        )
        return [
            {
                **json.loads(row["content_json"]),
                "_organization_id": row["organization_id"],
                "_schema_version": row["schema_version"],
            }
            for row in rows
        ]

    def alerts(self) -> list[Any]:
        return self.fetch(
            "SELECT organization_id, payload FROM shared.outbox_event"
            " WHERE event_name = 'security_alert'"
            " AND payload->>'alert_kind' = 'audit_archive_verification_failed'"
        )

    def failure_audits(self) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT organization_id, outcome, filters_json FROM shared.audit_entry"
            " WHERE operation = 'integrity_verification'"
            " AND filters_json->>'check' = 'audit_archive'"
        )
        return [
            {**json.loads(row["filters_json"]), "_organization_id": row["organization_id"]}
            for row in rows
        ]


def _moment(month: date, day: int = 10) -> datetime:
    return datetime(month.year, month.month, day, 8, 30, tzinfo=UTC)


async def _prepare(env: WriterEnvironment, places: tuple[FleetScope, ...], month: date) -> None:
    """Nodos de los dos lugares, particiones pasadas (como ``vigia_migrate``) y sus filas."""
    connection = await env.migrated.connect()
    try:
        for place in places:
            await connection.execute(
                "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
                " status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
                place.node_id,
                place.organization_id,
                place.plant_id,
                f"ND-{place.node_id.hex[:20].upper()}",
                _moment(add_months(month, -30)),
            )
        partitions = {
            "heartbeat_history": (4, 3, 1),
            "enrollment_attempt": (25, 23),
            "fleet_alarm": (26, 25),
        }
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE vigia_migrate")
            for table, backs in partitions.items():
                for back in backs:
                    start = add_months(month, -back)
                    name = f"{table}_{start.year:04d}_{start.month:02d}"
                    end = add_months(start, 1)
                    await connection.execute(
                        f"CREATE TABLE fleet.{name} PARTITION OF fleet.{table}"
                        f" FOR VALUES FROM ('{start.isoformat()} 00:00+00')"
                        f" TO ('{end.isoformat()} 00:00+00')"
                    )
                    await connection.execute(
                        "SELECT shared.vigia_protect_append_only_partition($1::regclass)",
                        f"fleet.{name}",
                    )
        for place in places:
            async with connection.transaction():
                await set_scope(connection, place.organization_id)
                steps: list[tuple[str, list[Any]]] = []
                for back in partitions["heartbeat_history"]:
                    start = add_months(month, -back)
                    steps += [
                        heartbeat(
                            place,
                            _moment(start, 2 + day),
                            summary={"pending": day, "fps": [12.5, 29.97], "kind": "ntp"},
                        )
                        for day in range(ROWS_PER_PARTITION)
                    ]
                for back in partitions["enrollment_attempt"]:
                    start = add_months(month, -back)
                    steps += [
                        enrollment_attempt(place, _moment(start, 2 + day))
                        for day in range(ROWS_PER_PARTITION)
                    ]
                old = add_months(month, -25)
                steps += [
                    fleet_alarm(place, _moment(old, 2 + day), kind="clock_drift")
                    for day in range(ROWS_PER_PARTITION)
                ]
                steps.append(
                    fleet_alarm(
                        place, _moment(add_months(month, -26)), kind="node_mute", cleared=False
                    )
                )
                for sql, args in steps:
                    await connection.execute(sql, *args)
    finally:
        await connection.close()


@pytest.fixture
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    s3 = localstack_endpoint.aws_client("s3")
    kms_key_id = localstack_endpoint.aws_client("kms").create_key(
        Description="vigia-archive de prueba"
    )["KeyMetadata"]["KeyId"]
    with (
        migrated_database(postgres_endpoint, "vigia_fleet_archive") as migrated,
        writer_environment(migrated) as env,
        versioned_bucket(s3, "vigia-archive") as bucket,
    ):
        assert env.outbox is not None
        storage = S3Storage(localstack_endpoint.storage_settings(bucket), env.clock)
        month = env.loop.run(_database_month(env))
        provider = env.provider_organization_id
        Place.new(provider)
        places = tuple(
            FleetScope(
                place.organization_id, place.plant_id, place.zone_id, place.node_id, uuid.uuid4()
            )
            for place in (Place.new(provider), Place.new())
        )
        env.loop.run(_prepare(env, places, month))
        yield World(env, storage, s3, bucket, kms_key_id, month, provider, (places[0], places[1]))


async def _database_month(env: WriterEnvironment) -> date:
    connection = await env.migrated.connect()
    try:
        value = await connection.fetchval(
            "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"
        )
    finally:
        await connection.close()
    assert isinstance(value, date)
    return value


def run_task(world: World, archiver: AuditArchiver) -> None:
    """La tarea heredada ``archive_audit_partitions``: la lista ampliada no añade ninguna."""
    registry = PeriodicTaskRegistry()
    task = register_archive_audit_partitions(
        registry, archive_audit_partitions_handler(archiver, world.provider)
    )
    assert task.task_name == ARCHIVE_AUDIT_PARTITIONS

    async def once() -> None:
        async with world.env.database.transaction(world.system()) as transaction:
            await task.handler(transaction)

    world.env.loop.run(once())


def sqlstate(error: BaseException | None) -> str | None:
    while error is not None:
        for candidate in (error, getattr(error, "orig", None)):
            code = getattr(candidate, "sqlstate", None)
            if isinstance(code, str):
                return code
        error = error.__cause__
    return None


# --- Plazo por tabla: qué se desprende y qué no -------------------------------------------------


def test_due_partitions_follow_each_table_retention(world: World) -> None:
    due = world.env.loop.run(world.archiver().tables.due_partitions(world.system()))
    assert {(p.table.name, p.month) for p in due} == {
        ("fleet.heartbeat_history", add_months(world.month, -4)),
        ("fleet.enrollment_attempt", add_months(world.month, -25)),
        ("fleet.fleet_alarm", add_months(world.month, -26)),
        ("fleet.fleet_alarm", add_months(world.month, -25)),
    }


def test_the_task_archives_what_is_due_and_nothing_else(world: World) -> None:
    heartbeats_old = world.partition(HEARTBEAT_HISTORY, 4)
    archived = (
        heartbeats_old,
        world.partition(ENROLLMENT_ATTEMPT, 25),
        world.partition(FLEET_ALARM, 25),
    )
    kept = (
        world.partition(HEARTBEAT_HISTORY, 3),
        world.partition(HEARTBEAT_HISTORY, 1),  # el mes anterior
        world.partition(ENROLLMENT_ATTEMPT, 23),  # hace 23 meses
        world.partition(FLEET_ALARM, 26),  # vencida, con una alarma abierta
        *(world.partition(table, -step) for table in ARCHIVED_TABLES for step in range(4)),
    )
    before = {partition: world.rows(partition) for partition in (*archived, *kept)}
    for partition in archived:
        assert len(before[partition]) == 2 * ROWS_PER_PARTITION, partition

    run_task(world, world.archiver())

    for partition in archived:
        assert not world.attached(partition), partition
        # Desprendida, no borrada: las mismas filas.
        assert world.rows(partition) == before[partition], partition
    for partition in kept:
        assert world.attached(partition), partition
        assert world.rows(partition) == before[partition], partition

    records = {record["partition_name"]: record for record in world.archived_records()}
    assert set(records) == {partition.qualified_name for partition in archived}
    for partition in archived:
        record = records[partition.qualified_name]
        assert record["_organization_id"] == world.provider
        assert record["_schema_version"] == 2
        assert record["period"] == partition.period
        assert record["entry_count"] == 2 * ROWS_PER_PARTITION
        key = record["archive_object_key"]
        assert key == f"fleet/{partition.table.short_name}/{partition.period}/{partition.name}.zip"
        stored = world.s3.get_object(Bucket=world.bucket, Key=key)
        body = stored["Body"].read()
        assert hashlib.sha256(body).hexdigest() == record["archive_sha256"]
        assert stored["ServerSideEncryption"] == "aws:kms"
        assert world.kms_key_id in stored["SSEKMSKeyId"]
        # Restauración de solo lectura: las mismas filas, columna a columna.
        restored = world.env.loop.run(
            restore_table_partition(world.storage, key, record["archive_sha256"])
        )
        assert sorted(restored.rows, key=json.dumps) == before[partition]
    world.env.loop.run(verify_ledger_chains(world.env.migrated, world.provider))
    assert world.alerts() == []

    # La tabla desprendida rechaza UPDATE, DELETE y TRUNCATE aun como su dueño.
    async def mutate(statement: str) -> str | None:
        connection = await world.env.migrated.connect("vigia_migrate")
        try:
            await connection.execute(statement)
        except Exception as error:
            return sqlstate(error)
        finally:
            await connection.close()
        return None

    table = heartbeats_old.qualified_name
    for statement in (
        f"DELETE FROM {table}",  # noqa: S608
        f"UPDATE {table} SET payload_summary = '{{}}'",  # noqa: S608
        f"TRUNCATE {table}",
    ):
        assert world.env.loop.run(mutate(statement)) == RESTRICT_VIOLATION, statement

    # Una segunda pasada no archiva nada más.
    run_task(world, world.archiver())
    assert len(world.archived_records()) == len(archived)


def test_closing_the_last_open_alarm_lets_its_partition_go(world: World) -> None:
    open_partition = world.partition(FLEET_ALARM, 26)
    run_task(world, world.archiver(tables=(FLEET_ALARM,)))
    assert world.attached(open_partition)

    async def clear() -> None:
        connection = await world.env.migrated.connect("vigia_app")
        try:
            for place in world.places:
                async with connection.transaction():
                    await set_scope(connection, place.organization_id)
                    await connection.execute(
                        "UPDATE fleet.fleet_alarm SET cleared_at = raised_at + interval '1 hour',"
                        " cleared_event_id = gen_random_uuid()"
                        " WHERE node_id = $1 AND cleared_at IS NULL",
                        place.node_id,
                    )
        finally:
            await connection.close()

    world.env.loop.run(clear())
    run_task(world, world.archiver(tables=(FLEET_ALARM,)))
    assert not world.attached(open_partition)


# --- Verificación de vuelta fallida -------------------------------------------------------------


class CorruptingStorage:
    """Sube de verdad y después altera un bit del objeto en el almacén (como un fallo del medio)."""

    def __init__(self, world: World, position: float, bit: int) -> None:
        self._world = world
        self._position = position
        self._bit = bit

    async def put_object(self, key: str, body: bytes, content_type: str, **kwargs: Any) -> Any:
        head = await self._world.storage.put_object(key, body, content_type, **kwargs)
        flipped = bytearray(body)
        flipped[int((len(body) - 1) * self._position)] ^= 1 << self._bit
        self._world.s3.put_object(Bucket=self._world.bucket, Key=key, Body=bytes(flipped))
        return head

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return await self._world.storage.get_object(key, version_id=version_id)


@pytest.mark.parametrize(("position", "bit"), [(0.0, 0), (0.5, 3), (1.0, 7)])
def test_failed_verification_alerts_and_keeps_the_partition_attached(
    world: World, position: float, bit: int
) -> None:
    partition = world.partition(HEARTBEAT_HISTORY, 4)
    before = world.rows(partition)
    archiver = world.archiver(
        storage=CorruptingStorage(world, position, bit), tables=(HEARTBEAT_HISTORY,)
    )
    with pytest.raises(AuditArchiveFailed) as raised:
        run_task(world, archiver)
    assert raised.value.partitions == (partition.qualified_name,)
    assert world.attached(partition)
    assert world.rows(partition) == before
    assert world.archived_records() == []
    assert [alert["organization_id"] for alert in world.alerts()] == [world.provider]
    audits = world.failure_audits()
    assert [(a["partition"], a["failure_reason"], a["_organization_id"]) for a in audits] == [
        (partition.qualified_name, ArchiveFailure.DIGEST_MISMATCH.value, world.provider)
    ]
    # La pasada siguiente, con el almacén sano, sí lo archiva (una sola vez).
    run_task(world, world.archiver(tables=(HEARTBEAT_HISTORY,)))
    assert not world.attached(partition)
    assert len(world.archived_records()) == 1


class WritingStorage:
    """Almacén real que, al leer de vuelta, deja un latido nuevo en la partición: algo escribió
    entre la exportación y el desprendimiento."""

    def __init__(self, world: World, partition: TablePartition) -> None:
        self._world = world
        self._partition = partition

    async def put_object(self, key: str, body: bytes, content_type: str, **kwargs: Any) -> Any:
        return await self._world.storage.put_object(key, body, content_type, **kwargs)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        place = self._world.places[1]
        connection = await self._world.env.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await set_scope(connection, place.organization_id)
                sql, args = heartbeat(place, _moment(self._partition.month, 20))
                await connection.execute(sql, *args)
        finally:
            await connection.close()
        return await self._world.storage.get_object(key, version_id=version_id)


def test_a_row_written_after_export_rejects_the_detach(world: World) -> None:
    partition = world.partition(HEARTBEAT_HISTORY, 4)
    with pytest.raises(AuditArchiveFailed):
        run_task(
            world,
            world.archiver(storage=WritingStorage(world, partition), tables=(HEARTBEAT_HISTORY,)),
        )
    assert world.attached(partition)
    assert world.archived_records() == []
    reasons = [audit["failure_reason"] for audit in world.failure_audits()]
    assert reasons == [ArchiveFailure.PARTITION_CHANGED.value]


# --- Guardas de las funciones de gob_0018 -------------------------------------------------------


def _call(world: World, context: ScopeContext, statement: str, **parameters: Any) -> str | None:
    async def run() -> str | None:
        try:
            async with world.env.database.transaction(context) as transaction:
                await transaction.execute(text(statement), parameters)
        except Exception as error:
            return sqlstate(error) or type(error).__name__
        return None

    return world.env.loop.run(run())


def test_only_the_system_reads_or_detaches_a_fleet_partition(world: World) -> None:
    partition = world.partition(HEARTBEAT_HISTORY, 4)
    for kind in (ActorKind.USER, ActorKind.OPERATOR, ActorKind.PROVIDER_USER, ActorKind.NODE):
        context = unit_context(world.provider, ActorUnit.U02, kind=kind)
        for statement in (
            "SELECT * FROM shared.vigia_table_partition_summary(:t, :p)",
            "SELECT * FROM shared.vigia_table_partition_rows(:t, :p, NULL, NULL, 10)",
            "SELECT shared.vigia_detach_table_partition(:t, :p, 0, now())",
        ):
            assert (
                _call(world, context, statement, t=partition.table.name, p=partition.name)
                == INSUFFICIENT_PRIVILEGE
            ), (kind, statement)
    assert world.attached(partition)


def test_detach_requires_the_verified_count_nothing_newer_and_no_open_alarm(world: World) -> None:
    partition = world.partition(HEARTBEAT_HISTORY, 4)
    count = len(world.rows(partition))
    statement = "SELECT shared.vigia_detach_table_partition(:t, :p, :n, :e)"
    table, name = partition.table.name, partition.name
    end = partition.range_end
    for rows, not_after in (
        (count - 1, end),
        (count + 1, end),
        (count, _moment(partition.month, 3)),
    ):
        assert (
            _call(world, world.system(), statement, t=table, p=name, n=rows, e=not_after)
            == NOT_IN_PREREQUISITE_STATE
        )
    assert world.attached(partition)
    open_alarms = world.partition(FLEET_ALARM, 26)
    alarms = len(world.rows(open_alarms))
    assert (
        _call(
            world,
            world.system(),
            statement,
            t=open_alarms.table.name,
            p=open_alarms.name,
            n=alarms,
            e=open_alarms.range_end,
        )
        == NOT_IN_PREREQUISITE_STATE
    )
    assert world.attached(open_alarms)


def test_only_attached_fleet_partitions_with_the_convention_are_touched(world: World) -> None:
    for table, name in (
        ("fleet.heartbeat_history", "heartbeat_history_default"),
        ("fleet.heartbeat_history", world.partition(FLEET_ALARM, 25).name),
        ("fleet.heartbeat_history", "heartbeat_history_2026_13"),
        ("fleet.heartbeat_history", "heartbeat_history_1999_01"),
        ("fleet.node_inventory", "node_inventory_2026_01"),
        ("shared.audit_entry", f"audit_entry_{world.month.year:04d}_{world.month.month:02d}"),
        ("fleet.heartbeat_history", "x; DROP"),
        (None, world.partition(HEARTBEAT_HISTORY, 4).name),
    ):
        for statement in (
            "SELECT * FROM shared.vigia_table_partition_rows(:t, :p, NULL, NULL, 10)",
            "SELECT shared.vigia_detach_table_partition(:t, :p, 0, now())",
        ):
            assert _call(world, world.system(), statement, t=table, p=name) in (
                "22023",
                "42P01",
            ), (table, name, statement)
    assert world.attached(world.partition(HEARTBEAT_HISTORY, 4))


def test_the_retention_of_each_table_is_the_designed_one() -> None:
    """NFR-GOB-16 y 39: 90 días de latidos; 24 meses de intentos y alarmas."""
    assert (HEARTBEAT_HISTORY.online_days, HEARTBEAT_HISTORY.online_months) == (90, None)
    assert (ENROLLMENT_ATTEMPT.online_days, ENROLLMENT_ATTEMPT.online_months) == (None, 24)
    assert (FLEET_ALARM.online_days, FLEET_ALARM.online_months) == (None, 24)
    now = datetime(2027, 4, 3, 3, 0, tzinfo=UTC)
    # Diciembre terminó el 1 de enero: 92 días antes del 3 de abril; enero, 62.
    assert HEARTBEAT_HISTORY.is_due(date(2026, 12, 1), now)
    assert not HEARTBEAT_HISTORY.is_due(date(2027, 1, 1), now)
    # El borde exacto: el mes terminó hace 90 días justos.
    edge = datetime(2027, 1, 1, tzinfo=UTC) + timedelta(days=90)
    assert HEARTBEAT_HISTORY.is_due(date(2026, 12, 1), edge)
    assert not HEARTBEAT_HISTORY.is_due(date(2026, 12, 1), edge - timedelta(milliseconds=1))
    assert ENROLLMENT_ATTEMPT.is_due(date(2025, 3, 1), now)
    assert not ENROLLMENT_ATTEMPT.is_due(date(2025, 4, 1), now)
