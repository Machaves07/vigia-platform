"""PR-LAZ-49 con el compositor sintético determinista (TASK-345; FS-LAZ-05 en comportamiento;
NFR-LAZ-12; BR-LAZ-101).

**Propiedad** (alcances e instantes de interrupción generados, más 40 % y 80 % explícitos): un
proceso hijo (``tests.export_stream_child``) compone el paquete contra LocalStack con
``ExportStream``; al llegar al porcentaje generado se para en su gancho de progreso (que solo
existe en ese proceso de prueba) y el padre lo mata con **``SIGKILL``**: no corre ningún
``finally`` ni se aborta nada. En ese instante:

- no existe **ningún** ``vol-*.zip`` completo ni el ``document.pdf`` (nada publicado como
  paquete), y sí la subida por partes que el hijo guardó (la de ``vol-01``).

La retoma, en este proceso y **desde cero** (``abort_previous_upload`` con la subida guardada y,
después, una composición nueva con otro número de intento):

- deja **cero** subidas por partes sin terminar bajo el prefijo de la exportación
  (``ListMultipartUploads`` de LocalStack), antes y después de componer;
- produce el **mismo** ``archive_sha256``, el mismo ``document_sha256``, los mismos volúmenes
  (SHA-256 y tamaño) y el mismo manifiesto que la ejecución sin interrupción (la de referencia,
  sobre el almacén en memoria: los bytes no dependen del almacén);
- y lo que queda en LocalStack es exactamente eso (SHA-256 de cada volumen descargado).

Cada ejemplo lanza y mata un proceso de verdad: por eso los ejemplos generados son pocos
(``max_examples``), con los dos instantes del escenario FS-LAZ-05 siempre presentes. Solo datos
generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tests.export_stream_support import (
    MemoryExportStore,
    RecordingSink,
    StreamingComposer,
    SyntheticArtifact,
    SyntheticScope,
    job_to_json,
    real_job,
    scope_to_json,
    synthetic_job,
)
from tests.integration.conftest import LocalStackEndpoint, versioned_bucket
from tests.properties.laz.strategies.export import WorldPorts, job_world
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.contents import EMPTY_SHA256, ExportComposer
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.accreditation.export.volumes import ExportLocation
from vigia_platform.accreditation.verifier_bundle import VERIFIER_VERSION
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

BACKEND: Final = Path(__file__).resolve().parents[3]
KIB: Final = 1024
SETTINGS: Final = {
    "part_size": 5 * 1024 * KIB,
    "volume_max_bytes": 512 * KIB,
    "manifest_reserve_bytes": 64 * KIB,
}
LINE_TIMEOUT_SECONDS: Final = 120.0
"""Tope de espera de cada línea del hijo (no decide nada: solo evita colgarse)."""

_KINDS: Final = ("clip", "attachment", "findings_json", "coverage", "audit")
_JSON: Final = frozenset({"findings_json", "coverage", "audit"})


@st.composite
def _scopes(draw: st.DrawFn) -> SyntheticScope:
    count = draw(st.integers(1, 5))
    artifacts = []
    for index in range(count):
        kind = draw(st.sampled_from(_KINDS))
        size = draw(st.integers(0, 400 * KIB))
        artifacts.append(SyntheticArtifact(f"{kind}/{index}.bin", kind, size, kind in _JSON))
    return SyntheticScope(tuple(artifacts), draw(st.integers(1, 64 * KIB)))


_FS_LAZ_05_SCOPE: Final = SyntheticScope(
    (
        SyntheticArtifact("findings.json", "findings_json", 120 * KIB, True),
        SyntheticArtifact("clips/1.mp4", "clip", 300 * KIB),
        SyntheticArtifact("clips/2.mp4", "clip", 350 * KIB),
        SyntheticArtifact("coverage.json", "coverage", 40 * KIB, True),
        SyntheticArtifact("audit.json", "audit", 60 * KIB, True),
    ),
    document_size=30 * KIB,
)
"""Seis artefactos (con el documento): el progreso pasa por 33, 50, 66, 83 y 100 %, así que el
40 % para tras el tercero y el 80 % tras el quinto, con dos volúmenes abiertos."""


@pytest.fixture(scope="module")
def bucket(localstack_endpoint: LocalStackEndpoint) -> Iterator[tuple[LocalStackEndpoint, str]]:
    with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-pr-laz-49") as name:
        yield localstack_endpoint, name


def _storage(endpoint: LocalStackEndpoint, bucket: str) -> S3Storage:
    return S3Storage(endpoint.storage_settings(bucket), SystemClock())


def _line(process: subprocess.Popen[str]) -> dict[str, Any]:
    """La siguiente línea JSON del hijo (un hilo lector las deja en ``process.lines``)."""
    lines: queue.Queue[str] = process.lines  # type: ignore[attr-defined]
    try:
        line = lines.get(timeout=LINE_TIMEOUT_SECONDS)
    except queue.Empty:
        line = ""
    if not line:
        process.kill()
        process.wait(timeout=60)
        stderr = process.stderr.read() if process.stderr is not None else ""
        pytest.fail(f"el proceso hijo terminó sin pararse: {stderr[-2000:]}")
    message: dict[str, Any] = json.loads(line)
    return message


def _spawn(spec: dict[str, Any], tmp: Path) -> subprocess.Popen[str]:
    environment = {**os.environ, "TMPDIR": str(tmp), "PYTHONPATH": str(BACKEND)}
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.export_stream_child", json.dumps(spec)],
        cwd=BACKEND,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put("")

    process.lines = lines  # type: ignore[attr-defined]
    threading.Thread(target=read, daemon=True).start()
    return process


def _check_resume(
    endpoint: LocalStackEndpoint,
    bucket: str,
    scope: SyntheticScope,
    kill_percent: int,
    tmp: Path,
) -> None:
    job = synthetic_job()
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    stream_settings = ExportStreamSettings(**SETTINGS)
    # Referencia: la misma composición sin interrupción (los bytes no dependen del almacén).
    reference = StreamingComposer(
        ExportStream(store=MemoryExportStore(), settings=stream_settings), scope
    )
    expected = asyncio.run(reference.compose(job, RecordingSink()))
    expected_volumes = reference.results[0].stored.volumes

    s3 = endpoint.aws_client("s3")
    storage = _storage(endpoint, bucket)
    process = _spawn(
        {
            "endpoint": endpoint.url,
            "bucket": bucket,
            "settings": SETTINGS,
            "job": job_to_json(job),
            "scope": scope_to_json(scope),
            "kill_percent": kill_percent,
        },
        tmp,
    )
    try:
        message = _line(process)
        while "paused" not in message:
            assert "progress" in message, message
            message = _line(process)
        assert message["paused"] >= kill_percent
        stored_upload = message["upload_id"]
        # El instante de la muerte: nada publicado como paquete, la subida guardada abierta.
        for number in range(1, len(expected_volumes) + 2):
            assert asyncio.run(storage.head_object(location.volume_key(number))) is None
        assert asyncio.run(storage.head_object(location.document_key)) is None
        pending = asyncio.run(storage.pending_multipart_uploads(location.prefix))
        assert stored_upload in {upload_id for _, upload_id in pending}
        process.send_signal(signal.SIGKILL)
        assert process.wait(timeout=60) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)
        for stream in (process.stdin, process.stderr):
            if stream is not None:
                stream.close()
    # Tras la muerte quedan fragmentos: la retoma los abandona por su identificador y barre.
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix))
    resumed = ExportStream(store=storage, settings=stream_settings)
    asyncio.run(resumed.abort_previous_upload(location, stored_upload))
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
    retaken = synthetic_job(
        organization_id=job.organization_id,
        plant_id=job.plant_id,
        export_id=job.export_id,
        attempt=2,
    )
    composer = StreamingComposer(resumed, scope)
    package = asyncio.run(composer.compose(retaken, RecordingSink()))
    assert package.archive_sha256 == expected.archive_sha256
    assert package.document_sha256 == expected.document_sha256
    assert composer.manifests == reference.manifests
    volumes = composer.results[0].stored.volumes
    assert [(v.sha256, v.size_bytes) for v in volumes] == [
        (v.sha256, v.size_bytes) for v in expected_volumes
    ]
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
    assert s3.list_multipart_uploads(Bucket=bucket, Prefix=location.prefix).get("Uploads", []) == []
    for volume in volumes:
        body = s3.get_object(
            Bucket=bucket, Key=location.volume_key(volume.number), VersionId=volume.version_id
        )["Body"].read()
        assert hashlib.sha256(body).hexdigest() == volume.sha256


@settings(
    max_examples=4,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(scope=_scopes(), kill_percent=st.integers(1, 100))
@example(scope=_FS_LAZ_05_SCOPE, kill_percent=40)
@example(scope=_FS_LAZ_05_SCOPE, kill_percent=80)
def test_pr_laz_49_killed_worker_resumes_to_the_same_package(
    bucket: tuple[LocalStackEndpoint, str],
    tmp_path_factory: pytest.TempPathFactory,
    scope: SyntheticScope,
    kill_percent: int,
) -> None:
    endpoint, name = bucket
    _check_resume(endpoint, name, scope, kill_percent, tmp_path_factory.mktemp("tmp"))


REAL_SETTINGS: Final = {
    "part_size": 5 * 1024 * KIB,
    # 48 KiB bastaba mientras el documento se declaraba ausente (hasta TASK-319, VIG-236): con
    # el documento real (``full_fonts=True`` por el determinismo del PR-LAZ-49, NFR-LAZ-54), un
    # solo artefacto no comprimido ronda los 500 KiB, más que cualquier volumen pequeño que no lo
    # contenga entero. 530 KiB sigue siendo un volumen pequeño: cabe el documento solo, pero no
    # junto al resto del contenido del primer volumen y la reserva del manifiesto, así que el
    # paquete real del escenario sigue ocupando varios.
    "volume_max_bytes": 530 * KIB,
    "manifest_reserve_bytes": 40 * KIB,
}
"""Volúmenes pequeños: el paquete real del escenario ocupa varios."""


def _real_composer(stream: ExportStream, job: ExportJob, findings: int) -> ExportComposer:
    return ExportComposer(
        stream=stream, ports=WorldPorts(job_world(job, findings_per_zone=findings)).ports()
    )


@pytest.mark.parametrize("form", ["with_clips", "without_clips"])
@pytest.mark.parametrize("kill_percent", [40, 80])
def test_pr_laz_49_with_real_content_resumes_to_the_same_package(
    bucket: tuple[LocalStackEndpoint, str],
    tmp_path: Path,
    kill_percent: int,
    form: str,
) -> None:
    """TASK-318 y TASK-319: con el compositor **real** (hallazgos, cobertura, auditoría, puntos
    de control, claves, clips, documento portátil y manifiesto), matar el proceso al 40 % y al
    80 % y retomar desde cero da el mismo ``archive_sha256``, el mismo ``document_sha256`` real
    (el documento es determinista: PR-LAZ-49 también para él), los mismos volúmenes y el mismo
    manifiesto que sin interrupción."""
    endpoint, name = bucket
    real = {"zones": 2, "form": form, "findings": 6}
    job = real_job(synthetic_job(), real)
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    settings_ = ExportStreamSettings(**REAL_SETTINGS)
    reference = asyncio.run(
        _real_composer(
            ExportStream(store=MemoryExportStore(), settings=settings_), job, real["findings"]
        ).compose(job, RecordingSink())
    )
    assert len(reference.stored.volumes) > 1
    assert reference.document_sha256 != EMPTY_SHA256
    storage = _storage(endpoint, name)
    process = _spawn(
        {
            "endpoint": endpoint.url,
            "bucket": name,
            "settings": REAL_SETTINGS,
            "job": job_to_json(job),
            "real": real,
            "kill_percent": kill_percent,
        },
        tmp_path,
    )
    try:
        message = _line(process)
        while "paused" not in message:
            assert "progress" in message, message
            message = _line(process)
        assert message["paused"] >= kill_percent
        stored_upload = message["upload_id"]
        for number in range(1, len(reference.stored.volumes) + 2):
            assert asyncio.run(storage.head_object(location.volume_key(number))) is None
        process.send_signal(signal.SIGKILL)
        assert process.wait(timeout=60) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)
        for stream in (process.stdin, process.stderr):
            if stream is not None:
                stream.close()
    resumed = ExportStream(store=storage, settings=settings_)
    asyncio.run(resumed.abort_previous_upload(location, stored_upload))
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
    retaken = dataclasses.replace(job, attempt=2)
    package = asyncio.run(
        _real_composer(resumed, retaken, real["findings"]).compose(retaken, RecordingSink())
    )
    assert package.archive_sha256 == reference.archive_sha256
    assert package.document_sha256 == reference.document_sha256
    assert package.manifest == reference.manifest
    assert package.manifest_sha256 == reference.manifest_sha256
    assert [(v.sha256, v.size_bytes) for v in package.stored.volumes] == [
        (v.sha256, v.size_bytes) for v in reference.stored.volumes
    ]
    # Desde TASK-319 y TASK-346 viajan el documento y el verificador: nada se declara ausente.
    assert package.manifest["absent_entries"] == []
    assert {entry["kind"] for entry in package.manifest["artifacts"]} >= {"document", "verifier"}
    assert package.manifest["verifier_version"] == VERIFIER_VERSION
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()


def test_the_child_pauses_with_two_volumes_open_at_40_and_80_percent(
    bucket: tuple[LocalStackEndpoint, str], tmp_path: Path
) -> None:
    """El escenario fijo de FS-LAZ-05 deja, al morir, más de una subida abierta (dos volúmenes
    y el documento): la retoma no puede limitarse a la subida guardada."""
    endpoint, name = bucket
    job = synthetic_job()
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    storage = _storage(endpoint, name)
    process = _spawn(
        {
            "endpoint": endpoint.url,
            "bucket": name,
            "settings": SETTINGS,
            "job": job_to_json(job),
            "scope": scope_to_json(_FS_LAZ_05_SCOPE),
            "kill_percent": 80,
        },
        tmp_path,
    )
    try:
        message = _line(process)
        while "paused" not in message:
            message = _line(process)
        pending = asyncio.run(storage.pending_multipart_uploads(location.prefix))
        assert len(pending) >= 3, pending
    finally:
        process.kill()
        process.wait(timeout=60)
        for stream in (process.stdin, process.stderr):
            if stream is not None:
                stream.close()
    asyncio.run(ExportStream(store=storage).abort_previous_upload(location, str(uuid.uuid4())))
    assert asyncio.run(storage.pending_multipart_uploads(location.prefix)) == ()
