"""Proceso hijo de la carga de exportación (TASK-353; NFR-LAZ-49; PAT-LAZ-REN-03; infraestructura
§6.2).

``python -m tests.load.export_load_child '<json>'`` ejecuta **un intento del consumidor real**
sobre una solicitud ``with_clips`` que la prueba ya creó por HTTP, en un proceso propio para medir
su memoria y su ``/tmp`` sin el resto de la prueba:

- ``ExportWorker`` real: arrendamiento, puntos de control fijados, progreso y cierre ``ready`` con
  ``export_package`` y ``export_produced`` en PostgreSQL como ``vigia_app``;
- ``ExportComposer`` real (TASK-318) con el documento portátil real, el verificador incluido y el
  manifiesto firmado; ``ExportStream`` real con los tamaños de producción (partes de 16 MiB,
  volúmenes de 2 GB) sobre ``S3Storage`` de LocalStack;
- el expediente de la zona es el mundo determinista de TASK-318 (``job_world``) con 2 387
  hallazgos y criptografía real (``seal_world``); cada evidencia apunta a un objeto de la reserva
  de clips sintéticos de LocalStack, que el compositor **descarga entero** con ``get_object`` y
  comprueba contra su SHA-256 antes de escribirlo.

Mide: la memoria máxima del proceso (``ru_maxrss``) y la base antes del intento; el máximo de
memoria de Python viva a la vez durante el intento (``tracemalloc``: los trozos, las partes, los
clips y los búferes del ZIP), que es lo que el flujo retiene; el uso máximo de su directorio
temporal (``TMPDIR``, que la prueba crea vacío; muestreado cada 50 ms y al terminar cada
artefacto) y la duración. Después hace una **segunda corrida** del mismo trabajo (el mismo
``ExportJob`` que recibió el compositor) hacia un almacén que solo cuenta los bytes, para comparar
el manifiesto y el ``archive_sha256``.

Una línea JSON en la salida estándar. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import resource
import sys
import tempfile
import threading
import tracemalloc
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from tests.export_stream_support import RecordingSink
from tests.export_support import CountingCheckpoints, RecordingAlarm
from tests.factories import uuid7
from tests.properties.laz.strategies.export import ExportWorld, WorldPorts, job_world
from tests.verifier_bundle_support import seal_world
from tests.worker_support import system_contexts
from tests.writer_support import unit_context
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.export.composer_port import (
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.contents import ExportComposer
from vigia_platform.accreditation.export.lease import SqlExportLeaseStore
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.accreditation.export.worker import (
    ExportWorker,
    covered_chains,
    register_export_worker,
)
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.ledger.application.reader import EvidenceReference
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode, Transaction
from vigia_platform.shared.outbox.publish import Outbox, OutboxEvent
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.storage import (
    AddressingStyle,
    ChecksumType,
    CompletedPart,
    ObjectHead,
    S3Storage,
    StorageCredentials,
    StorageSettings,
)

__all__ = ["ClipPool", "PoolPorts", "load_world"]

WALL: Final = SystemClock()
"""Solo para medir duraciones (informe de tendencia); ninguna decide un resultado."""
SAMPLE_SECONDS: Final = 0.05
LOCK_TIMEOUT_MS: Final = 60_000


@dataclass(frozen=True)
class ClipPool:
    """La reserva de clips de LocalStack: ``keys[i]`` tiene ``size`` bytes con ``sha256[i]``."""

    keys: tuple[str, ...]
    sha256: tuple[str, ...]
    size: int

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ClipPool:
        return cls(tuple(value["keys"]), tuple(value["sha256"]), int(value["size"]))


def load_world(job: ExportJob, findings: int, pool: ClipPool) -> tuple[ExportWorld, dict[Any, str]]:
    """El mundo de ``job`` con ``findings`` hallazgos en su zona, cada evidencia apuntando a un clip
    de la reserva (por turno), sellado con criptografía real; y evidencia → clave del clip."""
    world = job_world(job, findings_per_zone=findings)
    # Los puntos de control que fijó el consumidor, uno por cadena cubierta y en su orden
    # (``worker.covered_chains``: la planta, la organización y la auditoría), como los da
    # ``exported_packages`` (``job_world`` los pone todos en la cadena de la planta).
    organization, plant = job.organization_id, job.plant_id
    checkpoints = [
        dataclasses.replace(
            item,
            chain=chain,
            sequence=index + 10,
            content=dataclasses.replace(item.content, covered_sequence=index + 9),
        )
        for index, (item, chain) in enumerate(
            zip(world.checkpoints[organization], covered_chains(plant), strict=True)
        )
    ]
    world = dataclasses.replace(world, checkpoints={**world.checkpoints, organization: checkpoints})
    keys: dict[Any, str] = {}
    records = []
    for record in world.records:
        evidences = []
        for evidence in record.evidences:
            slot = len(keys) % len(pool.keys)
            keys[evidence.evidence_id] = pool.keys[slot]
            evidences.append(
                dataclasses.replace(evidence, sha256=pool.sha256[slot], size_bytes=pool.size)
            )
        records.append(dataclasses.replace(record, evidences=tuple(evidences)))
    return seal_world(dataclasses.replace(world, records=records, blobs={})), keys


@dataclass
class PoolPorts(WorldPorts):
    """Los puertos del mundo con los clips leídos de LocalStack (``S3Storage.get_object``)."""

    storage: Any = None
    keys: dict[Any, str] = field(default_factory=dict)

    async def clip_bytes(self, context: ScopeContext, evidence: EvidenceReference) -> bytes:
        self.calls["clip_bytes"] += 1
        data: bytes = await self.storage.get_object(self.keys[evidence.evidence_id])
        return data


class Sampler:
    """El uso máximo del directorio temporal del proceso, en un hilo cada ``SAMPLE_SECONDS``."""

    def __init__(self) -> None:
        self.root = Path(tempfile.gettempdir())
        self.tmp_max = self.usage()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def usage(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:  # un archivo que desaparece mientras se cuenta
                continue
        return total

    def sample(self) -> None:
        self.tmp_max = max(self.tmp_max, self.usage())

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_SECONDS):
            self.sample()

    def __enter__(self) -> Sampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        self.sample()


class _SampledSink:
    """El ``ExportSink`` del consumidor, con una muestra de ``/tmp`` al terminar cada artefacto."""

    def __init__(self, sink: ExportSink, sampler: Sampler) -> None:
        self._sink = sink
        self._sampler = sampler

    async def upload_started(self, multipart_upload_id: str) -> None:
        await self._sink.upload_started(multipart_upload_id)

    async def artifact_started(self, artifact: str) -> None:
        await self._sink.artifact_started(artifact)

    async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
        self._sampler.sample()
        await self._sink.artifact_finished(artifact, done, total)


class LoadComposer:
    """El ``PackageComposer`` del consumidor: el compositor real sobre el mundo de la carga."""

    def __init__(
        self, stream: ExportStream, storage: S3Storage, findings: int, pool: ClipPool
    ) -> None:
        self._stream = stream
        self._storage = storage
        self._findings = findings
        self._pool = pool
        self.sampler: Sampler | None = None
        self.jobs: list[ExportJob] = []
        self.ports: PoolPorts | None = None
        self.world_seconds = 0.0

    def ports_for(self, job: ExportJob) -> PoolPorts:
        if self.ports is None:
            began = WALL.monotonic()
            world, keys = load_world(job, self._findings, self._pool)
            self.world_seconds = round(WALL.monotonic() - began, 3)
            self.ports = PoolPorts(world, storage=self._storage, keys=keys)
        return self.ports

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        self.jobs.append(job)
        composer = ExportComposer(stream=self._stream, ports=self.ports_for(job).ports())
        if self.sampler is not None:
            sink = _SampledSink(sink, self.sampler)
        return await composer.compose(job, sink)


@dataclass
class CountingStore:
    """``ExportObjectStore`` que solo cuenta: la segunda corrida no vuelve a subir el paquete."""

    parts: int = 0
    bytes: int = 0

    async def create_multipart(
        self, key: str, content_type: str, *, metadata: Mapping[str, str] | None = None
    ) -> str:
        return uuid.uuid4().hex

    async def upload_part(
        self, key: str, upload_id: str, part_number: int, body: bytes
    ) -> CompletedPart:
        self.parts += 1
        self.bytes += len(body)
        return CompletedPart(part_number, f'"{part_number}"', "0" * 64)

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectHead:
        return ObjectHead(
            key=key,
            size_bytes=0,
            checksum_sha256=None,
            checksum_type=ChecksumType.COMPOSITE,
            content_type=None,
            metadata={},
            version_id="segunda-corrida",
        )

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        return None

    async def pending_multipart_uploads(self, prefix: str) -> tuple[tuple[str, str], ...]:
        return ()


class _NoStorage:
    """El escritor del cierre no lee el almacén: cualquier llamada falla."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"el cierre de la exportación no toca el almacén ({name})")


@dataclass(frozen=True)
class _EventRef:
    organization_id: uuid.UUID
    correlation_id: uuid.UUID


async def _noop(event: OutboxEvent, transaction: Transaction) -> None:
    raise AssertionError("el hijo no despacha eventos")


async def _writer(database: Database, clock: SystemClock) -> EscritorExpediente:
    """El escritor de ``export_stack``: los tipos de U-02 y de la acreditación (ya en la base) y el
    mismo catálogo de la bandeja, sincronizado con el de la base (así queda sellado)."""
    registry = RecordTypeRegistry()
    for definition in U02_RECORD_TYPES:
        registry.register(definition)
    register_accreditation_record_types(registry)
    registry.seal()
    catalog = OutboxCatalog()
    register_u02_event_types(catalog.event_types)
    register_accreditation_event_types(catalog.event_types)
    register_export_worker(catalog.consumers, _noop)  # type: ignore[arg-type]
    system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
    async with database.transaction(system) as transaction:
        await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)
    free_text = FreeTextPolicyRegistry()
    free_text.seal()
    return EscritorExpediente(
        database=database,
        registry=registry,
        free_text=free_text,
        evidence=EvidenceVerifier(_NoStorage(), clock),  # type: ignore[arg-type]
        outbox=Outbox(catalog, clock),
        clock=clock,
    )


def _rss_kib() -> int:
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


async def _main(spec: Mapping[str, Any]) -> dict[str, Any]:
    clock = SystemClock()
    database = Database.create(
        DatabaseSettings(
            url=spec["database_url"],
            process=ProcessKind.WORKER,
            sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS
            worker_pool_size=4,
            lock_timeout_ms=LOCK_TIMEOUT_MS,
        )
    )
    storage = S3Storage(
        StorageSettings(
            bucket=spec["bucket"],
            endpoint_url=spec["endpoint"],
            region="us-east-1",
            addressing_style=AddressingStyle.PATH,
            read_timeout_seconds=120.0,
            credentials=StorageCredentials("test", "test"),
        ),
        clock,
    )
    stream = ExportStream(store=storage)  # partes de 16 MiB y volúmenes de 2 GB
    pool = ClipPool.from_json(spec["clips"])
    composer = LoadComposer(stream, storage, int(spec["findings"]), pool)
    worker = ExportWorker(
        leases=SqlExportLeaseStore(database=database, writer=await _writer(database, clock)),
        checkpoints=CountingCheckpoints(),
        uploads=stream,
        composer=composer,
        alarm=RecordingAlarm(),
        clock=clock,
        owner=f"carga-{os.getpid()}",
    )
    organization_id = uuid.UUID(spec["organization_id"])
    context = system_contexts(
        clock, uuid.UUID(spec["provider_organization_id"])
    ).context_from_event(_EventRef(organization_id, uuid7()), unit=ActorUnit.U04)
    base_rss = _rss_kib()
    # Lo que el intento retiene en objetos de Python (los trozos, las partes, los clips y los
    # búferes del ZIP son ``bytes``/``bytearray``): el máximo de lo vivo a la vez desde aquí.
    tracemalloc.start()
    started = WALL.monotonic()
    try:
        with Sampler() as sampler:
            composer.sampler = sampler
            await worker.run(context, uuid.UUID(spec["export_id"]))
        duration = WALL.monotonic() - started
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        python_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        (job,) = composer.jobs
        ports = composer.ports_for(job)
        clip_reads = ports.calls["clip_bytes"]
        # Segunda corrida del mismo trabajo, hacia un almacén que solo cuenta.
        counting = CountingStore()
        again = ExportComposer(stream=ExportStream(store=counting), ports=ports.ports())
        began = WALL.monotonic()
        rerun = await again.compose(job, RecordingSink())
        rerun_seconds = WALL.monotonic() - began
    finally:
        await database.dispose()
    return {
        "base_rss_kib": base_rss,
        "max_rss_kib": max_rss,
        "tmp_max_bytes": sampler.tmp_max,
        "tmpdir": str(sampler.root),
        "duration_seconds": round(duration, 3),
        "world_seconds": composer.world_seconds,
        "rerun_seconds": round(rerun_seconds, 3),
        "clip_reads": clip_reads,
        "findings": int(spec["findings"]),
        "rerun": {
            "archive_sha256": rerun.archive_sha256,
            "manifest_sha256": rerun.manifest_sha256,
            "document_sha256": rerun.document_sha256,
            "size_bytes": rerun.size_bytes,
            "parts": counting.parts,
        },
        "python_peak_kib": python_peak // 1024,
        "pid": os.getpid(),
    }


if __name__ == "__main__":
    result = asyncio.run(_main(json.loads(sys.argv[1])))
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
