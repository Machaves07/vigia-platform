"""``archive_audit_partitions`` contra PostgreSQL 16 y LocalStack reales (TASK-131, PAT-NUC-MAN-03).

Base migrada hasta ``nuc_0016``; escritor, auditoría, bandeja y puntos de control reales como
``vigia_app`` (nunca superusuario); ``vigia-archive`` es un depósito de LocalStack con versionado y
el archivo se sube cifrado con una clave KMS de LocalStack. Las entradas de auditoría se escriben
**ahora** (el disparador fija ``occurred_at`` con la hora de la base), así que caen en la partición
del mes en curso; el reloj del archivado va 25 meses por delante para que esa partición venza.

- **PR-NUC-56, ida y vuelta**: la partición del mes se exporta, se lee de vuelta, se verifica, se
  desprende y queda ``audit_partition_archived`` en la cadena de expediente de la proveedora; la
  restauración de solo lectura devuelve exactamente las filas de la tabla desprendida (columnas y
  hashes), que sigue siendo de solo anexar. Una segunda pasada no hace nada.
- **Criterio 2 (bit alterado)**: si el objeto del almacén tiene un bit alterado al leerlo de vuelta,
  la partición sigue adjunta, no se escribe ``audit_partition_archived`` y quedan la alerta
  ``security_alert`` y la entrada ``integrity_verification`` con ``error`` en la proveedora.
- **Atomicidad**: si el escritor rechaza el registro, el desprendimiento se deshace con él.
- **Fallo transitorio**: el almacén caído no alerta ni desprende nada.
- **Desprendimiento rechazado** (entrada nueva tras exportar): fallo de partición con alerta, sin
  cortar la pasada; tras una pasada fallida, la siguiente archiva una vez y la tercera no duplica.
- **Concurrencia**: el desprendimiento espera a un escritor en curso y rechaza su entrada; dos
  archivados a la vez desprenden y registran una sola vez.
- **Guardas de nuc_0016**: solo el sistema lee y desprende; el desprendimiento exige el recuento
  verificado y ninguna entrada posterior; solo particiones adjuntas con el nombre del convenio.
- El manejador solo actúa en la iteración de la organización proveedora.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.identity_db import migrated_database
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import (
    Place,
    WriterEnvironment,
    unit_context,
    verify_ledger_chains,
    writer_environment,
)
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.shared.archive.audit_archive import (
    ARCHIVE_AUDIT_PARTITIONS,
    ARCHIVE_AUDIT_PARTITIONS_SCHEDULE,
    ARCHIVED_RECORD_TYPE,
    AUDIT_COLUMNS,
    ArchiveFailure,
    AuditArchiveFailed,
    AuditArchiver,
    AuditPartition,
    archive_audit_partitions_handler,
    column_values,
    register_archive_audit_partitions,
    restore_audit_partition,
)
from vigia_platform.shared.archive.partitions import add_months
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.storage import S3Storage, StorageUnavailable

pytestmark = pytest.mark.integration

VERIFIER = b"# vigia_verify.py de prueba\n"
INSUFFICIENT_PRIVILEGE = "42501"
NOT_IN_PREREQUISITE_STATE = "55000"
RESTRICT_VIOLATION = "23001"


@dataclass
class World:
    env: WriterEnvironment
    signing: SigningWorld
    checkpoints: CheckpointService
    storage: S3Storage
    s3: Any
    bucket: str
    kms_key_id: str
    month: date
    provider: uuid.UUID
    client: uuid.UUID

    def system(self, organization_id: uuid.UUID | None = None) -> ScopeContext:
        return unit_context(organization_id or self.provider, ActorUnit.U02, kind=ActorKind.SYSTEM)

    @property
    def partition(self) -> AuditPartition:
        return AuditPartition(self.month)

    def archiver(
        self, storage: Any = None, writer: Any = None, batch_size: int = 5_000
    ) -> AuditArchiver:
        now = datetime(self.month.year, self.month.month, 2, 3, 0, tzinfo=UTC)
        later = add_months(self.month, 25)
        clock = SimulatedClock(now.replace(year=later.year, month=later.month))
        return AuditArchiver(
            database=self.env.database,
            storage=storage or self.storage,
            writer=writer or self.env.writer,
            audit=self.env.audit,
            outbox=self.env.outbox,  # type: ignore[arg-type]
            checkpoint_keys=self.checkpoints.checkpoint_public_keys,
            verifier=VERIFIER,
            clock=clock,
            kms_key_id=self.kms_key_id,
            batch_size=batch_size,
            # Solo la auditoría: la lista ampliada de fleet la prueba
            # test_fleet_partition_archive_localstack.py (TASK-203).
            tables=(),
        )

    def fetch(self, query: str, *args: Any) -> list[Any]:
        async def run() -> list[Any]:
            connection = await self.env.migrated.connect()
            try:
                return list(await connection.fetch(query, *args))
            finally:
                await connection.close()

        return self.env.loop.run(run())

    def attached(self) -> bool:
        rows = self.fetch(
            "SELECT 1 FROM pg_inherits AS inheritance JOIN pg_class AS child"
            " ON child.oid = inheritance.inhrelid"
            " WHERE inheritance.inhparent = 'shared.audit_entry'::regclass AND child.relname = $1",
            self.partition.name,
        )
        return bool(rows)

    def partition_rows(self) -> list[dict[str, Any]]:
        rows = self.fetch(
            f"SELECT * FROM {self.partition.qualified_name}"  # noqa: S608 - nombre fijo de la prueba
            " ORDER BY organization_id, chain_sequence"
        )
        return [column_values(dict(row)) for row in rows]

    def archived_records(self) -> list[Any]:
        return self.fetch(
            "SELECT content_json, organization_id FROM ledger.ledger_record WHERE record_type = $1",
            ARCHIVED_RECORD_TYPE,
        )

    def alerts(self) -> list[Any]:
        return self.fetch(
            "SELECT organization_id, payload FROM shared.outbox_event"
            " WHERE event_name = 'security_alert'"
            " AND payload->>'alert_kind' = 'audit_archive_verification_failed'"
        )

    def failure_audits(self) -> list[Any]:
        return self.fetch(
            "SELECT organization_id, outcome, filters_json FROM shared.audit_entry"
            " WHERE operation = 'integrity_verification'"
            " AND filters_json->>'check' = 'audit_archive'"
        )


@pytest.fixture
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    s3 = localstack_endpoint.aws_client("s3")
    kms_key_id = localstack_endpoint.aws_client("kms").create_key(
        Description="vigia-archive de prueba"
    )["KeyMetadata"]["KeyId"]
    with (
        migrated_database(postgres_endpoint, "vigia_archive") as migrated,
        writer_environment(migrated) as env,
        versioned_bucket(s3, "vigia-archive") as bucket,
    ):
        signing = env.loop.run(bootstrapped_world())
        assert env.outbox is not None
        store = SqlCheckpointStore(
            database=env.database, writer=env.writer, audit=env.audit, outbox=env.outbox
        )
        checkpoints = CheckpointService(store=store, signer=signing.service, clock=signing.clock)
        storage = S3Storage(localstack_endpoint.storage_settings(bucket), env.clock)
        month = env.loop.run(_database_month(env))
        provider = env.provider_organization_id
        client = uuid.uuid4()
        Place.new(provider)
        Place.new(client)
        world = World(
            env, signing, checkpoints, storage, s3, bucket, kms_key_id, month, provider, client
        )
        _populate(world)
        yield world


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


def _populate(world: World) -> None:
    """Auditoría en las dos organizaciones, con un punto de control a mitad de cada cadena."""
    env = world.env
    for organization_id in (world.provider, world.client):
        context = world.system(organization_id)
        for _ in range(3):
            env.loop.run(env.audit.append(context, AuditOperation.LEDGER_READ, result_count=2))
        env.loop.run(world.checkpoints.write_checkpoints_now(context))
        env.loop.run(
            env.audit.append(
                context, AuditOperation.AUDIT_READ, filters={"zona": "Prensas «2»"}, result_count=0
            )
        )


def run_task(
    world: World, archiver: AuditArchiver, organization_id: uuid.UUID | None = None
) -> None:
    registry = PeriodicTaskRegistry()
    task = register_archive_audit_partitions(
        registry, archive_audit_partitions_handler(archiver, world.provider)
    )
    assert task.task_name == ARCHIVE_AUDIT_PARTITIONS
    assert task.schedule == ARCHIVE_AUDIT_PARTITIONS_SCHEDULE

    async def once() -> None:
        async with world.env.database.transaction(world.system(organization_id)) as transaction:
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


# --- PR-NUC-56: ida y vuelta ---------------------------------------------------------------------


def test_round_trip_archives_detaches_and_records(world: World) -> None:
    before = world.partition_rows()
    organizations = {row["organization_id"] for row in before}
    assert {world.provider, world.client} <= organizations
    archiver = world.archiver()
    assert world.partition in world.env.loop.run(archiver.due_partitions(world.system()))

    run_task(world, archiver)

    # Desprendida, no borrada: la tabla sigue con las mismas filas y es de solo anexar.
    assert not world.attached()
    assert world.partition_rows() == before
    # Exactamente un registro, en la cadena de expediente de la proveedora, con lo verificado.
    records = world.archived_records()
    assert len(records) == 1
    content = json.loads(records[0]["content_json"])
    assert records[0]["organization_id"] == world.provider
    key = world.partition.object_key
    assert content["partition_name"] == world.partition.qualified_name
    assert content["period"] == world.partition.period
    assert content["archive_object_key"] == key
    assert content["entry_count"] == len(before)
    stored = world.s3.get_object(Bucket=world.bucket, Key=key)
    body = stored["Body"].read()
    assert hashlib.sha256(body).hexdigest() == content["archive_sha256"]
    assert stored["ServerSideEncryption"] == "aws:kms"
    assert world.kms_key_id in stored["SSEKMSKeyId"]
    world.env.loop.run(verify_ledger_chains(world.env.migrated, world.provider))
    assert world.alerts() == []

    # Restauración de solo lectura: las mismas filas, columna a columna (hashes incluidos).
    restored = world.env.loop.run(
        restore_audit_partition(world.storage, key, content["archive_sha256"])
    )
    assert [column_values(row) for row in restored.rows] == before
    assert set(restored.rows[0]) == set(AUDIT_COLUMNS)
    assert sum(segment.checkpoints for segment in restored.contents.segments) >= 2

    # La tabla desprendida rechaza UPDATE y DELETE aun como su dueño.
    async def mutate(statement: str) -> str | None:
        connection = await world.env.migrated.connect("vigia_migrate")
        try:
            await connection.execute(statement)
        except Exception as error:
            return sqlstate(error)
        finally:
            await connection.close()
        return None

    table = world.partition.qualified_name
    assert world.env.loop.run(mutate(f"DELETE FROM {table}")) == RESTRICT_VIOLATION  # noqa: S608
    assert (
        world.env.loop.run(mutate(f"UPDATE {table} SET result_count = 9"))  # noqa: S608
        == RESTRICT_VIOLATION
    )
    assert world.env.loop.run(mutate(f"TRUNCATE {table}")) == RESTRICT_VIOLATION

    # Una segunda pasada no encuentra nada que archivar ni escribe otro registro.
    run_task(world, world.archiver())
    assert len(world.archived_records()) == 1


# --- Criterio 2: bit alterado --------------------------------------------------------------------


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
def test_flipped_bit_keeps_the_partition_attached_and_writes_no_record(
    world: World, position: float, bit: int
) -> None:
    before = world.partition_rows()
    archiver = world.archiver(storage=CorruptingStorage(world, position, bit))
    with pytest.raises(AuditArchiveFailed) as raised:
        run_task(world, archiver)
    assert raised.value.partitions == (world.partition.qualified_name,)
    assert world.attached()
    assert world.archived_records() == []
    # La alerta y la auditoría del fallo, confirmadas en la cadena de la proveedora.
    alerts = world.alerts()
    assert [alert["organization_id"] for alert in alerts] == [world.provider]
    audits = world.failure_audits()
    assert len(audits) == 1
    assert audits[0]["organization_id"] == world.provider
    assert audits[0]["outcome"] == "error"
    assert json.loads(audits[0]["filters_json"]) == {
        "task": ARCHIVE_AUDIT_PARTITIONS,
        "check": "audit_archive",
        "partition": world.partition.qualified_name,
        "failure_reason": ArchiveFailure.DIGEST_MISMATCH.value,
    }
    # Nada de la partición cambió (la entrada de la alerta va a la partición del mes, al final).
    after = [row for row in world.partition_rows() if row["entry_id"] != audits_entry_id(world)]
    assert after == before


def audits_entry_id(world: World) -> uuid.UUID:
    rows = world.fetch(
        "SELECT entry_id FROM shared.audit_entry WHERE operation = 'integrity_verification'"
    )
    return uuid.UUID(str(rows[0]["entry_id"]))


# --- Atomicidad y fallos transitorios ------------------------------------------------------------


class RejectingWriter:
    async def write(
        self, context: Any, record_type: str, content: Mapping[str, Any], **_: Any
    ) -> Any:
        return LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/period")


def test_a_rejected_record_undoes_the_detach(world: World) -> None:
    with pytest.raises(RuntimeError):
        world.env.loop.run(
            world.archiver(writer=RejectingWriter()).archive(world.system(), world.partition)
        )
    assert world.attached()
    assert world.archived_records() == []


def test_an_unexpected_failure_ends_the_pass_as_failed_without_escaping(world: World) -> None:
    """Un error no previsto (aquí, el rechazo del escritor) no sale de la pasada tal cual: la
    partición queda como fallida, sigue adjunta y la pasada termina con ``AuditArchiveFailed``."""
    with pytest.raises(AuditArchiveFailed) as raised:
        run_task(world, world.archiver(writer=RejectingWriter()))
    assert raised.value.partitions == (world.partition.qualified_name,)
    assert world.attached()
    assert world.archived_records() == []


class DownStorage:
    def __init__(self, world: World) -> None:
        self._world = world

    async def put_object(self, key: str, body: bytes, content_type: str, **kwargs: Any) -> Any:
        return await self._world.storage.put_object(key, body, content_type, **kwargs)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        raise StorageUnavailable("get_object")


def test_storage_down_neither_alerts_nor_detaches(world: World) -> None:
    with pytest.raises(AuditArchiveFailed):
        run_task(world, world.archiver(storage=DownStorage(world)))
    assert world.attached()
    assert world.archived_records() == []
    assert world.alerts() == []


class WritingStorage:
    """Almacén real que, al leer de vuelta, deja una entrada de auditoría nueva en la partición:
    algo escribió entre la exportación y el desprendimiento."""

    def __init__(self, world: World) -> None:
        self._world = world

    async def put_object(self, key: str, body: bytes, content_type: str, **kwargs: Any) -> Any:
        return await self._world.storage.put_object(key, body, content_type, **kwargs)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        await self._world.env.audit.append(
            self._world.system(self._world.client), AuditOperation.LEDGER_READ, result_count=1
        )
        return await self._world.storage.get_object(key, version_id=version_id)


def test_rejected_detach_is_a_partition_failure_with_alert(world: World) -> None:
    """La base rechaza el desprendimiento (una entrada nueva tras exportar): la pasada no se
    corta, la partición sigue adjunta, no hay registro y queda la alerta."""
    with pytest.raises(AuditArchiveFailed) as raised:
        run_task(world, world.archiver(storage=WritingStorage(world)))
    assert raised.value.partitions == (world.partition.qualified_name,)
    assert world.attached()
    assert world.archived_records() == []
    assert [alert["organization_id"] for alert in world.alerts()] == [world.provider]
    reasons = [json.loads(row["filters_json"])["failure_reason"] for row in world.failure_audits()]
    assert reasons == [ArchiveFailure.PARTITION_CHANGED.value]


def test_a_failed_pass_resumes_once_and_never_twice(world: World) -> None:
    """Tras una pasada fallida, la siguiente archiva una sola vez y la tercera no duplica."""
    with pytest.raises(AuditArchiveFailed):
        run_task(world, world.archiver(storage=CorruptingStorage(world, 0.5, 3)))
    assert world.attached()
    run_task(world, world.archiver())
    assert not world.attached()
    assert len(world.archived_records()) == 1
    run_task(world, world.archiver())
    assert len(world.archived_records()) == 1


# --- Concurrencia (la garantía «se desprende exactamente lo verificado, una sola vez») ----------


def test_detach_waits_for_an_in_flight_writer_and_rejects_its_entry(world: World) -> None:
    """Un escritor con una entrada sin confirmar en la partición mientras otra transacción la
    desprende con el recuento confirmado: el desprendimiento espera al escritor, ve la entrada
    nueva y se rechaza. Sin el ``LOCK TABLE`` de nuc_0016 contaría sin ella y desprendería una
    partición con una entrada sin archivar."""
    count = len(world.partition_rows())
    end = datetime(*add_months(world.month, 1).timetuple()[:3], tzinfo=UTC)
    inserted = asyncio.Event()

    async def writer() -> None:
        context = world.system(world.client)
        async with world.env.database.transaction(context) as transaction:
            await world.env.audit.append(
                context, AuditOperation.LEDGER_READ, result_count=1, transaction=transaction
            )
            inserted.set()
            await asyncio.sleep(1.5)

    async def detach() -> str | None:
        await inserted.wait()
        try:
            async with world.env.database.transaction(world.system()) as transaction:
                await transaction.execute(
                    text("SELECT shared.vigia_detach_audit_partition(:p, :n, :t)"),
                    {"p": world.partition.name, "n": count, "t": end},
                )
        except Exception as error:
            return sqlstate(error) or type(error).__name__
        return None

    async def both() -> list[Any]:
        return await asyncio.gather(writer(), detach())

    _, outcome = world.env.loop.run(both())
    assert outcome == NOT_IN_PREREQUISITE_STATE
    assert world.attached()
    assert len(world.partition_rows()) == count + 1


def test_two_concurrent_archives_detach_and_record_once(world: World) -> None:
    """Dos archivados de la misma partición a la vez: uno la desprende y la registra; el otro
    falla sin desprender nada más ni escribir un segundo ``audit_partition_archived``."""

    async def both() -> list[Any]:
        return await asyncio.gather(
            world.archiver().archive(world.system(), world.partition),
            world.archiver().archive(world.system(), world.partition),
            return_exceptions=True,
        )

    outcomes = world.env.loop.run(both())
    archived = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    failed = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(archived) == 1, outcomes
    assert len(failed) == 1, outcomes
    assert not world.attached()
    assert len(world.archived_records()) == 1


@pytest.mark.parametrize("batch_size", [1, 2, 3])
def test_export_in_small_batches_reads_every_row_once(world: World, batch_size: int) -> None:
    """La lectura por lotes (orden de organización y secuencia) cruza organizaciones sin perder
    ni repetir filas: el archivo verifica y la restauración es la partición entera."""
    before = world.partition_rows()
    assert len(before) > 3 * batch_size
    archived = world.env.loop.run(
        world.archiver(batch_size=batch_size).archive(world.system(), world.partition)
    )
    assert archived.entry_count == len(before)
    restored = world.env.loop.run(
        restore_audit_partition(world.storage, archived.object_key, archived.sha256)
    )
    assert [column_values(row) for row in restored.rows] == before
    assert not world.attached()


def test_handler_acts_only_in_the_provider_iteration(world: World) -> None:
    run_task(world, world.archiver(), world.client)
    assert world.attached()
    assert world.archived_records() == []


# --- Guardas de nuc_0016 -------------------------------------------------------------------------


def _call(world: World, context: ScopeContext, statement: str, **parameters: Any) -> str | None:
    async def run() -> str | None:
        try:
            async with world.env.database.transaction(context) as transaction:
                await transaction.execute(text(statement), parameters)
        except Exception as error:
            return sqlstate(error) or type(error).__name__
        return None

    return world.env.loop.run(run())


@pytest.mark.parametrize("kind", [ActorKind.USER, ActorKind.OPERATOR, ActorKind.PROVIDER_USER])
def test_only_the_system_reads_or_detaches_an_audit_partition(
    world: World, kind: ActorKind
) -> None:
    context = unit_context(world.provider, ActorUnit.U02, kind=kind)
    name = world.partition.name
    for statement in (
        "SELECT * FROM shared.vigia_audit_partition_summary(:p)",
        "SELECT * FROM shared.vigia_audit_partition_rows(:p, NULL, NULL, 10)",
        "SELECT shared.vigia_detach_audit_partition(:p, 0, now())",
    ):
        assert _call(world, context, statement, p=name) == INSUFFICIENT_PRIVILEGE, statement
    assert world.attached()


def test_detach_requires_the_verified_count_and_nothing_newer(world: World) -> None:
    count = len(world.partition_rows())
    end = datetime(*add_months(world.month, 1).timetuple()[:3], tzinfo=UTC)
    name = world.partition.name
    statement = "SELECT shared.vigia_detach_audit_partition(:p, :n, :t)"
    assert (
        _call(world, world.system(), statement, p=name, n=count - 1, t=end)
        == NOT_IN_PREREQUISITE_STATE
    )
    assert (
        _call(world, world.system(), statement, p=name, n=count + 1, t=end)
        == NOT_IN_PREREQUISITE_STATE
    )
    early = datetime(world.month.year, world.month.month, 1, tzinfo=UTC) + timedelta(seconds=1)
    assert (
        _call(world, world.system(), statement, p=name, n=count, t=early)
        == NOT_IN_PREREQUISITE_STATE
    )
    assert world.attached()


@pytest.mark.parametrize(
    "name",
    [
        "audit_entry_default",
        "ledger_record_2026_10",
        "audit_entry_2026_13",
        "audit_entry_1999_01",
        "x; DROP",
    ],
)
def test_only_attached_audit_partitions_with_the_convention_are_touched(
    world: World, name: str
) -> None:
    for statement in (
        "SELECT * FROM shared.vigia_audit_partition_rows(:p, NULL, NULL, 10)",
        "SELECT shared.vigia_detach_audit_partition(:p, 0, now())",
    ):
        assert _call(world, world.system(), statement, p=name) in ("22023", "42P01"), (
            name,
            statement,
        )
    assert world.attached()
