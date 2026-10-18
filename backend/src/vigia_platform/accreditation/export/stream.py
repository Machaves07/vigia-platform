"""Paquete exportado en flujo hacia el almacén (TASK-345; LC-LAZ-22; PAT-LAZ-REN-03, RES-01 y
MAN-02; NFR-LAZ-10, 12, 15 y 49; infraestructura §6.2, §7.1 y H-LAZ-6).

El compositor (TASK-318; en las pruebas, el sintético determinista) abre un ``ArchiveWriter`` con
``ExportStream.writer(job, sink)`` y le pasa los artefactos **en flujo**, trozo a trozo:

- Cada volumen es un ZIP escrito con ``zipfile`` de la biblioteca estándar sobre un **destino no
  reposicionable** (un objeto de archivo sin ``tell`` ni ``seek``): ``zipfile`` lo detecta y
  escribe cada entrada con **descriptor de datos** (bit 3), con la suma CRC y los tamaños detrás
  de los datos. Es la ``[hipótesis]`` de ``tech-stack-decisions.md`` §3, comprobada en
  ``tests/integration/test_export_multipart_localstack.py`` abriendo los volúmenes con
  ``zipfile`` y con ``unzip -t``. Sin ZIP64: un volumen no llega a 4 GiB.
- Los bytes de cada volumen van a una **subida por partes** propia (``create_multipart``), en
  partes de ``PART_SIZE`` (16 MiB) con su **suma SHA-256 por parte** (``upload_part``) y la lista
  de sumas al completar (``complete_multipart``): H-LAZ-6 con la contingencia de cabecera por
  parte. En memoria vive a lo sumo una parte pendiente por subida abierta más un trozo de 1 MiB;
  **nada se escribe en disco** (ni en ``/tmp``): la memoria no crece con el paquete (NFR-LAZ-49).
- **Volúmenes** de ``VOLUME_MAX_BYTES`` (2 GB): una entrada nunca se parte; si no cabe en el
  volumen en curso, abre el siguiente. ``vol-01.zip`` guarda sitio para el **manifiesto**, que
  ``finish`` escribe en él al final (con ``canonical_bytes`` de U-02).
- El documento portátil (``Artifact.document``) va dentro del paquete y, además, a su propio
  objeto ``document.pdf`` (la URL de SUP-U05-23 apunta a un objeto).
- **Ningún objeto se publica antes de tiempo**: todas las subidas siguen abiertas hasta
  ``finish``, que cierra los ZIP y completa las subidas; antes no existe ningún ``vol-*.zip``
  completo. El paquete solo se publica cuando el consumidor lo pasa a ``ready`` (TASK-317).
- **Abandono**: si la composición falla o se cancela dentro del ``async with``, se abortan todas
  las subidas abiertas. Si el proceso muere (sin ``finally``), la retoma llama a
  ``abort_previous_upload(location, multipart_upload_id)``: aborta la subida guardada en
  ``ExportRequest`` (la de ``vol-01``, que el ``ExportSink`` recibe en ``upload_started``) por su
  identificador y, después, las demás subidas **sin terminar** del prefijo de esa exportación
  (``pending_multipart_uploads``, ``s3:ListBucketMultipartUploads`` de la infraestructura §9.1:
  subidas, nunca objetos). Así no queda ningún fragmento pagándose.
- **Un tramo de traza por artefacto** (``accreditation.export.artifact``) con
  ``long_job_attributes``: ``correlation_id``, ``export_request_id`` y el tipo de artefacto; nada
  de texto libre ni usuario.

La compresión, los hashes y la escritura del ZIP corren en el pool de hilos (PAT-NUC-REN-05);
las llamadas al almacén, en el suyo (``S3Storage``).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import re
import zipfile
from collections.abc import AsyncIterable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from types import TracebackType
from typing import IO, Any, Final, Protocol, Self, cast

from opentelemetry import trace as otel_trace
from pydantic import BaseModel, JsonValue

from vigia_platform.accreditation.export.composer_port import ExportJob, ExportSink
from vigia_platform.accreditation.export.volumes import (
    DOCUMENT_CONTENT_TYPE,
    MANIFEST_NAME,
    MANIFEST_RESERVE_BYTES,
    MAX_VOLUMES,
    MIN_PART_SIZE,
    PART_SIZE,
    VOLUME_CONTENT_TYPE,
    VOLUME_MAX_BYTES,
    ExportLocation,
    StoredObject,
    StoredObjects,
    Volume,
    archive_sha256,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.loop.observability.metrics import long_job_attributes, register_long_job_values
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name
from vigia_platform.shared.storage import CompletedPart, ObjectHead

__all__ = [
    "ARTIFACT_KINDS",
    "EXPORT_ARTIFACT_SPAN",
    "SLICE_BYTES",
    "ArchiveResult",
    "ArchiveWriter",
    "Artifact",
    "ExportObjectStore",
    "ExportStream",
    "ExportStreamSettings",
    "WrittenArtifact",
]

ARTIFACT_KINDS: Final = (
    "document",
    "findings_json",
    "clip",
    "attachment",
    "coverage",
    "audit",
    "checkpoint",
    "public_key",
    "verifier",
    "manifest",
)
"""``kind`` de los artefactos del manifiesto (DE §3.2) más el propio manifiesto."""

EXPORT_ARTIFACT_SPAN: Final = span_name("accreditation.export.artifact")
register_long_job_values(artifacts=ARTIFACT_KINDS)

SLICE_BYTES: Final = 1024 * 1024
"""Lo que se escribe de una vez en el ZIP: un trozo del llamador se parte en trozos de 1 MiB."""
ZIP_DATE_TIME: Final = (1980, 1, 1, 0, 0, 0)
"""Fecha fija de las entradas: el ZIP no depende del reloj (PR-LAZ-49)."""
_ZIP_FILE_MODE: Final = 0o100644 << 16
_ZIP_UNIX: Final = 3
_ZIP_LIMIT: Final = 0xFFFFFFFF - 1024 * 1024
"""Un volumen nunca necesita ZIP64 (``allowZip64=False``)."""
_LOCAL_HEADER: Final = 30
_CENTRAL_HEADER: Final = 46
_DATA_DESCRIPTOR: Final = 16
_END_OF_DIRECTORY: Final = 22
_MAX_PATH: Final = 255
_PATH: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")

_tracer_default: Final = otel_trace.get_tracer(TRACER_NAME)


class ExportObjectStore(Protocol):
    """Lo que el flujo usa del almacén (``S3Storage``): subida por partes, nunca listar objetos."""

    async def create_multipart(
        self, key: str, content_type: str, *, metadata: Mapping[str, str] | None = None
    ) -> str: ...

    async def upload_part(
        self, key: str, upload_id: str, part_number: int, body: bytes
    ) -> CompletedPart: ...

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[CompletedPart]
    ) -> ObjectHead: ...

    async def abort_multipart(self, key: str, upload_id: str) -> None: ...

    async def pending_multipart_uploads(self, prefix: str) -> tuple[tuple[str, str], ...]: ...


@dataclass(frozen=True, slots=True)
class ExportStreamSettings:
    """Tamaños del flujo; los de producción son los del diseño (``volumes``)."""

    part_size: int = PART_SIZE
    volume_max_bytes: int = VOLUME_MAX_BYTES
    manifest_reserve_bytes: int = MANIFEST_RESERVE_BYTES

    def __post_init__(self) -> None:
        if type(self.part_size) is not int or self.part_size < MIN_PART_SIZE:
            raise ValueError("part_size no llega al mínimo del servicio (5 MiB)")
        if not 0 < self.manifest_reserve_bytes < self.volume_max_bytes <= _ZIP_LIMIT:
            raise ValueError("el volumen debe superar la reserva del manifiesto y no pedir ZIP64")


@dataclass(frozen=True, slots=True, kw_only=True)
class Artifact:
    """Una entrada del paquete: su ruta dentro del ZIP, su tipo y su tamaño declarado."""

    path: str
    kind: str
    size_bytes: int
    compress: bool = False
    """``True`` para lo que se comprime bien (JSON); los clips y el PDF van sin comprimir."""
    document: bool = False
    """El documento portátil: también se escribe como objeto propio (SUP-U05-23)."""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, str)
            or len(self.path) > _MAX_PATH
            or _PATH.fullmatch(self.path) is None
            or ".." in self.path.split("/")
            or self.path == MANIFEST_NAME
        ):
            raise ValueError("ruta de artefacto no válida")
        if self.kind not in ARTIFACT_KINDS or self.kind == "manifest":
            raise ValueError("tipo de artefacto desconocido")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes debe ser un entero no negativo")
        if self.document and (self.kind != "document" or self.size_bytes == 0):
            raise ValueError("solo un artefacto document no vacío se publica como documento")

    def bound(self) -> int:
        """Cota de lo que ocupa en su volumen: cabeceras, datos y entrada del directorio."""
        data = self.size_bytes + self.size_bytes // 1000 + 64 if self.compress else self.size_bytes
        name = len(self.path.encode("ascii"))
        return _LOCAL_HEADER + _CENTRAL_HEADER + _DATA_DESCRIPTOR + 2 * name + data


@dataclass(frozen=True, slots=True, kw_only=True)
class WrittenArtifact:
    """Un artefacto ya escrito: lo que el manifiesto declara de él."""

    path: str
    kind: str
    sha256: str
    size_bytes: int
    volume: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveResult:
    """El paquete escrito y completado."""

    archive_sha256: str
    size_bytes: int
    """Suma de los volúmenes."""
    artifacts: tuple[WrittenArtifact, ...]
    manifest_sha256: str
    document_sha256: str | None
    stored: StoredObjects


def _no_such_upload(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return isinstance(response, Mapping) and response.get("Error", {}).get("Code") == "NoSuchUpload"


class ExportStream:
    """El ``UploadPort`` del consumidor y la fábrica de ``ArchiveWriter``."""

    def __init__(
        self,
        *,
        store: ExportObjectStore,
        settings: ExportStreamSettings | None = None,
        executor: Executor | None = None,
        tracer: otel_trace.Tracer | None = None,
    ) -> None:
        self._store = store
        self.settings = settings if settings is not None else ExportStreamSettings()
        self._executor = executor
        self._tracer = tracer if tracer is not None else _tracer_default

    @property
    def store(self) -> ExportObjectStore:
        return self._store

    async def abort_previous_upload(
        self, location: ExportLocation, multipart_upload_id: str | None
    ) -> None:
        """Abandona lo que dejó el intento anterior: su subida por su identificador y, después,
        cualquier otra subida sin terminar de esa exportación (los demás volúmenes y el
        documento)."""
        if multipart_upload_id is not None:
            try:
                await self._store.abort_multipart(location.volume_key(1), multipart_upload_id)
            except Exception as error:
                if not _no_such_upload(error):
                    raise
        for key, upload_id in await self._store.pending_multipart_uploads(location.prefix):
            if upload_id == multipart_upload_id:
                continue
            try:
                await self._store.abort_multipart(key, upload_id)
            except Exception as error:
                if not _no_such_upload(error):
                    raise

    def writer(self, job: ExportJob, sink: ExportSink) -> ArchiveWriter:
        """El escritor del paquete de ``job``: úsalo con ``async with``."""
        return ArchiveWriter(
            store=self._store,
            location=ExportLocation(job.organization_id, job.plant_id, job.export_id),
            job=job,
            sink=sink,
            settings=self.settings,
            executor=self._executor,
            tracer=self._tracer,
        )


class _Upload:
    """Una subida por partes alimentada por ``write`` (síncrono, en el pool) y ``drain``."""

    def __init__(self, store: ExportObjectStore, key: str, content_type: str, part_size: int):
        self._store = store
        self.key = key
        self._content_type = content_type
        self._part_size = part_size
        self._pending = bytearray()
        self._parts: list[CompletedPart] = []
        self._hasher = hashlib.sha256()
        self.upload_id: str | None = None
        self.size = 0
        self.head: ObjectHead | None = None

    # Objeto de archivo de ``zipfile``: solo ``write`` y ``flush`` (ni ``tell`` ni ``seek``).
    def write(self, data: bytes | memoryview) -> int:
        self._pending += data
        self._hasher.update(data)
        self.size += len(data)
        return len(data)

    def flush(self) -> None:
        return None

    @property
    def sha256(self) -> str:
        return self._hasher.hexdigest()

    async def open(self) -> str:
        self.upload_id = await self._store.create_multipart(self.key, self._content_type)
        return self.upload_id

    async def drain(self, *, final: bool = False) -> None:
        """Sube cada parte completa; con ``final``, también la última (más corta)."""
        if self.upload_id is None:
            raise RuntimeError("la subida no está abierta")
        while len(self._pending) >= self._part_size or (final and self._pending):
            body = bytes(self._pending[: self._part_size])
            del self._pending[: self._part_size]
            part = await self._store.upload_part(
                self.key, self.upload_id, len(self._parts) + 1, body
            )
            self._parts.append(part)

    async def complete(self) -> ObjectHead:
        await self.drain(final=True)
        if self.upload_id is None or not self._parts:
            raise RuntimeError("una subida por partes necesita al menos una parte")
        self.head = await self._store.complete_multipart(self.key, self.upload_id, self._parts)
        return self.head

    async def abort(self) -> None:
        if self.upload_id is None or self.head is not None:
            return
        upload_id, self.upload_id = self.upload_id, None
        try:
            await self._store.abort_multipart(self.key, upload_id)
        except Exception as error:
            if not _no_such_upload(error):
                raise


class _Volume:
    """Un volumen ZIP abierto sobre su subida por partes."""

    def __init__(self, number: int, upload: _Upload) -> None:
        self.number = number
        self.upload = upload
        # Sin ``tell``: ``zipfile`` usa descriptores de datos (destino no reposicionable).
        self.archive = zipfile.ZipFile(cast(IO[bytes], upload), mode="w", allowZip64=False)
        self.directory = 0
        """Bytes que añadirá el directorio central de lo ya escrito."""
        self.entries = 0

    def room(self, limit: int) -> int:
        return limit - self.upload.size - self.directory - _END_OF_DIRECTORY


class ArchiveWriter:
    """El paquete de una exportación mientras se escribe (``async with``)."""

    def __init__(
        self,
        *,
        store: ExportObjectStore,
        location: ExportLocation,
        job: ExportJob,
        sink: ExportSink,
        settings: ExportStreamSettings,
        executor: Executor | None,
        tracer: otel_trace.Tracer,
    ) -> None:
        self._store = store
        self.location = location
        self._job = job
        self._sink = sink
        self._settings = settings
        self._executor = executor
        self._tracer = tracer
        self._volumes: list[_Volume] = []
        self._document: _Upload | None = None
        self._artifacts: list[WrittenArtifact] = []
        self._paths: set[str] = set()
        self._finished = False

    @property
    def artifacts(self) -> tuple[WrittenArtifact, ...]:
        """Los artefactos escritos hasta ahora, en orden (para componer el manifiesto)."""
        return tuple(self._artifacts)

    async def _run[T](self, function: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._executor, function)

    def _uploads(self) -> list[_Upload]:
        uploads = [volume.upload for volume in self._volumes]
        if self._document is not None:
            uploads.append(self._document)
        return uploads

    async def __aenter__(self) -> Self:
        await self._open_volume()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._finished:
            await self._abandon()

    async def _abandon(self) -> None:
        """Aborta toda subida abierta (fallo o cancelación de la composición)."""
        self._finished = True
        for upload in self._uploads():
            try:
                await upload.abort()
            except Exception:  # noqa: S112 - se intenta con las demás; la retoma barre el resto
                continue

    async def _open_volume(self) -> _Volume:
        number = len(self._volumes) + 1
        if number > MAX_VOLUMES:
            raise ValueError("el paquete no cabe en 99 volúmenes")
        upload = _Upload(
            self._store,
            self.location.volume_key(number),
            VOLUME_CONTENT_TYPE,
            self._settings.part_size,
        )
        upload_id = await upload.open()
        volume = _Volume(number, upload)
        self._volumes.append(volume)
        if number == 1:
            # La subida que se guarda en ``ExportRequest`` para abortarla al retomar.
            await self._sink.upload_started(upload_id)
        return volume

    async def _volume_for(self, artifact: Artifact) -> _Volume:
        """El volumen en curso si la entrada cabe entera; si no, uno nuevo (nunca se parte)."""
        limit = self._settings.volume_max_bytes
        current = self._volumes[-1]
        reserve = self._settings.manifest_reserve_bytes if current.number == 1 else 0
        if artifact.bound() <= current.room(limit) - reserve:
            return current
        if artifact.bound() > limit - _END_OF_DIRECTORY:
            raise ValueError("el artefacto no cabe en un volumen")
        return await self._open_volume()

    async def add(
        self,
        artifact: Artifact,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> WrittenArtifact:
        """Escribe ``artifact`` con los bytes de ``chunks`` (exactamente ``size_bytes``)."""
        if self._finished:
            raise RuntimeError("el paquete ya está cerrado")
        if artifact.path in self._paths:
            raise ValueError("ruta de artefacto repetida")
        if artifact.document and self._document is not None:
            raise ValueError("el paquete lleva un solo documento")
        with self._tracer.start_as_current_span(
            EXPORT_ARTIFACT_SPAN, attributes=self._span_attributes(artifact.kind)
        ):
            volume = await self._volume_for(artifact)
            if artifact.document:
                self._document = _Upload(
                    self._store,
                    self.location.document_key,
                    DOCUMENT_CONTENT_TYPE,
                    self._settings.part_size,
                )
                await self._document.open()
            written = await self._write(volume, artifact, chunks)
        self._paths.add(artifact.path)
        self._artifacts.append(written)
        return written

    async def add_bytes(self, artifact: Artifact, data: bytes) -> WrittenArtifact:
        return await self.add(artifact, (data,))

    async def add_canonical(
        self, path: str, kind: str, document: BaseModel | JsonValue
    ) -> WrittenArtifact:
        """Un artefacto canónico (JSON RFC 8785 con ``canonical_bytes`` de U-02), comprimido."""
        data = await self._run(functools.partial(canonical_bytes_sync, document))
        return await self.add_bytes(
            Artifact(path=path, kind=kind, size_bytes=len(data), compress=True), data
        )

    def _span_attributes(self, kind: str) -> dict[str, Any]:
        return long_job_attributes(
            {
                "correlation_id": str(self._job.correlation_id),
                "export_request_id": str(self._job.export_id),
                "artifact": kind,
            }
        )

    async def _write(
        self,
        volume: _Volume,
        artifact: Artifact,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
    ) -> WrittenArtifact:
        info = zipfile.ZipInfo(artifact.path, date_time=ZIP_DATE_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED if artifact.compress else zipfile.ZIP_STORED
        info.create_system = _ZIP_UNIX
        info.external_attr = _ZIP_FILE_MODE
        info.file_size = artifact.size_bytes
        entry = await self._run(functools.partial(volume.archive.open, info, mode="w"))
        hasher = hashlib.sha256()
        document = self._document if artifact.document else None
        written = 0

        def write(piece: memoryview) -> None:
            entry.write(piece)
            hasher.update(piece)
            if document is not None:
                document.write(piece)

        try:
            async for chunk in _chunks(chunks):
                view = memoryview(chunk)
                for start in range(0, len(view), SLICE_BYTES):
                    piece = view[start : start + SLICE_BYTES]
                    written += len(piece)
                    if written > artifact.size_bytes:
                        raise ValueError("el artefacto supera su tamaño declarado")
                    await self._run(functools.partial(write, piece))
                    await volume.upload.drain()
                    if document is not None:
                        await document.drain()
            if written != artifact.size_bytes:
                raise ValueError("el artefacto no llega a su tamaño declarado")
        finally:
            await self._run(entry.close)
        await volume.upload.drain()
        volume.directory += _CENTRAL_HEADER + len(artifact.path.encode("ascii"))
        volume.entries += 1
        return WrittenArtifact(
            path=artifact.path,
            kind=artifact.kind,
            sha256=hasher.hexdigest(),
            size_bytes=artifact.size_bytes,
            volume=volume.number,
        )

    async def finish(self, manifest: BaseModel | JsonValue) -> ArchiveResult:
        """Escribe el manifiesto en ``vol-01``, cierra los ZIP y completa todas las subidas."""
        if self._finished:
            raise RuntimeError("el paquete ya está cerrado")
        data = await self._run(functools.partial(canonical_bytes_sync, manifest))
        first = self._volumes[0]
        bound = (
            _LOCAL_HEADER
            + _CENTRAL_HEADER
            + _DATA_DESCRIPTOR
            + 2 * len(MANIFEST_NAME)
            + len(data)
            + len(data) // 1000
            + 64
        )
        if bound > first.room(self._settings.volume_max_bytes):
            raise ValueError("el manifiesto no cabe en vol-01")
        with self._tracer.start_as_current_span(
            EXPORT_ARTIFACT_SPAN, attributes=self._span_attributes("manifest")
        ):
            info = zipfile.ZipInfo(MANIFEST_NAME, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _ZIP_UNIX
            info.external_attr = _ZIP_FILE_MODE
            await self._run(functools.partial(first.archive.writestr, info, data))
        volumes: list[Volume] = []
        for volume in self._volumes:
            await self._run(volume.archive.close)
            head = await volume.upload.complete()
            volumes.append(
                Volume(
                    number=volume.number,
                    size_bytes=volume.upload.size,
                    sha256=volume.upload.sha256,
                    version_id=head.version_id,
                )
            )
        document: StoredObject | None = None
        if self._document is not None:
            head = await self._document.complete()
            document = StoredObject(
                size_bytes=self._document.size,
                sha256=self._document.sha256,
                version_id=head.version_id,
            )
        self._finished = True
        return ArchiveResult(
            archive_sha256=archive_sha256(volumes),
            size_bytes=sum(volume.size_bytes for volume in volumes),
            artifacts=tuple(self._artifacts),
            manifest_sha256=hashlib.sha256(data).hexdigest(),
            document_sha256=None if document is None else document.sha256,
            stored=StoredObjects(volumes=tuple(volumes), document=document),
        )


async def _chunks(chunks: Iterable[bytes] | AsyncIterable[bytes]) -> AsyncIterable[bytes]:
    if isinstance(chunks, AsyncIterable):
        async for chunk in chunks:
            yield chunk
    else:
        for chunk in chunks:
            yield chunk
