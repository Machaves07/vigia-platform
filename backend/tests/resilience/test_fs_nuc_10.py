"""FS-NUC-10 · Ensayo de restauración en ``nightly`` sobre datos generados (NFR-NUC-11, 12;
PR-NUC-19; DR-NUC-01 a 03; ``deployment-architecture.md`` §6.1).

**Datos**: un PostgreSQL 16 del arnés con **archivo continuo de WAL** (``archive_mode``), base
migrada y, por ``EscritorExpediente`` como ``vigia_app``, hallazgos de los **generadores del kit**
en varias plantas de varias organizaciones, registros de organización, entradas de auditoría y
**puntos de control firmados** (``CheckpointService``) en todas las cadenas.

**Inyección** (la copia y la restauración del runbook, sobre contenedores):

- **DR-NUC-02**, desde la instantánea: copia física (``pg_basebackup``) restaurada en un
  **contenedor nuevo**;
- **DR-NUC-01**, a un instante: la misma copia más el WAL archivado, recuperada hasta un **punto
  con nombre** (``pg_create_restore_point``) en otro **contenedor nuevo**; lo escrito después del
  punto no debe aparecer;
- **DR-NUC-03**: el objeto de una evidencia se sobrescribe en el depósito versionado (LocalStack) y
  se **restaura su versión anterior**; su suma vuelve a ser la del expediente restaurado.

**Resultado esperado**: en cada base restaurada, la **verificación completa de todas las cadenas**
de todas las organizaciones (``IntegrityService.verify_all`` en modo ``full``, como ``vigia_app``)
da **todas ``intact``**, el oráculo de PR-NUC-13 también, las cabezas son las del instante
restaurado y se **registra el punto de control alcanzado** de cada cadena; el **tiempo medido**
(restauración más verificación) se compara con el **RTO de 4 h** y queda en el informe.

El ensayo trimestral sobre el entorno desplegado es del runbook (VIG-97), no de esta prueba.
Solo datos generados.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import pytest
from vigia_contracts.conformance.stub_platform.objects import synthetic_clip

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase
from tests.integration.conftest import POSTGRES_USER, LocalStackEndpoint, versioned_bucket
from tests.outbox_support import probe_catalog
from tests.resilience.harness import (
    WALL,
    Container,
    dedicated_localstack,
    restored_postgres,
    scenario,
    wait_until,
)
from tests.resilience.load import all_clips, kit_findings
from tests.resilience.stack import LedgerStack, ledger_stack
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import (
    NOW,
    ORDER_TYPE,
    Place,
    order_document,
    organization_document,
    unit_context,
    verify_audit_chain,
    verify_ledger_chains,
)
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityStore
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.ledger.chain.verify import IntegrityService, VerificationMode
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.storage import ANONYMIZED_METADATA_KEY, S3Storage

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

RTO_SECONDS: Final = 4 * 3600
"""RTO de NFR-NUC-11 (4 h)."""
PGDATA: Final = "/var/lib/postgresql/data"
ARCHIVE: Final = "/var/lib/postgresql/archive"
BACKUP: Final = "/var/lib/postgresql/backup"
RESTORE_POINT: Final = "vigia_drill"
ARCHIVING: Final = (
    "postgres",
    "-c",
    "wal_level=replica",
    "-c",
    "archive_mode=on",
    "-c",
    f"archive_command=test ! -f {ARCHIVE}/%f && cp %p {ARCHIVE}/%f",
)
ORGANIZATIONS: Final = 2
PLANTS_PER_ORGANIZATION: Final = 2


@dataclass
class Source:
    stack: LedgerStack
    world: SigningWorld
    checkpoints: CheckpointService
    organizations: list[uuid.UUID]

    def run(self, awaitable: Any) -> Any:
        return self.stack.run(awaitable)


@pytest.fixture(scope="module")
def source() -> Iterator[Source]:
    with ledger_stack("fs_nuc_10", command=ARCHIVING) as stack:
        code, output = stack.container.exec(["mkdir", "-p", ARCHIVE], user="postgres")
        assert code == 0, output
        env = stack.env
        world = stack.run(bootstrapped_world())
        assert env.outbox is not None
        store = SqlCheckpointStore(
            database=env.database, writer=env.writer, audit=env.audit, outbox=env.outbox
        )
        checkpoints = CheckpointService(store=store, signer=world.service, clock=world.clock)
        yield Source(stack, world, checkpoints, [])


@pytest.fixture(scope="module")
def localstack_endpoint() -> Iterator[LocalStackEndpoint]:
    """El almacén versionado del ensayo: un LocalStack propio del arnés."""
    with dedicated_localstack() as (endpoint, _):
        yield endpoint


def _system(organization_id: uuid.UUID) -> ScopeContext:
    return unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)


def _write(source: Source, context: ScopeContext, record_type: str, content: Any) -> None:
    receipt = source.run(source.stack.env.writer.write(context, record_type, content))
    assert isinstance(receipt, Receipt), receipt


def _populate(source: Source, seed: int, findings_per_plant: int) -> None:
    """Hallazgos del kit por planta, registros de organización, auditoría y puntos de control."""
    env = source.stack.env
    if not source.organizations:
        source.organizations.extend(uuid.uuid4() for _ in range(ORGANIZATIONS))
    for index, organization in enumerate(source.organizations):
        node_context = unit_context(organization, ActorUnit.U03, kind=ActorKind.SYSTEM)
        for plant_index in range(PLANTS_PER_ORGANIZATION):
            place = _plant(organization, plant_index)
            findings = kit_findings(seed + index * 31 + plant_index, place, findings_per_plant)
            for clip in all_clips(findings):
                env.storage.put(clip)
            for finding in findings:
                _write(source, node_context, "finding_received", finding)
        reading = unit_context(organization, ActorUnit.U02)
        _write(source, reading, "organization_created", organization_document(organization))
        # Lecturas del expediente: cada una deja su entrada en la cadena de auditoría.
        reader = LectorExpediente(database=env.database, audit=env.audit)
        for _ in range(2):
            source.run(reader.list(reading))
        source.run(source.checkpoints.write_checkpoints_now(_system(organization)))


_PLANTS: dict[tuple[uuid.UUID, int], Place] = {}


def _plant(organization: uuid.UUID, index: int) -> Place:
    key = (organization, index)
    if key not in _PLANTS:
        _PLANTS[key] = Place.new(organization)
    plant = _PLANTS[key]
    return Place(plant.organization_id, plant.plant_id, plant.zone_id, uuid.uuid4())


async def _heads(migrated: MigratedDatabase, organizations: Sequence[uuid.UUID]) -> list[Any]:
    connection = await migrated.connect()
    try:
        rows = await connection.fetch(
            "SELECT organization_id, kind, plant_id, last_sequence, last_hash"
            " FROM ledger.chain_head WHERE organization_id = ANY($1::uuid[])"
            " ORDER BY organization_id, kind, plant_id NULLS FIRST",
            list(organizations),
        )
        return [tuple(row.values()) for row in rows]
    finally:
        await connection.close()


async def _superuser_value(migrated: MigratedDatabase, sql: str) -> Any:
    connection = await migrated.connect()
    try:
        return await connection.fetchval(sql)
    finally:
        await connection.close()


# --- Copia y archivos -----------------------------------------------------------------------------


def _read_container_file(container: Container, path: str) -> bytes:
    stream, _ = container.raw.get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as outer:
        (member,) = [m for m in outer.getmembers() if m.isfile()]
        extracted = outer.extractfile(member)
        assert extracted is not None
        return extracted.read()


def _archive_into(container: Container, source_dir: str, target_name: str) -> bytes:
    """Los archivos de ``source_dir`` del contenedor como ``tar`` bajo ``target_name/``."""
    stream, _ = container.raw.get_archive(source_dir)
    packed = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as outer,
        tarfile.open(fileobj=packed, mode="w") as inner,
    ):
        for member in outer.getmembers():
            if not member.isfile():
                continue
            data = outer.extractfile(member)
            assert data is not None
            info = tarfile.TarInfo(f"{target_name}/{member.name.rsplit('/', 1)[-1]}")
            content = data.read()
            info.size = len(content)
            info.mode = 0o600
            inner.addfile(info, io.BytesIO(content))
    return packed.getvalue()


def _empty_file(name: str) -> bytes:
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = 0
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(b""))
    return packed.getvalue()


# --- Verificación de lo restaurado ---------------------------------------------------------------


@dataclass
class Verified:
    restore_seconds: float
    verify_seconds: float
    heads: list[Any]
    results: list[dict[str, Any]]
    oracle: dict[str, Any]
    checkpoints: list[dict[str, Any]]
    evidence_sha256: dict[str, str]


async def _verify_restored(
    source: Source, restored: MigratedDatabase, evidence_keys: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Verificación completa de todas las cadenas como ``vigia_app`` y puntos de control."""
    env = source.stack.env
    database = Database.create(
        DatabaseSettings(
            url=restored.as_role("vigia_app").sqlalchemy_url,
            process=ProcessKind.WORKER,
            sslmode=SslMode.DISABLE,
        )
    )
    try:
        catalog = probe_catalog()
        async with database.transaction(_system(source.organizations[0])) as transaction:
            await catalog.synchronize(SqlOutboxCatalogStore(transaction), env.clock)
        outbox = Outbox(catalog, env.clock)
        audit = AuditWriter(
            database=database,
            clock=env.clock,
            provider_organization_id=env.provider_organization_id,
        )
        writer = EscritorExpediente(
            database=database,
            registry=env.registry,
            free_text=FreeTextPolicyRegistry(),
            evidence=EvidenceVerifier(env.storage, env.clock),
            outbox=outbox,
            clock=env.clock,
        )
        restored_checkpoints = CheckpointService(
            store=SqlCheckpointStore(database=database, writer=writer, audit=audit, outbox=outbox),
            signer=source.world.service,
            clock=source.world.clock,
        )
        service = IntegrityService(
            store=SqlIntegrityStore(database=database, audit=audit, outbox=outbox),
            keys=source.checkpoints,
            clock=env.clock,
        )
        results: list[dict[str, Any]] = []
        reached: list[dict[str, Any]] = []
        for organization in source.organizations:
            context = _system(organization)
            for checkpoint in await restored_checkpoints.latest_checkpoints(context):
                reached.append(
                    {
                        "organization_id": str(organization),
                        "chain": checkpoint.chain.describe(),
                        "sequence": checkpoint.sequence,
                        "covered_sequence": checkpoint.content.covered_sequence,
                        "taken_at": checkpoint.content.taken_at,
                    }
                )
            for result in await service.verify_all(context, VerificationMode.FULL):
                results.append(
                    {
                        "organization_id": str(organization),
                        "chain": result.chain.describe(),
                        "status": result.status.value,
                        "to_sequence": result.to_sequence,
                        "head_sequence": result.head_sequence,
                        "checkpoints_checked": result.checkpoints_checked,
                        "canonical_checked": result.canonical_checked,
                    }
                )
        evidence: dict[str, str] = {}
        if evidence_keys:
            connection = await restored.connect()
            try:
                for row in await connection.fetch(
                    "SELECT storage_key, sha256 FROM ledger.evidence"
                    " WHERE storage_key = ANY($1::text[])",
                    list(evidence_keys),
                ):
                    evidence[str(row["storage_key"])] = str(row["sha256"])
            finally:
                await connection.close()
        return results, reached, evidence
    finally:
        await database.dispose()


def _restore_and_verify(
    source: Source,
    files: Sequence[tuple[str, bytes]],
    command: Sequence[str],
    evidence_keys: Sequence[str],
) -> Verified:
    migrated = source.stack.migrated
    started = WALL.monotonic()
    with restored_postgres(files, command=command) as (endpoint, _):
        restore_seconds = WALL.monotonic() - started
        restored = MigratedDatabase(
            endpoint, migrated.database, migrated.app_password, migrated.migrate_password
        )
        heads = source.run(_heads(restored, source.organizations))
        verify_started = WALL.monotonic()
        results, reached, evidence = source.run(_verify_restored(source, restored, evidence_keys))
        verify_seconds = WALL.monotonic() - verify_started
        oracle: dict[str, Any] = {}
        for organization in source.organizations:
            chains = source.run(verify_ledger_chains(restored, organization))
            oracle[str(organization)] = {
                "ledger": {str(plant): length for plant, length in chains.items()},
                "audit": source.run(verify_audit_chain(restored, organization)),
            }
    return Verified(
        round(restore_seconds, 2),
        round(verify_seconds, 2),
        heads,
        results,
        oracle,
        reached,
        evidence,
    )


# --- DR-NUC-03: la versión anterior de una evidencia --------------------------------------------


def _evidence_with_object(
    source: Source, localstack: LocalStackEndpoint, bucket: str
) -> tuple[str, str, str]:
    """Un registro con un clip de verdad en el depósito versionado: (clave, sha256, versión)."""
    s3 = localstack.aws_client("s3")
    place = _plant(source.organizations[0], 0)
    content = synthetic_clip(f"fs-nuc-10 {uuid.uuid4().hex}")
    digest = hashlib.sha256(content).digest()
    clip_id = str(uuid7())
    key = place.storage_key(clip_id)
    version = s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType="video/mp4",
        ChecksumSHA256=base64.b64encode(digest).decode(),
        Metadata={ANONYMIZED_METADATA_KEY: "1"},
    )["VersionId"]
    document = order_document(place, clips=0)
    document["clips"] = [
        {
            "clip_id": clip_id,
            "camera_id": str(uuid.uuid4()),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": digest.hex(),
            "size_bytes": len(content),
            "duration_ms": 10_000,
            "starts_at": _stamp(NOW),
            "ends_at": _stamp(NOW + timedelta(seconds=10)),
            "segment": "full",
            "anonymized": True,
            "storage_key": key,
        }
    ]
    storage = S3Storage(localstack.storage_settings(bucket), WALL)
    writer = source.stack.writer(source.stack.env.database, storage=storage)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
    receipt = source.run(writer.write(context, ORDER_TYPE, document))
    assert isinstance(receipt, Receipt), receipt
    return key, digest.hex(), str(version)


def _stamp(moment: Any) -> str:
    return str(moment.replace(tzinfo=None).isoformat(timespec="milliseconds")) + "Z"


def _restore_previous_version(
    source: Source, localstack: LocalStackEndpoint, bucket: str, key: str, version: str
) -> tuple[float, Any]:
    """Sobrescribe el objeto (pérdida o alteración) y restaura su versión anterior.

    Devuelve lo que tardó la restauración y la cabecera del objeto leída por ``S3Storage``.
    """
    s3 = localstack.aws_client("s3")
    damaged = synthetic_clip("contenido alterado")
    s3.put_object(Bucket=bucket, Key=key, Body=damaged, ContentType="video/mp4")
    started = WALL.monotonic()
    # La versión anterior pasa a ser la vigente (copia sobre la misma clave). Los metadatos se
    # repiten explícitamente: S3 de LocalStack rechaza una copia sobre sí misma sin cambiarlos.
    s3.copy_object(
        Bucket=bucket,
        Key=key,
        CopySource={"Bucket": bucket, "Key": key, "VersionId": version},
        ChecksumAlgorithm="SHA256",
        MetadataDirective="REPLACE",
        Metadata={ANONYMIZED_METADATA_KEY: "1"},
        ContentType="video/mp4",
    )
    seconds = WALL.monotonic() - started
    storage = S3Storage(localstack.storage_settings(bucket), WALL)
    return round(seconds, 3), source.run(storage.head_object(key))


# --- El ensayo ------------------------------------------------------------------------------------


def test_fs_nuc_10_restore_drill_on_generated_data(
    source: Source, localstack_endpoint: LocalStackEndpoint
) -> None:
    with (
        scenario(
            "FS-NUC-10",
            title="Ensayo de restauración",
            injection=(
                "copia de la base restaurada en un contenedor nuevo y almacén restaurado a una"
                " versión, sobre datos generados"
            ),
            expected=(
                "verificación completa de todas las cadenas intact; punto de control alcanzado"
                " registrado; tiempo medido y comparado con el RTO de 4 h"
            ),
        ) as run,
        versioned_bucket(localstack_endpoint.aws_client("s3"), "fs-nuc-10") as bucket,
    ):
        stack = source.stack
        container = stack.container
        per_plant = run.random.randint(15, 30)
        _populate(source, run.random.getrandbits(32), per_plant)
        evidence_key, evidence_sha256, evidence_version = _evidence_with_object(
            source, localstack_endpoint, bucket
        )
        source.run(source.checkpoints.write_checkpoints_now(_system(source.organizations[0])))

        # Copia física (la instantánea) y cabezas en ese instante.
        backup_started = WALL.monotonic()
        code, output = container.exec(
            [
                "pg_basebackup",
                "-D",
                BACKUP,
                "-Ft",
                "-X",
                "fetch",
                "--checkpoint=fast",
                "-U",
                POSTGRES_USER,
                "-h",
                "/var/run/postgresql",
            ],
            user="postgres",
        )
        assert code == 0, output
        backup_seconds = WALL.monotonic() - backup_started
        snapshot_heads = source.run(_heads(stack.migrated, source.organizations))

        # Más escritura, el punto con nombre, y más escritura que no debe volver.
        _populate(source, run.random.getrandbits(32), max(1, per_plant // 3))
        source.run(
            _superuser_value(stack.migrated, f"SELECT pg_create_restore_point('{RESTORE_POINT}')")
        )
        point_heads = source.run(_heads(stack.migrated, source.organizations))
        _populate(source, run.random.getrandbits(32), 2)
        after_point_heads = source.run(_heads(stack.migrated, source.organizations))
        segment = source.run(
            _superuser_value(stack.migrated, "SELECT pg_walfile_name(pg_switch_wal())")
        )

        def archived() -> bool:
            last = source.run(
                _superuser_value(stack.migrated, "SELECT last_archived_wal FROM pg_stat_archiver")
            )
            return last is not None and str(last) >= str(segment)

        wait_until(archived, timeout=60, message="el WAL del punto no se archivó")
        base = _read_container_file(container, f"{BACKUP}/base.tar")
        wal = _archive_into(container, ARCHIVE, "drill_archive")

        # DR-NUC-02: desde la instantánea, en un contenedor nuevo.
        from_snapshot = _restore_and_verify(source, [(PGDATA, base)], ["postgres"], [evidence_key])
        # DR-NUC-01: a un instante (el punto con nombre), en otro contenedor nuevo.
        to_point = _restore_and_verify(
            source,
            [(PGDATA, base), (PGDATA, wal), (PGDATA, _empty_file("recovery.signal"))],
            [
                "postgres",
                "-c",
                f"restore_command=cp {PGDATA}/drill_archive/%f %p",
                "-c",
                f"recovery_target_name={RESTORE_POINT}",
                "-c",
                "recovery_target_action=promote",
            ],
            [evidence_key],
        )
        # DR-NUC-03: la versión anterior del objeto de la evidencia.
        object_seconds, head = _restore_previous_version(
            source, localstack_endpoint, bucket, evidence_key, evidence_version
        )
        assert head is not None and head.checksum_sha256 is not None
        restored_object_sha256 = base64.b64decode(head.checksum_sha256).hex()

        drills = {"DR-NUC-02": from_snapshot, "DR-NUC-01": to_point}
        run.observe(
            backup_seconds=round(backup_seconds, 2),
            rto_seconds=RTO_SECONDS,
            drills={
                name: {
                    "restore_seconds": drill.restore_seconds,
                    "verify_seconds": drill.verify_seconds,
                    "total_seconds": round(drill.restore_seconds + drill.verify_seconds, 2),
                    "within_rto": drill.restore_seconds + drill.verify_seconds < RTO_SECONDS,
                    "chains": len(drill.results),
                    "statuses": sorted({result["status"] for result in drill.results}),
                    "results": drill.results,
                    "checkpoints_reached": drill.checkpoints,
                    "oracle": drill.oracle,
                }
                for name, drill in drills.items()
            },
            object_version={
                "restore_seconds": object_seconds,
                "sha256_matches_ledger": restored_object_sha256 == evidence_sha256,
            },
        )
        for name, drill in drills.items():
            assert drill.results, name
            assert {result["status"] for result in drill.results} == {"intact"}, name
            # Todas las cadenas: expediente de cada planta y de la organización, y auditoría.
            kinds = {result["chain"].split(":")[0] for result in drill.results}
            assert len(drill.results) >= ORGANIZATIONS * (PLANTS_PER_ORGANIZATION + 2), kinds
            assert drill.checkpoints, f"{name}: el punto de control alcanzado se registra"
            assert drill.restore_seconds + drill.verify_seconds < RTO_SECONDS
            assert drill.evidence_sha256 == {evidence_key: evidence_sha256}
        assert from_snapshot.heads == snapshot_heads, "DR-NUC-02: el estado de la instantánea"
        assert to_point.heads == point_heads, "DR-NUC-01: el estado del punto con nombre"
        assert after_point_heads != point_heads, "lo escrito tras el punto no vuelve"
        assert restored_object_sha256 == evidence_sha256, "DR-NUC-03: la versión anterior"
