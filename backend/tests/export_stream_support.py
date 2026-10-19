"""El flujo de bytes de la exportación (TASK-345) con el compositor sintético determinista.

- ``SyntheticScope``: los artefactos de un paquete sintético (ruta, tipo, tamaño y si se
  comprime) más el documento portátil. ``scope_from_job`` lo deriva **solo** del ``ExportJob``
  (sin el número de intento), así que la misma solicitud da el mismo paquete en cualquier intento.
- ``synthetic_chunks(seed, size)``: bytes generados en flujo, en trozos de 1 MiB: un bloque de
  ``shake_256`` por artefacto con el número del trozo delante. Nunca hay un archivo en disco ni el
  artefacto entero en memoria.
- ``StreamingComposer``: el ``PackageComposer`` sintético que **escribe bytes de verdad** con
  ``ExportStream``: un artefacto tras otro, el manifiesto con la lista de artefactos y sus hashes
  y el ``ExportPackage`` con ``archive_sha256`` y ``stored``. Avisa al ``ExportSink`` al empezar y
  al terminar cada artefacto (el gancho de progreso de las pruebas va en el ``ExportSink``).
- ``MemoryExportStore``: un ``ExportObjectStore`` en memoria (sumas por parte comprobadas, objetos
  con versión, subidas pendientes), para la ejecución de referencia sin interrupción.
- ``RecordingSink``: un ``ExportSink`` que anota la subida guardada y el progreso, con un gancho
  opcional por artefacto terminado.

Solo datos generados.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from vigia_platform.accreditation.export.composer_port import (
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.stream import (
    SLICE_BYTES,
    ArchiveResult,
    Artifact,
    ExportStream,
)
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import ChecksumType, CompletedPart, ObjectHead

__all__ = [
    "MemoryExportStore",
    "RecordingSink",
    "StreamingComposer",
    "SyntheticArtifact",
    "SyntheticScope",
    "job_from_json",
    "job_to_json",
    "read_volume",
    "scope_from_json",
    "scope_to_json",
    "synthetic_chunks",
    "synthetic_job",
]

MIB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SyntheticArtifact:
    path: str
    kind: str
    size_bytes: int
    compress: bool = False


@dataclass(frozen=True, slots=True)
class SyntheticScope:
    """El contenido de un paquete sintético: artefactos más el documento portátil."""

    artifacts: tuple[SyntheticArtifact, ...]
    document_size: int = 4096


def scope_to_json(scope: SyntheticScope) -> dict[str, Any]:
    return {
        "artifacts": [
            [item.path, item.kind, item.size_bytes, item.compress] for item in scope.artifacts
        ],
        "document_size": scope.document_size,
    }


def scope_from_json(value: Mapping[str, Any]) -> SyntheticScope:
    return SyntheticScope(
        tuple(SyntheticArtifact(*item) for item in value["artifacts"]), value["document_size"]
    )


def synthetic_job(
    *,
    organization_id: uuid.UUID | None = None,
    plant_id: uuid.UUID | None = None,
    export_id: uuid.UUID | None = None,
    attempt: int = 1,
) -> ExportJob:
    generated = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    return ExportJob(
        export_id=export_id or uuid.uuid4(),
        organization_id=organization_id or uuid.uuid4(),
        plant_id=plant_id or uuid.uuid4(),
        zone_ids=(uuid.UUID(int=1),),
        period_from=datetime(2026, 9, 1, tzinfo=UTC),
        period_to=datetime(2026, 10, 1, tzinfo=UTC),
        package_form=PackageForm.WITHOUT_CLIPS,
        requested_by={
            "user_id": str(uuid.UUID(int=2)),
            "display_name": "Persona",
            "role_in_use": None,
        },
        correlation_id=uuid.UUID(int=3),
        attempt=attempt,
        generated_at=generated,
        checkpoint_record_ids=(uuid.UUID(int=4),),
    )


def job_to_json(job: ExportJob) -> dict[str, Any]:
    return {
        "export_id": str(job.export_id),
        "organization_id": str(job.organization_id),
        "plant_id": str(job.plant_id),
        "attempt": job.attempt,
    }


def job_from_json(value: Mapping[str, Any]) -> ExportJob:
    return synthetic_job(
        organization_id=uuid.UUID(value["organization_id"]),
        plant_id=uuid.UUID(value["plant_id"]),
        export_id=uuid.UUID(value["export_id"]),
        attempt=int(value["attempt"]),
    )


def _job_seed(job: ExportJob) -> bytes:
    """Solo del alcance y de lo fijado en el primer intento; nunca del número de intento."""
    document = {
        "export_id": str(job.export_id),
        "organization_id": str(job.organization_id),
        "plant_id": str(job.plant_id),
        "zone_ids": [str(zone) for zone in job.zone_ids],
        "period": [format_timestamp(job.period_from), format_timestamp(job.period_to)],
        "generated_at": format_timestamp(job.generated_at),
        "checkpoints": [str(value) for value in job.checkpoint_record_ids],
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).digest()


def synthetic_chunks(seed: bytes, size: int, chunk: int = SLICE_BYTES) -> Iterator[bytes]:
    """``size`` bytes deterministas, generados en flujo de ``chunk`` en ``chunk``."""
    block = hashlib.shake_256(seed).digest(chunk)
    produced, index = 0, 0
    while produced < size:
        take = min(chunk, size - produced)
        piece = index.to_bytes(8, "big") + block[8:]
        yield piece[:take]
        produced += take
        index += 1


def _document_bytes(seed: bytes, size: int) -> bytes:
    header = b"%PDF-1.7\n% sintetico\n"
    return (header + hashlib.shake_256(seed + b"document").digest(max(0, size - len(header))))[
        :size
    ]


@dataclass
class StreamingComposer:
    """``PackageComposer`` sintético determinista que escribe los bytes con ``ExportStream``."""

    stream: ExportStream
    scope: SyntheticScope | Callable[[ExportJob], SyntheticScope]
    results: list[ArchiveResult] = field(default_factory=list)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[ExportJob] = field(default_factory=list)

    def scope_of(self, job: ExportJob) -> SyntheticScope:
        return self.scope(job) if callable(self.scope) else self.scope

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        self.jobs.append(job)
        scope = self.scope_of(job)
        seed = _job_seed(job)
        total = len(scope.artifacts) + 1
        async with self.stream.writer(job, sink) as writer:
            await sink.artifact_started("document")
            await writer.add_bytes(
                Artifact(
                    path="document.pdf",
                    kind="document",
                    size_bytes=scope.document_size,
                    document=True,
                ),
                _document_bytes(seed, scope.document_size),
            )
            await sink.artifact_finished("document", 1, total)
            for index, item in enumerate(scope.artifacts, start=2):
                await sink.artifact_started(item.kind)
                await writer.add(
                    Artifact(
                        path=item.path,
                        kind=item.kind,
                        size_bytes=item.size_bytes,
                        compress=item.compress,
                    ),
                    synthetic_chunks(seed + item.path.encode(), item.size_bytes),
                )
                await sink.artifact_finished(item.kind, index, total)
            manifest: dict[str, Any] = {
                "manifest_version": 1,
                "export_id": str(job.export_id),
                "generated_at": format_timestamp(job.generated_at),
                "package_form": job.package_form.value,
                "artifacts": [
                    {
                        "path": written.path,
                        "kind": written.kind,
                        "sha256": written.sha256,
                        "size_bytes": written.size_bytes,
                    }
                    for written in writer.artifacts
                ],
            }
            result = await writer.finish(manifest)
        self.results.append(result)
        self.manifests.append(manifest)
        return ExportPackage(
            document_sha256=result.document_sha256 or "0" * 64,
            archive_sha256=result.archive_sha256,
            manifest=manifest,
            size_bytes=result.size_bytes,
            artifact_count=len(result.artifacts),
            verifier_version="1.0.0",
            stored=result.stored,
        )


@dataclass
class RecordingSink:
    """``ExportSink`` que anota la subida guardada y el progreso; ``hook`` tras cada artefacto."""

    uploads: list[str] = field(default_factory=list)
    progress: list[int] = field(default_factory=list)
    hook: Callable[[int], Awaitable[None]] | None = None

    async def upload_started(self, multipart_upload_id: str) -> None:
        self.uploads.append(multipart_upload_id)

    async def artifact_started(self, artifact: str) -> None:
        return None

    async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
        percent = done * 100 // total
        self.progress.append(percent)
        if self.hook is not None:
            await self.hook(percent)


# --- Almacén en memoria ---------------------------------------------------------------------------


@dataclass
class _OpenUpload:
    key: str
    content_type: str
    parts: dict[int, bytes] = field(default_factory=dict)


@dataclass
class MemoryExportStore:
    """``ExportObjectStore`` en memoria: comprueba la suma de cada parte y versiona objetos."""

    uploads: dict[str, _OpenUpload] = field(default_factory=dict)
    objects: dict[str, list[tuple[str, bytes]]] = field(default_factory=dict)
    aborted: list[str] = field(default_factory=list)

    async def create_multipart(
        self, key: str, content_type: str, *, metadata: Mapping[str, str] | None = None
    ) -> str:
        upload_id = uuid.uuid4().hex
        self.uploads[upload_id] = _OpenUpload(key, content_type)
        return upload_id

    async def upload_part(
        self, key: str, upload_id: str, part_number: int, body: bytes
    ) -> CompletedPart:
        upload = self.uploads[upload_id]
        assert upload.key == key
        upload.parts[part_number] = bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        return CompletedPart(part_number, f'"{digest[:32]}"', digest)

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectHead:
        upload = self.uploads.pop(upload_id)
        numbers = [part.part_number for part in parts]
        assert numbers == sorted(upload.parts), "faltan partes o sobran"
        for part in parts:
            body = upload.parts[part.part_number]
            assert hashlib.sha256(body).hexdigest() == part.checksum_sha256
        data = b"".join(upload.parts[number] for number in numbers)
        version = uuid.uuid4().hex
        self.objects.setdefault(key, []).append((version, data))
        return ObjectHead(
            key=key,
            size_bytes=len(data),
            checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode(),
            checksum_type=ChecksumType.COMPOSITE,
            content_type=upload.content_type,
            metadata={},
            version_id=version,
        )

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        upload = self.uploads.pop(upload_id, None)
        if upload is None:
            error = RuntimeError("NoSuchUpload")
            error.response = {"Error": {"Code": "NoSuchUpload"}}  # type: ignore[attr-defined]
            raise error
        self.aborted.append(upload_id)

    async def pending_multipart_uploads(self, prefix: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (upload.key, upload_id)
            for upload_id, upload in self.uploads.items()
            if upload.key.startswith(prefix)
        )

    def latest(self, key: str) -> bytes:
        return self.objects[key][-1][1]


def read_volume(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))
