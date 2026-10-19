"""El flujo de bytes de la exportación contra LocalStack (TASK-345; LC-LAZ-22; PAT-LAZ-REN-03;
NFR-LAZ-15 y 49; H-LAZ-6; A-47).

- **La hipótesis del ZIP sobre un destino no reposicionable** (``tech-stack-decisions.md`` §3): los
  volúmenes que el flujo sube por partes se descargan y se abren con ``zipfile`` (``testzip``) y
  con un verificador externo, ``unzip -t`` (Info-ZIP); cada entrada lleva descriptor de datos
  (bit 3), el manifiesto va en ``vol-01.zip`` con los bytes canónicos y cada artefacto tiene el
  SHA-256 que declara el manifiesto. El documento portátil es además su propio objeto.
- **Partes de 16 MiB con suma por parte** (H-LAZ-6, contingencia de cabecera por parte): un
  volumen de unos 40 MiB queda en tres partes (16, 16 y el resto), cada una con su
  ``ChecksumSHA256``, y el objeto con la suma compuesta de ``complete``.
- **2,5 GB sintéticos** (generados en flujo en un proceso hijo, sin archivo en disco): dos
  volúmenes con el manifiesto en ``vol-01.zip`` (leído con rangos: nunca se descargan 2 GB);
  el directorio temporal del proceso nunca pasa de 32 MiB; y la memoria máxima con **200 MB** y
  con **1 GB** (NFR-LAZ-49, medición informativa: el umbral se fijó con la primera medición y
  queda en la salida).
- Un fallo de la composición aborta **todas** las subidas abiertas.
- **El consumidor real** (TASK-317) con ``ExportStream``: el primer intento guarda la subida de
  ``vol-01`` en ``ExportRequest.multipart_upload_id`` y se queda colgado (proceso muerto); el
  arrendamiento vence, el segundo intento aborta lo que dejó (cero subidas sin terminar),
  compone desde cero y cierra ``ready`` con ``stored_objects``: los volúmenes y el documento con
  su ``version_id``, el mismo ``archive_sha256`` que la ejecución sin interrupción.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from tests.export_stream_support import (
    MIB,
    MemoryExportStore,
    RecordingSink,
    StreamingComposer,
    SyntheticArtifact,
    SyntheticScope,
    job_to_json,
    scope_to_json,
    synthetic_chunks,
    synthetic_job,
)
from tests.export_support import (
    CountingCheckpoints,
    ExportStack,
    export_body,
    export_stack,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from vigia_platform.accreditation.export.stream import (
    Artifact,
    ExportStream,
    ExportStreamSettings,
)
from vigia_platform.accreditation.export.volumes import (
    MANIFEST_NAME,
    PART_SIZE,
    ExportLocation,
    StoredObjects,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

BACKEND: Final = Path(__file__).resolve().parents[2]
MB: Final = 1000 * 1000
TMP_LIMIT: Final = 32 * MIB
"""``/tmp`` del proceso por debajo de 32 MiB (infraestructura §6.2, ``[objetivo propio]``)."""
MEMORY_GROWTH_LIMIT_KB: Final = 64 * 1024
"""Umbral de NFR-LAZ-49 fijado con la primera medición (WSL, 2026-10-18): la memoria máxima con
1 GB no supera la de 200 MB en más de 64 MiB (medición informativa)."""
SMALL: Final = ExportStreamSettings(
    part_size=5 * MIB, volume_max_bytes=512 * 1024, manifest_reserve_bytes=64 * 1024
)
SCOPE: Final = SyntheticScope(
    (
        SyntheticArtifact("findings.json", "findings_json", 200 * 1024, True),
        SyntheticArtifact("clips/a.mp4", "clip", 300 * 1024),
        SyntheticArtifact("clips/b.mp4", "clip", 350 * 1024),
        SyntheticArtifact("empty.json", "audit", 0, True),
    ),
    document_size=40 * 1024,
)


@pytest.fixture(scope="module")
def s3_bucket(localstack_endpoint: LocalStackEndpoint) -> Iterator[tuple[LocalStackEndpoint, str]]:
    with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-export") as name:
        yield localstack_endpoint, name


def _storage(endpoint: LocalStackEndpoint, bucket: str) -> S3Storage:
    return S3Storage(endpoint.storage_settings(bucket), SystemClock())


def _get(s3: Any, bucket: str, key: str, version_id: str | None = None) -> bytes:
    params: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        params["VersionId"] = version_id
    body: bytes = s3.get_object(**params)["Body"].read()
    return body


# --- La hipótesis del ZIP no reposicionable ----------------------------------------------------


def test_volumes_open_with_zipfile_and_with_unzip(
    s3_bucket: tuple[LocalStackEndpoint, str], tmp_path: Path
) -> None:
    endpoint, bucket = s3_bucket
    s3 = endpoint.aws_client("s3")
    job = synthetic_job()
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    composer = StreamingComposer(
        ExportStream(store=_storage(endpoint, bucket), settings=SMALL), SCOPE
    )
    package = asyncio.run(composer.compose(job, RecordingSink()))
    result = composer.results[0]
    assert len(result.stored.volumes) >= 2
    declared = {item["path"]: item["sha256"] for item in composer.manifests[0]["artifacts"]}
    names: list[str] = []
    unzip = shutil.which("unzip")
    assert unzip is not None, "falta el verificador externo unzip (Info-ZIP)"
    for volume in result.stored.volumes:
        data = _get(s3, bucket, location.volume_key(volume.number), volume.version_id)
        assert hashlib.sha256(data).hexdigest() == volume.sha256
        assert len(data) == volume.size_bytes
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert archive.testzip() is None
            for info in archive.infolist():
                # Descriptor de datos: escrito sin volver atrás sobre el destino.
                assert info.flag_bits & 0x08
                names.append(info.filename)
                if info.filename != MANIFEST_NAME:
                    digest = hashlib.sha256(archive.read(info)).hexdigest()
                    assert digest == declared[info.filename]
        path = tmp_path / volume.name
        path.write_bytes(data)
        checked = subprocess.run(
            [unzip, "-t", str(path)], capture_output=True, text=True, timeout=60, check=False
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "No errors detected" in checked.stdout
    first = _get(s3, bucket, location.volume_key(1), result.stored.volumes[0].version_id)
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.read(MANIFEST_NAME) == canonical_bytes_sync(composer.manifests[0])
    assert sorted(names) == sorted(
        ["document.pdf", MANIFEST_NAME, *(item.path for item in SCOPE.artifacts)]
    )
    document = _get(s3, bucket, location.document_key)
    assert hashlib.sha256(document).hexdigest() == package.document_sha256
    assert s3.list_multipart_uploads(Bucket=bucket, Prefix=location.prefix).get("Uploads", []) == []


def test_parts_of_16_mib_carry_their_own_checksum(
    s3_bucket: tuple[LocalStackEndpoint, str],
) -> None:
    endpoint, bucket = s3_bucket
    s3 = endpoint.aws_client("s3")
    job = synthetic_job()
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    size = 40 * MIB
    composer = StreamingComposer(
        ExportStream(store=_storage(endpoint, bucket)),
        SyntheticScope((SyntheticArtifact("clips/big.mp4", "clip", size),), document_size=1024),
    )
    asyncio.run(composer.compose(job, RecordingSink()))
    (volume,) = composer.results[0].stored.volumes
    key = location.volume_key(1)
    sizes = []
    for number in (1, 2, 3):
        part = s3.head_object(Bucket=bucket, Key=key, PartNumber=number, ChecksumMode="ENABLED")
        assert part["PartsCount"] == 3
        assert part.get("ChecksumSHA256"), part
        sizes.append(part["ContentLength"])
    assert sizes[:2] == [PART_SIZE, PART_SIZE] and sum(sizes) == volume.size_bytes
    head = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    assert head["ChecksumSHA256"].endswith("-3")  # suma compuesta de las tres partes
    assert hashlib.sha256(_get(s3, bucket, key)).hexdigest() == volume.sha256


def test_a_failing_composition_aborts_every_open_upload(
    s3_bucket: tuple[LocalStackEndpoint, str],
) -> None:
    endpoint, bucket = s3_bucket
    storage = _storage(endpoint, bucket)
    job = synthetic_job()
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    stream = ExportStream(store=storage, settings=SMALL)
    pending: list[int] = []

    async def scenario() -> None:
        async with stream.writer(job, RecordingSink()) as writer:
            await writer.add_bytes(
                Artifact(path="document.pdf", kind="document", size_bytes=4, document=True),
                b"%PDF",
            )
            for name in ("a", "b"):
                await writer.add(
                    Artifact(path=f"{name}.mp4", kind="clip", size_bytes=300 * 1024),
                    synthetic_chunks(name.encode(), 300 * 1024),
                )
            pending.append(len(await storage.pending_multipart_uploads(location.prefix)))
            raise RuntimeError("insumo roto")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())
    assert pending == [3]
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
    assert asyncio.run(storage.head_object(location.volume_key(1))) is None


# --- 2,5 GB, memoria y /tmp (NFR-LAZ-49) ---------------------------------------------------------


class _RangeFile:
    """Un objeto de S3 leído por rangos: ``zipfile`` lee solo el directorio y lo que pida."""

    def __init__(self, s3: Any, bucket: str, key: str, size: int) -> None:
        self._s3, self._bucket, self._key, self._size = s3, bucket, key, size
        self._position = 0
        self.fetched = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = base + offset
        return self._position

    def read(self, size: int = -1) -> bytes:
        end = self._size if size < 0 else min(self._size, self._position + size)
        if end <= self._position:
            return b""
        data: bytes = self._s3.get_object(
            Bucket=self._bucket, Key=self._key, Range=f"bytes={self._position}-{end - 1}"
        )["Body"].read()
        self._position += len(data)
        self.fetched += len(data)
        return data


def _clips_scope(total: int) -> SyntheticScope:
    """``total`` bytes en clips de 50 MB (el tope de un clip de U-01), generados en flujo."""
    clip = 50 * MB
    sizes = [clip] * (total // clip) + ([total % clip] if total % clip else [])
    return SyntheticScope(
        tuple(
            SyntheticArtifact(f"clips/{index:04d}.mp4", "clip", size)
            for index, size in enumerate(sizes)
        ),
        document_size=256 * 1024,
    )


def _measure(endpoint: LocalStackEndpoint, bucket: str, total: int, tmp: Path) -> dict[str, Any]:
    job = synthetic_job()
    spec = {
        "endpoint": endpoint.url,
        "bucket": bucket,
        "settings": {},
        "job": job_to_json(job),
        "scope": scope_to_json(_clips_scope(total)),
    }
    environment = {**os.environ, "TMPDIR": str(tmp), "PYTHONPATH": str(BACKEND)}
    finished = subprocess.run(
        [sys.executable, "-m", "tests.export_stream_child", json.dumps(spec)],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr[-3000:]
    lines = [json.loads(line) for line in finished.stdout.splitlines()]
    (final,) = [line for line in lines if "result" in line]
    final["job"] = job
    assert final["tmpdir"] == str(tmp)
    return final


def test_two_and_a_half_gb_make_two_volumes_without_growing_memory_or_tmp(
    s3_bucket: tuple[LocalStackEndpoint, str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    endpoint, bucket = s3_bucket
    s3 = endpoint.aws_client("s3")
    small = _measure(endpoint, bucket, 200 * MB, tmp_path_factory.mktemp("tmp200"))
    medium = _measure(endpoint, bucket, 1000 * MB, tmp_path_factory.mktemp("tmp1000"))
    large = _measure(endpoint, bucket, 2500 * MB, tmp_path_factory.mktemp("tmp2500"))
    report = {
        name: {
            "max_rss_mib": round(run["max_rss_kb"] / 1024, 1),
            "tmp_max_bytes": run["tmp_max_bytes"],
            "volumes": [(v["number"], v["size_bytes"]) for v in run["result"]["volumes"]],
        }
        for name, run in (("200MB", small), ("1GB", medium), ("2.5GB", large))
    }
    print(f"\nNFR-LAZ-49 (informativa): {json.dumps(report)}")
    for run in (small, medium, large):
        assert run["tmp_max_bytes"] < TMP_LIMIT
    assert medium["max_rss_kb"] - small["max_rss_kb"] <= MEMORY_GROWTH_LIMIT_KB, report
    assert large["max_rss_kb"] - small["max_rss_kb"] <= MEMORY_GROWTH_LIMIT_KB, report
    # 2,5 GB: dos volúmenes, ninguno por encima de 2 GB, con el manifiesto en vol-01.
    volumes = StoredObjects.from_json({"volumes": large["result"]["volumes"]}).volumes
    assert [volume.number for volume in volumes] == [1, 2]
    assert all(volume.size_bytes <= 2 * 1000**3 for volume in volumes)
    job = large["job"]
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    names = []
    for volume in volumes:
        ranged = _RangeFile(s3, bucket, location.volume_key(volume.number), volume.size_bytes)
        with zipfile.ZipFile(ranged) as archive:  # type: ignore[call-overload]
            names.append(archive.namelist())
            if volume.number == 1:
                manifest = archive.read(MANIFEST_NAME)
                assert manifest == canonical_bytes_sync(large["result"]["manifest"])
        assert ranged.fetched < 4 * MIB  # solo el directorio central y el manifiesto
    assert MANIFEST_NAME in names[0] and MANIFEST_NAME not in names[1]
    assert sum(len(entries) for entries in names) == len(_clips_scope(2500 * MB).artifacts) + 2


# --- El consumidor real con el flujo -----------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_multipart") as built:
        yield built


def test_the_worker_retakes_aborting_what_the_dead_attempt_left(
    stack: ExportStack, s3_bucket: tuple[LocalStackEndpoint, str]
) -> None:
    endpoint, bucket = s3_bucket
    storage = _storage(endpoint, bucket)
    site = stack.site(plants=1, zones=2)
    plant = next(iter(site.plants))
    person = stack.member(site)
    response = stack.request_export(
        person,
        export_body(plant, list(site.plants[plant]), start=stack.now() - timedelta(days=40)),
    )
    assert response.status_code == 202, response.text
    organization, export_id = site.organization_id, uuid.UUID(response.json()["export_id"])
    location = ExportLocation(organization, plant, export_id)
    stream = ExportStream(store=storage, settings=SMALL)
    paused, never = asyncio.Event(), asyncio.Event()

    class Hooked:
        """El ``ExportSink`` del consumidor con el gancho de prueba al 50 %."""

        def __init__(self, sink: Any) -> None:
            self._sink = sink

        async def upload_started(self, multipart_upload_id: str) -> None:
            await self._sink.upload_started(multipart_upload_id)

        async def artifact_started(self, artifact: str) -> None:
            await self._sink.artifact_started(artifact)

        async def artifact_finished(self, artifact: str, done: int, total: int) -> None:
            await self._sink.artifact_finished(artifact, done, total)
            if done * 100 // total >= 50:
                paused.set()
                await never.wait()  # el proceso muere aquí: nada más ocurre

    class Dying(StreamingComposer):
        async def compose(self, job: Any, sink: Any) -> Any:
            return await super().compose(job, Hooked(sink))

    first = Dying(stream, SCOPE)
    second = StreamingComposer(stream, SCOPE)
    checkpoints = CountingCheckpoints()
    a = stack.export_worker(
        composer=first, checkpoints=checkpoints, uploads=stream, database=stack.new_database()
    )
    b = stack.export_worker(
        composer=second, checkpoints=checkpoints, uploads=stream, database=stack.new_database()
    )
    started = stack.now()

    async def scenario() -> None:
        dying = asyncio.create_task(a.run(stack.event_context(organization), export_id))
        await asyncio.wait_for(paused.wait(), 60)
        row = await stack.export_row_now(export_id)
        pending = await storage.pending_multipart_uploads(location.prefix)
        assert row["multipart_upload_id"] in {upload_id for _, upload_id in pending}
        assert len(pending) >= 2  # el documento y vol-01 como mínimo
        assert await storage.head_object(location.volume_key(1)) is None
        stack.clock.set(started + timedelta(seconds=60, milliseconds=1))
        await b.run(stack.event_context(organization), export_id)
        assert await storage.pending_multipart_uploads(location.prefix) == ()
        dying.cancel()
        with pytest.raises(BaseException):  # noqa: B017 - cancelado o sin arrendamiento
            await dying

    stack.run(scenario())
    row = stack.export_row(export_id)
    assert (row["state"], row["attempt_count"], row["multipart_upload_id"]) == ("ready", 2, None)
    package = stack.package_row(export_id)
    stored = StoredObjects.from_json(
        package["stored_objects"]
        if not isinstance(package["stored_objects"], str)
        else json.loads(package["stored_objects"])
    )
    assert stored == second.results[0].stored
    assert all(volume.version_id for volume in stored.volumes)
    assert stored.document is not None and stored.document.version_id
    assert package["archive_sha256"] == second.results[0].archive_sha256
    # El mismo paquete que una ejecución sin interrupción del mismo trabajo.
    reference = StreamingComposer(ExportStream(store=MemoryExportStore(), settings=SMALL), SCOPE)
    (job_b,) = second.jobs
    assert first.jobs[0].generated_at == job_b.generated_at and job_b.attempt == 2
    asyncio.run(reference.compose(job_b, RecordingSink()))
    assert reference.results[0].archive_sha256 == package["archive_sha256"]
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
